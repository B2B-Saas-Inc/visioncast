"""Paid tickets through Stripe hosted Checkout. Contract: docs/tickets.md.

What this module guarantees:

* Free stays the default: a price is ``Occurrence.ticket_price_minor`` (else the event's);
  both null means free and ``registration.register`` behaves exactly as before.
* A paid registration never creates a Registration, consent, reminder or outbox row up
  front. ``create_hold`` (inside the registration transaction, under the occurrence row
  lock taken by ``registration._load_occurrence``) holds a seat in a ``TicketOrder`` with
  an immutable price snapshot; ``start_checkout`` then calls Stripe outside any DB lock.
* Fulfillment happens only in ``process_webhook`` for a signature-verified
  ``checkout.session.completed`` / ``async_payment_succeeded`` event whose session id,
  ``client_reference_id``, ``metadata[order_id]``, amount, currency, mode and livemode all
  match the order, and ``payment_status == "paid"``. The success redirect and anything a
  browser sends are never trusted. Event ids are deduplicated durably
  (``StripeEventRecord``) in the same transaction as the fulfillment.
* A seat is released only when Stripe says the session expired (webhook or a GET from the
  sweep) or when we know nothing was sent. Unknown outcomes keep the seat
  (``needs_reconciliation``). A payment that arrives after release, for a cancelled/ended
  session or for someone already registered becomes ``refund_required``: no admission and
  no automatic refund (staff refund in Stripe, then ``mark_refunded``).
* Card only (``payment_method_types[]=card``), quantity 1, no subscriptions, coupons,
  taxes or automatic refunds. Every Stripe contract detail here needs sandbox verification.
"""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from webinars import capacity, crypto
from webinars.audit import audit
from webinars.models import (
    BrowserCapability,
    Event,
    Occurrence,
    PaymentAccount,
    Registration,
    ReminderSchedule,
    StripeEventRecord,
    TicketOrder,
)

STRIPE_HOST = "api.stripe.com"
STRIPE_ALLOWED_HOSTS = frozenset({STRIPE_HOST})
CHECKOUT_URL_HOSTS = ("checkout.stripe.com",)
TIMEOUT_SECONDS = 10
TOTAL_TIMEOUT_SECONDS = 20
USER_AGENT = "webinar-platform-tickets/1"

# Stripe requires expires_at at least 30 minutes after creation (needs sandbox
# verification); 31 leaves room for clock skew. The hold outlives the checkout so a
# payment completed at the last second still finds its seat.
DEFAULT_CHECKOUT_MINUTES = 31
MAX_CHECKOUT_MINUTES = 24 * 60
HOLD_GRACE_MINUTES = 15
STATUS_TOKEN_DAYS = 7
# Stripe keeps idempotency keys for at least 24 hours; replays stop before that.
IDEMPOTENT_REPLAY_HOURS = 23
RECHECK_MINUTES = 2
SIGNATURE_TOLERANCE_SECONDS = 300
MAX_WEBHOOK_BYTES = 1024 * 1024
MAX_SIGNATURE_HEADER = 4096

MIN_AMOUNT_MINOR = 50
MAX_AMOUNT_MINOR = 99_999_999
# Explicit allowlist (ISO 4217, lower case) -> minor-unit exponent. HUF/TWD/ISK and other
# currencies with Stripe-specific rounding rules are deliberately not offered.
CURRENCIES = {
    "usd": 2, "eur": 2, "gbp": 2, "cad": 2, "aud": 2, "nzd": 2, "chf": 2, "sek": 2, "nok": 2, "dkk": 2,
    "pln": 2, "czk": 2, "ron": 2, "bgn": 2, "sgd": 2, "hkd": 2, "inr": 2, "mxn": 2, "brl": 2, "zar": 2,
    "ils": 2, "aed": 2, "jpy": 0, "krw": 0,
}
CURRENCY_CHOICES = [(c, c.upper()) for c in sorted(CURRENCIES)]

_SECRET_KEY_PREFIX = {"test": ("sk_test_", "rk_test_"), "live": ("sk_live_", "rk_live_")}
_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


class PaymentsUnavailable(Exception):
    """No enabled, fully configured payment account (never fake a purchase)."""


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


def clean_price(amount, currency):
    """Validate ``(amount_minor|None, currency)``; returns ``(None, "")`` for free."""
    if amount in (None, ""):
        return None, ""
    if isinstance(amount, bool) or not isinstance(amount, int):
        try:
            amount = int(str(amount).strip())
        except (TypeError, ValueError):
            raise ValidationError({"ticket_price_minor": "Enter the price as a whole number of minor units (e.g. cents)."})
    currency = (currency or "").strip().lower()
    errors = {}
    if not MIN_AMOUNT_MINOR <= amount <= MAX_AMOUNT_MINOR:
        errors["ticket_price_minor"] = f"Price must be between {MIN_AMOUNT_MINOR} and {MAX_AMOUNT_MINOR} minor units."
    if currency not in CURRENCIES:
        errors["ticket_currency"] = "Choose a supported currency."
    if errors:
        raise ValidationError(errors)
    return amount, currency


def parse_major_amount(text, currency):
    """'25.00' + 'usd' -> 2500. Decimal input from the studio; never floats."""
    from decimal import Decimal, InvalidOperation

    text = (text or "").strip()
    if not text:
        return None
    currency = (currency or "").strip().lower()
    if currency not in CURRENCIES:
        raise ValidationError({"ticket_currency": "Choose a supported currency."})
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValidationError({"ticket_price_minor": "Enter a price such as 25 or 25.00."})
    exponent = CURRENCIES[currency]
    scaled = value * (10 ** exponent)
    if value.is_nan() or value.is_infinite() or scaled != scaled.to_integral_value():
        raise ValidationError({"ticket_price_minor": f"{currency.upper()} allows {exponent} decimal places."})
    return int(scaled)


def format_amount(amount_minor, currency):
    if amount_minor is None:
        return "Free"
    exponent = CURRENCIES.get(currency, 2)
    if exponent == 0:
        return f"{amount_minor} {currency.upper()}"
    return f"{amount_minor / (10 ** exponent):.{exponent}f} {currency.upper()}"


def effective_price(occ):
    """``{"amountMinor", "currency", "source"}`` or None (free)."""
    if occ.ticket_price_minor is not None:
        return {"amountMinor": occ.ticket_price_minor, "currency": occ.ticket_currency, "source": "occurrence"}
    event = occ.event
    if event.ticket_price_minor is not None:
        return {"amountMinor": event.ticket_price_minor, "currency": event.ticket_currency, "source": "event"}
    return None


def public_ticket(occ):
    """Public projection: price only (never account or mode)."""
    price = effective_price(occ)
    if price is None:
        return None
    return {
        "amountMinor": price["amountMinor"],
        "currency": price["currency"],
        "display": format_amount(price["amountMinor"], price["currency"]),
        "available": payments_available(active_account()),
    }


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


def active_account():
    return PaymentAccount.objects.filter(enabled=True).first()


def configured_account():
    """The single account row ``save_account`` edits, enabled or not. ``enabled`` gates
    new sales only; events for orders already taken (payment, refund, dispute) must
    still be verified and applied after staff stop sales."""
    return PaymentAccount.objects.order_by("created_at").first()


def _base_url():
    return (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")


def payments_available(account):
    if account is None or not account.enabled:
        return False
    if not account.secret_key_encrypted or not account.webhook_secret_encrypted:
        return False
    if not getattr(settings, "INTEGRATION_MASTER_KEY", None):
        return False
    return _base_url().startswith("https://") or bool(getattr(settings, "DEBUG", False))


def _valid_secret_key(value, mode):
    return (
        isinstance(value, str)
        and 20 <= len(value) <= 255
        and value.startswith(_SECRET_KEY_PREFIX[mode])
        and set(value) <= _KEY_CHARS
    )


def _valid_webhook_secret(value):
    return isinstance(value, str) and value.startswith("whsec_") and 16 <= len(value) <= 255 and set(value) <= _KEY_CHARS


def account_problems(account):
    """Readiness messages (no secrets)."""
    problems = []
    if account is None:
        return ["No Stripe account is configured."]
    if not account.secret_key_encrypted:
        problems.append("Set the Stripe secret key.")
    if not account.webhook_secret_encrypted:
        problems.append("Set the webhook signing secret.")
    if not getattr(settings, "INTEGRATION_MASTER_KEY", None):
        problems.append("The operator must set INTEGRATION_MASTER_KEY.")
    if not _base_url().startswith("https://"):
        problems.append("The operator must set PUBLIC_BASE_URL to an https URL (Checkout return links).")
    from webinars import ssrf

    try:
        ssrf.validate_destination_url(f"https://{STRIPE_HOST}/")
    except ssrf.UnsafeDestination:
        problems.append(f"Ask your operator to add {STRIPE_HOST} to WEBHOOK_HOST_ALLOWLIST.")
    return problems


def webhook_url():
    return _base_url() + "/api/v1/payments/stripe/webhook"


def serialize_account(account):
    if account is None:
        return {"configured": False, "enabled": False, "mode": None, "secretKeySet": False, "webhookSecretSet": False,
                "expectedAccountId": "", "webhookUrl": webhook_url(), "problems": account_problems(None)}
    return {
        "configured": True,
        "id": str(account.pk),
        "enabled": account.enabled,
        "mode": account.mode,
        "secretKeySet": bool(account.secret_key_encrypted),
        "webhookSecretSet": bool(account.webhook_secret_encrypted),
        "expectedAccountId": account.expected_account_id,
        "webhookUrl": webhook_url(),
        "problems": account_problems(account),
    }


def save_account(*, mode=None, secret_key="", webhook_secret="", expected_account_id=None, enabled=None,
                 user=None, api_key=None):
    """Create/update the single Stripe account. Secrets are set-only: blank keeps the
    stored value. Changing mode requires re-entering a secret key of the new mode."""
    with transaction.atomic():
        account = PaymentAccount.objects.select_for_update().order_by("created_at").first()
        creating = account is None
        if creating:
            account = PaymentAccount(mode=PaymentAccount.Mode.TEST)
        errors = {}
        new_mode = account.mode if mode in (None, "") else mode
        if new_mode not in PaymentAccount.Mode.values:
            errors["mode"] = "Choose test or live."
            new_mode = account.mode
        secret_key = (secret_key or "").strip()
        webhook_secret = (webhook_secret or "").strip()
        if secret_key and not _valid_secret_key(secret_key, new_mode):
            errors["secret_key"] = (f"Expected a {new_mode} secret or restricted key "
                                    f"({' or '.join(_SECRET_KEY_PREFIX[new_mode])}...).")
        if new_mode != account.mode and not secret_key and account.secret_key_encrypted:
            errors["secret_key"] = "Enter a secret key for the new mode (keys never cross test/live)."
        if webhook_secret and not _valid_webhook_secret(webhook_secret):
            errors["webhook_secret"] = "Expected a webhook signing secret starting with whsec_."
        if expected_account_id is not None:
            expected_account_id = expected_account_id.strip()
            if expected_account_id and not (expected_account_id.startswith("acct_") and len(expected_account_id) <= 64
                                            and set(expected_account_id) <= _KEY_CHARS):
                errors["expected_account_id"] = "Expected an account id starting with acct_."
        if errors:
            raise ValidationError(errors)
        rebinding = (
            new_mode != account.mode
            or (secret_key and account.secret_key_encrypted)
            or (expected_account_id is not None and expected_account_id != account.expected_account_id)
        )
        if not creating and rebinding and TicketOrder.objects.filter(account=account, status__in=TicketOrder.HOLDING).exists():
            # Open orders replay their checkout request with the account's current key; a
            # different mode/key/account would create a new session elsewhere (or charge
            # live money for a test order). Resolve open orders first.
            raise ValidationError({"secret_key": "Orders are still open (held, in checkout or awaiting reconciliation). "
                                                 "Resolve them before changing the mode, key or account."})
        changed = []
        if new_mode != account.mode:
            account.mode = new_mode
            changed.append("mode")
        if secret_key:
            account.secret_key_encrypted = crypto.encrypt_secret(secret_key)
            changed.append("secret_key")
        if webhook_secret:
            account.webhook_secret_encrypted = crypto.encrypt_secret(webhook_secret)
            changed.append("webhook_secret")
        if expected_account_id is not None and expected_account_id != account.expected_account_id:
            account.expected_account_id = expected_account_id
            changed.append("expected_account_id")
        if enabled is not None and bool(enabled) != account.enabled:
            if enabled and (not account.secret_key_encrypted or not account.webhook_secret_encrypted):
                raise ValidationError({"enabled": "Set the secret key and webhook signing secret before enabling."})
            account.enabled = bool(enabled)
            changed.append("enabled")
        account.save()
        audit("payments.account_saved", user=user, api_key=api_key, obj=account, created=creating, fields=changed,
              mode=account.mode, enabled=account.enabled)
    return account


# ---------------------------------------------------------------------------
# Stripe transport (the only outbound path; tests monkeypatch ``stripe_request``)
# ---------------------------------------------------------------------------


def stripe_request(method, path, *, secret, form=None, idempotency_key=None):
    """Returns ``(kind, data)``: kind ``ok`` (2xx JSON object), ``failed`` (definitive: the
    request was refused or never sent) or ``unknown`` (it may have been processed)."""
    from webinars import ssrf

    url = f"https://{STRIPE_HOST}{path}"
    headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json", "User-Agent": USER_AGENT}
    version = getattr(settings, "STRIPE_API_VERSION", "") or ""
    if version:
        headers["Stripe-Version"] = version
    body = b""
    if form is not None:
        body = urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        status, raw, _retry_after, truncated = ssrf.vendor_request_bounded(
            method, url, body, headers, TIMEOUT_SECONDS, TOTAL_TIMEOUT_SECONDS, allowed_hosts=STRIPE_ALLOWED_HOSTS
        )
    except ssrf.ResolutionFailed:
        return "failed", {"error": "dns_resolution_failed"}
    except ssrf.UnsafeDestination:
        return "failed", {"error": "destination_not_allowed"}
    except Exception as exc:  # timeout / transport: the request may have reached Stripe
        return "unknown", {"error": type(exc).__name__}
    if 200 <= status < 300:
        if truncated:
            return "unknown", {"error": "response_too_large"}
        try:
            data = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "unknown", {"error": "response_unparsable"}
        if not isinstance(data, dict):
            return "unknown", {"error": "response_unexpected"}
        return "ok", data
    if status == 409 or status == 429 or status >= 500:
        return "unknown", {"error": f"http {status}"}
    return "failed", {"error": f"http {status}"}


def _secret(account):
    return crypto.decrypt_secret(account.secret_key_encrypted)


# ---------------------------------------------------------------------------
# Holds and checkout
# ---------------------------------------------------------------------------


def _iso(dt):
    return dt.astimezone(timezone.get_fixed_timezone(0)).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _checkout_minutes():
    try:
        minutes = int(getattr(settings, "TICKET_CHECKOUT_MINUTES", DEFAULT_CHECKOUT_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_CHECKOUT_MINUTES
    return max(DEFAULT_CHECKOUT_MINUTES, min(minutes, MAX_CHECKOUT_MINUTES))


def create_hold(occ, payload, price, *, person, capability, trusted, allow_started, req_hash, email_key=None):
    """Hold one seat. Caller holds the occurrence row lock and has checked capacity."""
    account = active_account()
    if not payments_available(account):
        raise PaymentsUnavailable()
    now = timezone.now()
    token = crypto.generate_token(32)
    order = TicketOrder.objects.create(
        occurrence=occ,
        person=person,
        account=account,
        status=TicketOrder.Status.HOLD,
        mode=account.mode,
        amount_minor=price["amountMinor"],
        currency=price["currency"],
        price_snapshot={
            "amountMinor": price["amountMinor"],
            "currency": price["currency"],
            "source": price["source"],
            "title": (occ.event.public_title or occ.event.internal_name)[:200],
            "startsAt": _iso(occ.starts_at),
        },
        idempotency_key=payload["idempotencyKey"],
        request_hash=req_hash,
        email_key=(email_key or "")[:254],
        caller_capability_id=capability.pk if capability is not None else None,
        capability_hash=crypto.hash_token(token),
        capability_expires_at=now + timedelta(days=STATUS_TOKEN_DAYS),
        hold_expires_at=now + timedelta(minutes=_checkout_minutes() + HOLD_GRACE_MINUTES + 5),
        pending_payload={
            "envelope": payload,
            "capabilityId": str(capability.pk) if capability is not None else None,
            "trusted": bool(trusted),
            "allowStarted": bool(allow_started),
        },
    )
    audit("ticket.hold_created", obj=order, occurrence_id=str(occ.pk), amount_minor=order.amount_minor,
          currency=order.currency, mode=order.mode)
    return order, token


def open_hold_for(occ, email_key):
    """An order still holding a seat for this person's email on this session (caller holds
    the occurrence lock), or None."""
    if not email_key:
        return None
    return (
        TicketOrder.objects.filter(occurrence=occ, email_key=email_key, status__in=TicketOrder.HOLDING)
        .order_by("-created_at")
        .first()
    )


def needs_checkout_start(order):
    return order.status == TicketOrder.Status.HOLD and not order.checkout_session_id


def _checkout_form(order, expires_at):
    base = _base_url()
    ret = f"{base}/tickets/return?order={order.pk}"
    return [
        ("mode", "payment"),
        ("payment_method_types[0]", "card"),
        ("line_items[0][quantity]", "1"),
        ("line_items[0][price_data][currency]", order.currency),
        ("line_items[0][price_data][unit_amount]", str(order.amount_minor)),
        ("line_items[0][price_data][product_data][name]", order.price_snapshot.get("title") or "Webinar ticket"),
        ("client_reference_id", str(order.pk)),
        ("metadata[order_id]", str(order.pk)),
        ("metadata[occurrence_id]", str(order.occurrence_id)),
        ("payment_intent_data[metadata][order_id]", str(order.pk)),
        ("expires_at", str(int(expires_at.timestamp()))),
        # The return pages only say "we are confirming"; they never fulfil anything.
        ("success_url", ret + "&result=success"),
        ("cancel_url", ret + "&result=cancelled"),
    ]


def _idempotency_key(order):
    return f"order-{order.pk}"


def _checkout_link(value):
    if not isinstance(value, str) or len(value) > 2000:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in CHECKOUT_URL_HOSTS:
        return ""
    return value


_SESSION_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def valid_session_id(value):
    """``cs_`` + [A-Za-z0-9_] only: the id is later interpolated into a GET path."""
    return isinstance(value, str) and value.startswith("cs_") and 4 <= len(value) <= 255 and set(value) <= _SESSION_ID_CHARS


def _apply_session(order, data):
    """Record a created session (order locked by caller). False if the response is unusable."""
    session_id = data.get("id")
    link = _checkout_link(data.get("url"))
    if not valid_session_id(session_id) or not link:
        return False
    if data.get("amount_total") not in (None, order.amount_minor) or (data.get("currency") or order.currency) != order.currency:
        return False
    if "livemode" in data and bool(data["livemode"]) != (order.mode == PaymentAccount.Mode.LIVE):
        return False
    order.checkout_session_id = session_id
    order.checkout_url = link
    expires = data.get("expires_at")
    if isinstance(expires, int) and not isinstance(expires, bool):
        from datetime import datetime, timezone as dt_timezone

        order.checkout_expires_at = datetime.fromtimestamp(expires, tz=dt_timezone.utc)
    order.status = TicketOrder.Status.CHECKOUT_OPEN
    return True


def _post_checkout(order, account):
    """Send (or replay with the same Idempotency-Key) the persisted checkout request."""
    form = order.price_snapshot.get("checkoutRequest")
    kind, data = stripe_request("POST", "/v1/checkout/sessions", secret=_secret(account), form=[tuple(p) for p in form],
                                idempotency_key=_idempotency_key(order))
    return kind, data


def start_checkout(order_id, token):
    """Create the Checkout Session for a committed hold. No DB lock is held during the call."""
    from webinars.registration import RegistrationResult, error_body

    order = TicketOrder.objects.select_related("account").get(pk=order_id)
    if not needs_checkout_start(order):
        return order_result(order, token)
    account = order.account
    try:
        secret_ok = bool(_secret(account)) if account is not None else False
    except crypto.DecryptionError:
        secret_ok = False
    if not secret_ok or not payments_available(account):
        _release(order.pk, TicketOrder.Status.CANCELLED, "payments_unavailable", only_from=(TicketOrder.Status.HOLD,))
        return RegistrationResult(503, error_body("payments_unavailable", "Ticket sales are not configured"))

    # Persist the exact request before sending: from here on a session may exist, so the
    # sweep replays the same body + Idempotency-Key instead of releasing the seat.
    with transaction.atomic():
        locked = TicketOrder.objects.select_for_update().get(pk=order.pk)
        if not needs_checkout_start(locked):
            return order_result(locked, token)
        if "checkoutRequest" not in locked.price_snapshot:
            expires_at = timezone.now() + timedelta(minutes=_checkout_minutes())
            locked.price_snapshot = {**locked.price_snapshot, "checkoutRequest": _checkout_form(locked, expires_at)}
            locked.hold_expires_at = expires_at + timedelta(minutes=HOLD_GRACE_MINUTES)
            locked.save(update_fields=["price_snapshot", "hold_expires_at", "updated_at"])
        order = locked

    kind, data = _post_checkout(order, account)
    if kind == "unknown":
        kind, data = _post_checkout(order, account)  # same Idempotency-Key: Stripe dedupes

    with transaction.atomic():
        locked = TicketOrder.objects.select_for_update().get(pk=order.pk)
        _apply_create_outcome(locked, kind, data)
        order = locked
    if order.status == TicketOrder.Status.CANCELLED:
        # 409, not 5xx: Stripe definitively refused, nothing is held, and the client must
        # retry with a new idempotency key. A 5xx would be masked as bad_gateway by the
        # Node proxy (sdk/proxy/server.js) and the SDK could not rotate its key.
        return RegistrationResult(409, error_body("checkout_failed", "Checkout could not be started. Please try again."))
    return order_result(order, token)


def _apply_create_outcome(locked, kind, data):
    """Record a checkout-create outcome on a locked order. Returns True if state changed.

    A usable session is applied whenever the order has none yet and still holds its seat
    (``hold`` or ``needs_reconciliation``), so a concurrent same-key retry that lost the
    race can never discard a session that Stripe did create.
    """
    if locked.checkout_session_id or locked.status not in (TicketOrder.Status.HOLD, TicketOrder.Status.NEEDS_RECONCILIATION):
        return False
    error = (data or {}).get("error", "") if kind != "ok" else ""
    if kind == "ok" and _apply_session(locked, data):
        locked.last_error = ""
        locked.save()
        audit("ticket.checkout_opened", obj=locked)
        return True
    if locked.status != TicketOrder.Status.HOLD:
        return False  # already awaiting reconciliation; nothing definitive learned
    if kind == "failed":
        locked.status = TicketOrder.Status.CANCELLED
        locked.pending_payload = {}
        locked.email_key = ""
        locked.last_error = f"checkout create: {error}"[:300]
        audit("ticket.checkout_failed", obj=locked, error=locked.last_error)
    elif kind == "unknown" and error == "http 409":
        # Stripe: a request with this Idempotency-Key is still in progress (a concurrent
        # retry). Stay in ``hold``; the in-flight request or the sweep applies the session.
        locked.last_error = "checkout create in progress"
    else:
        locked.status = TicketOrder.Status.NEEDS_RECONCILIATION
        locked.last_error = f"checkout create: {error or 'response_unusable'}"[:300]
        audit("ticket.needs_reconciliation", obj=locked, error=locked.last_error)
    locked.save()
    return True


def serialize_order_public(order, token=None):
    out = {
        "id": str(order.pk),
        "status": order.status,
        "registered": order.status == TicketOrder.Status.FULFILLED,
        "checkoutUrl": order.checkout_url if order.status == TicketOrder.Status.CHECKOUT_OPEN and order.checkout_url else None,
        "expiresAt": _iso(order.checkout_expires_at or order.hold_expires_at),
        "amountMinor": order.amount_minor,
        "currency": order.currency,
        "statusPath": f"/api/v1/orders/{order.pk}",
    }
    if token:
        out["statusToken"] = token
    return out


def order_result(order, token):
    """Registration-endpoint answer for a paid intent. Never a confirmation before payment."""
    from webinars.registration import RegistrationResult, build_response_body, error_body

    if order.status == TicketOrder.Status.FULFILLED:
        occ = Occurrence.objects.select_related("event").get(pk=order.occurrence_id)
        return RegistrationResult(200, {**build_response_body(occ), "order": serialize_order_public(order, token)})
    if order.status in TicketOrder.HOLDING:
        return RegistrationResult(202, {"status": "payment_required", "order": serialize_order_public(order, token)})
    body = error_body("order_closed", "This checkout is no longer available. Start a new registration.")
    body["order"] = serialize_order_public(order, token)
    return RegistrationResult(409, body)


def order_status(order_id, token):
    """Public status lookup by opaque token (hash compare + expiry). None if not allowed."""
    if not isinstance(token, str) or not 20 <= len(token) <= 200:
        return None
    order = TicketOrder.objects.filter(pk=order_id).first()
    if order is None or order.capability_expires_at <= timezone.now():
        return None
    if not hmac.compare_digest(order.capability_hash, crypto.hash_token(token)):
        return None
    return serialize_order_public(order)


def _release(order_id, status, reason, *, only_from=TicketOrder.HOLDING, user=None, api_key=None):
    with transaction.atomic():
        order = TicketOrder.objects.select_for_update().filter(pk=order_id).first()
        if order is None or order.status not in only_from:
            return False
        order.status = status
        order.pending_payload = {}
        order.email_key = ""
        order.last_error = reason[:300]
        order.save(update_fields=["status", "pending_payload", "email_key", "last_error", "updated_at"])
        audit("ticket.released", user=user, api_key=api_key, obj=order, status=status, reason=reason)
    return True


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def verify_signature(raw, header, secret, *, tolerance=SIGNATURE_TOLERANCE_SECONDS, now=None):
    """Stripe-Signature: ``t=<unix>,v1=<hex>[,v1=...]``; HMAC-SHA256 over ``t.raw``."""
    if not header or not secret or len(header) > MAX_SIGNATURE_HEADER:
        return False
    timestamp, candidates = None, []
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        if k == "t":
            timestamp = v
        elif k == "v1" and v:
            candidates.append(v)
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = int(time.time()) if now is None else int(now)
    if abs(now - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("ascii") + bytes(raw), hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(c.encode(), expected.encode()) for c in candidates)


class WebhookRejected(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def _session_mismatch(order, obj, account):
    """Reason string when the session does not belong to this order exactly, else ''."""
    if obj.get("object") != "checkout.session":
        return "object"
    if order.checkout_session_id and obj.get("id") != order.checkout_session_id:
        return "session_id"
    if not valid_session_id(obj.get("id")):
        return "session_id"
    if obj.get("client_reference_id") != str(order.pk):
        return "client_reference_id"
    if (obj.get("metadata") or {}).get("order_id") != str(order.pk):
        return "metadata"
    if obj.get("mode") not in (None, "payment"):
        return "mode"
    if bool(obj.get("livemode")) != (order.mode == PaymentAccount.Mode.LIVE):
        return "livemode"
    if order.account_id is not None and account is not None and order.account_id != account.pk:
        return "account"
    return ""


def _amount_mismatch(order, obj):
    if obj.get("amount_total") != order.amount_minor or isinstance(obj.get("amount_total"), bool):
        return "amount"
    if obj.get("currency") != order.currency:
        return "currency"
    return ""


def process_webhook(raw, signature_header, *, now=None):
    """Verify, dedupe and apply one Stripe event. Returns ``(status, body)``."""
    if len(raw) > MAX_WEBHOOK_BYTES:
        return 413, {"error": "too_large"}
    account = configured_account()
    if account is None or not account.webhook_secret_encrypted:
        return 400, {"error": "not_configured"}
    try:
        secret = crypto.decrypt_secret(account.webhook_secret_encrypted)
    except crypto.DecryptionError:
        return 400, {"error": "not_configured"}
    if not verify_signature(raw, signature_header, secret, now=now):
        return 400, {"error": "invalid_signature"}
    try:
        event = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, {"error": "invalid_payload"}
    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"].startswith("evt_"):
        return 400, {"error": "invalid_payload"}
    if bool(event.get("livemode")) != (account.mode == PaymentAccount.Mode.LIVE):
        return 400, {"error": "mode_mismatch"}
    if account.expected_account_id and event.get("account") not in (None, account.expected_account_id):
        return 400, {"error": "account_mismatch"}
    event_type = str(event.get("type") or "")[:100]
    obj = ((event.get("data") or {}).get("object")) or {}
    if not isinstance(obj, dict):
        obj = {}

    with transaction.atomic():
        try:
            with transaction.atomic():
                record = StripeEventRecord.objects.create(
                    event_id=event["id"][:255], event_type=event_type, livemode=bool(event.get("livemode"))
                )
        except IntegrityError:
            return 200, {"received": True, "duplicate": True}
        order_id = (obj.get("metadata") or {}).get("order_id") if isinstance(obj.get("metadata"), dict) else None
        outcome, order = "ignored", None
        if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            outcome, order = _on_paid(order_id, obj, account)
        elif event_type == "checkout.session.expired":
            outcome, order = _on_expired(order_id, obj, account)
        elif event_type in ("charge.refunded", "charge.dispute.created"):
            outcome, order = _on_refund_or_dispute(event_type, obj, account)
        record.outcome = outcome[:64]
        record.order = order
        record.save(update_fields=["outcome", "order"])
    return 200, {"received": True}


def _lock_order(order_id):
    """Lock occurrence then order (the global lock order). Returns ``(occ, order)`` or Nones."""
    try:
        import uuid

        uuid.UUID(str(order_id))
    except (TypeError, ValueError):
        return None, None
    occurrence_id = TicketOrder.objects.filter(pk=order_id).values_list("occurrence_id", flat=True).first()
    if occurrence_id is None:
        return None, None
    occ = capacity.lock_occurrence(occurrence_id)
    order = TicketOrder.objects.select_for_update(of=("self",)).select_related("account").get(pk=order_id)
    return occ, order


def _to_refund_required(order, reason):
    order.status = TicketOrder.Status.REFUND_REQUIRED
    order.pending_payload = {}
    order.email_key = ""
    order.last_error = reason[:300]
    order.save()
    audit("ticket.refund_required", obj=order, reason=reason)
    return "refund_required"


def _on_paid(order_id, obj, account):
    occ, order = _lock_order(order_id)
    if order is None:
        return "unknown_order", None
    mismatch = _session_mismatch(order, obj, account) or _amount_mismatch(order, obj)
    if mismatch:
        if order.status in TicketOrder.HOLDING:
            if obj.get("payment_status") == "paid":
                # A signed event from our account says money was taken, but not for exactly
                # this order: never admit, never release as unpaid; staff refund in Stripe.
                order.paid_at = order.paid_at or timezone.now()
                order.status = TicketOrder.Status.REFUND_REQUIRED
                order.pending_payload = {}
                order.email_key = ""
                intent = obj.get("payment_intent")
                if (not _session_mismatch(order, obj, account) and order.checkout_session_id == obj.get("id")
                        and isinstance(intent, str) and intent.startswith("pi_") and len(intent) <= 255):
                    # Our own session (id, reference, metadata, mode, account all match),
                    # only the amount differs: link the intent so a Stripe refund matches.
                    order.payment_intent_id = intent
            else:
                order.status = TicketOrder.Status.NEEDS_RECONCILIATION
            order.last_error = f"webhook mismatch: {mismatch}"
            order.save()
        audit("ticket.webhook_mismatch", obj=order, reason=mismatch)
        return "mismatch", order
    if obj.get("payment_status") != "paid":
        return "not_paid", order
    if order.status == TicketOrder.Status.FULFILLED:
        return "already_fulfilled", order
    if order.status in (TicketOrder.Status.REFUND_REQUIRED, TicketOrder.Status.REFUNDED):
        return "already_closed", order
    if not order.checkout_session_id:
        # Our create response was lost: the signed event proves this session is ours.
        order.checkout_session_id = obj["id"][:255]
    order.payment_intent_id = str(obj.get("payment_intent") or "")[:255]
    order.paid_at = timezone.now()
    if order.status not in TicketOrder.HOLDING:
        return _to_refund_required(order, "paid_after_release"), order
    if occ.status != Occurrence.Status.SCHEDULED or occ.ends_at <= timezone.now():
        return _to_refund_required(order, "session_unavailable"), order
    return _fulfill(occ, order), order


def _fulfill(occ, order):
    from webinars import registration as registration_mod

    pending = order.pending_payload or {}
    envelope = pending.get("envelope")
    if not isinstance(envelope, dict):
        return _to_refund_required(order, "missing_envelope")
    capability = None
    if pending.get("capabilityId"):
        capability = BrowserCapability.objects.select_related("person").filter(pk=pending["capabilityId"]).first()
        if capability is not None and (capability.revoked_at is not None or capability.person.erased_at is not None):
            capability = None
    try:
        with transaction.atomic():
            result, registration = registration_mod._process(
                envelope, capability, None, bool(pending.get("trusted")), bool(pending.get("allowStarted")),
                fulfilling=order, locked_occ=occ,
            )
    except registration_mod._Abort as abort:
        return _to_refund_required(order, f"not_admitted: {abort.result.body.get('error', {}).get('code', 'error')}")
    if registration is None or result.status_code >= 300:
        return _to_refund_required(order, "not_admitted")
    order.status = TicketOrder.Status.FULFILLED
    order.registration = registration
    order.person_id = registration.person_id
    order.pending_payload = {}
    order.email_key = ""
    order.last_error = ""
    order.save()
    audit("ticket.fulfilled", obj=order, registration_id=str(registration.pk), occurrence_id=str(occ.pk))
    return "fulfilled"


def _on_expired(order_id, obj, account):
    occ, order = _lock_order(order_id)
    if order is None:
        return "unknown_order", None
    if _session_mismatch(order, obj, account):
        return "mismatch", order
    if order.status not in TicketOrder.HOLDING:
        return "ignored", order
    order.status = TicketOrder.Status.EXPIRED
    order.pending_payload = {}
    order.email_key = ""
    order.last_error = "checkout expired"
    order.save(update_fields=["status", "pending_payload", "email_key", "last_error", "updated_at"])
    audit("ticket.released", obj=order, status="expired", reason="stripe_expired")
    return "expired", order


def _on_refund_or_dispute(event_type, obj, account):
    """A refund or dispute started in Stripe (dashboard, chargeback). Matched only by the
    payment intent recorded at fulfillment, on this account and mode. A full refund is
    recorded like ``mark_refunded``; a dispute flags the order ``refund_required`` so the
    waiting room stops admitting it. Partial refunds are left to staff."""
    intent = obj.get("payment_intent")
    if not isinstance(intent, str) or not intent.startswith("pi_") or len(intent) > 255:
        return "ignored", None
    if bool(obj.get("livemode")) != (account.mode == PaymentAccount.Mode.LIVE):
        return "mismatch", None
    order = TicketOrder.objects.filter(payment_intent_id=intent, account=account).first()
    if order is None:
        return "unknown_order", None
    if event_type == "charge.refunded":
        if obj.get("refunded") is not True:
            audit("ticket.partial_refund_seen", obj=order)
            return "partial_refund_ignored", order
        if order.status in (TicketOrder.Status.FULFILLED, TicketOrder.Status.REFUND_REQUIRED,
                            TicketOrder.Status.NEEDS_RECONCILIATION):
            mark_refunded(order, source="stripe")
            return "refunded", order
        return "ignored", order
    with transaction.atomic():
        locked = TicketOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status == TicketOrder.Status.FULFILLED:
            locked.status = TicketOrder.Status.REFUND_REQUIRED
            locked.last_error = "dispute opened in Stripe"
            locked.save(update_fields=["status", "last_error", "updated_at"])
            audit("ticket.disputed", obj=locked)
            return "disputed", locked
    return "ignored", order


def flag_cancelled_occurrence(occ):
    """Paid registrations of a cancelled session are owed a refund (staff, in Stripe)."""
    n = TicketOrder.objects.filter(occurrence=occ, status=TicketOrder.Status.FULFILLED).update(
        status=TicketOrder.Status.REFUND_REQUIRED, last_error="session cancelled", updated_at=timezone.now()
    )
    if n:
        audit("ticket.cancelled_session_refunds", obj=occ, orders=n)
    return n


# ---------------------------------------------------------------------------
# Sweep (worker): reconcile without ever releasing a possibly paid seat
# ---------------------------------------------------------------------------


def sweep(now=None, limit=25):
    """Reconcile stale orders. Returns the number of orders whose state changed."""
    now = now or timezone.now()
    changed = 0
    from django.db.models import Q

    due = Q(hold_expires_at__lte=now) | (
        # A create that was sent but has no session yet (lost response, concurrent-retry 409)
        # is replayed with the same Idempotency-Key well before the hold expires, while the
        # buyer can still use the Checkout.
        Q(checkout_session_id__isnull=True) & Q(price_snapshot__has_key="checkoutRequest")
    ) | ~Q(occurrence__status=Occurrence.Status.SCHEDULED)  # cancelled session: close open checkouts now
    stale = (
        TicketOrder.objects.filter(due, status__in=TicketOrder.HOLDING)
        # Every order is re-checked at most every RECHECK_MINUTES (errors bump updated_at too).
        .exclude(updated_at__gt=now - timedelta(minutes=RECHECK_MINUTES))
        .select_related("account", "occurrence")
        .order_by("updated_at")[:limit]
    )
    for order in stale:
        try:
            changed += 1 if _reconcile_one(order, now) else 0
        except Exception as exc:
            # Never stall the sweep on one order: keep its seat, flag it and move it to the
            # back of the queue (updated_at is bumped).
            _mark_reconcile(order, f"sweep error: {type(exc).__name__}")
    return changed


def _reconcile_one(order, now):
    account = order.account
    sent = "checkoutRequest" in (order.price_snapshot or {})
    # A cancelled (or otherwise unscheduled) session cannot admit anyone: open checkouts
    # are expired in Stripe instead of waiting for the hold to run out (a payment that
    # still lands becomes refund_required in _on_paid).
    closed = order.occurrence.status != Occurrence.Status.SCHEDULED
    if order.status == TicketOrder.Status.HOLD and not sent:
        if order.hold_expires_at > now and not closed:
            return False
        # Nothing was ever sent to Stripe: safe to release.
        return _release(order.pk, TicketOrder.Status.EXPIRED, "hold expired before checkout",
                        only_from=(TicketOrder.Status.HOLD,))
    if account is None:
        _mark_reconcile(order, "no payment account; check Stripe and resolve in the studio")
        return False
    if not order.checkout_session_id:
        if order.created_at < now - timedelta(hours=IDEMPOTENT_REPLAY_HOURS):
            _mark_reconcile(order, "checkout outcome unknown; check Stripe and resolve in the studio")
            return False
        kind, data = _post_checkout(order, account)
        with transaction.atomic():
            locked = TicketOrder.objects.select_for_update().get(pk=order.pk)
            if locked.status not in TicketOrder.HOLDING or locked.checkout_session_id:
                return False
            if kind == "ok" and _apply_session(locked, data):
                locked.last_error = ""
                locked.save()
                return True
            if kind == "unknown" and data.get("error") == "http 409":
                locked.last_error = "checkout create in progress"
                locked.save(update_fields=["last_error", "updated_at"])
                return False
            if kind == "failed" and data.get("error") in ("http 400", "http 402"):
                # The identical replay was refused by Stripe, so the original was refused too
                # (or never arrived): no session exists and the seat can go.
                locked.status = TicketOrder.Status.CANCELLED
                locked.pending_payload = {}
                locked.email_key = ""
                locked.last_error = f"checkout create: {data['error']}"
                locked.save()
                audit("ticket.released", obj=locked, status="cancelled", reason="stripe_refused_replay")
                return True
            if locked.status != TicketOrder.Status.NEEDS_RECONCILIATION:
                locked.status = TicketOrder.Status.NEEDS_RECONCILIATION
                locked.last_error = "checkout create: outcome unknown"
                locked.save(update_fields=["status", "last_error", "updated_at"])
                return True
        return False
    kind, data = stripe_request("GET", f"/v1/checkout/sessions/{order.checkout_session_id}", secret=_secret(account))
    if kind != "ok" or data.get("id") != order.checkout_session_id:
        _mark_reconcile(order, "session lookup failed; seat kept")
        return False
    status, paid = data.get("status"), data.get("payment_status")
    if order.hold_expires_at > now and status == "open" and not closed:
        return False  # still a live checkout
    if status == "expired" and paid != "paid":
        return _release(order.pk, TicketOrder.Status.EXPIRED, "stripe session expired")
    if paid == "paid":
        # Paid: keep the seat and wait for the signed webhook to fulfil. ``paid_at`` makes
        # the order refundable but never releasable as unpaid.
        TicketOrder.objects.filter(pk=order.pk, paid_at__isnull=True).update(paid_at=now)
        _mark_reconcile(order, "paid in Stripe; awaiting verified webhook")
        return False
    if status == "complete":
        _mark_reconcile(order, "checkout complete but not paid in Stripe; check the payment there")
        return False
    if status == "open":
        stripe_request("POST", f"/v1/checkout/sessions/{order.checkout_session_id}/expire", secret=_secret(account),
                       form=[], idempotency_key=f"expire-{order.pk}")
    return False


def _mark_reconcile(order, reason):
    TicketOrder.objects.filter(pk=order.pk, status__in=TicketOrder.HOLDING).update(
        status=TicketOrder.Status.NEEDS_RECONCILIATION, last_error=reason[:300], updated_at=timezone.now()
    )


# ---------------------------------------------------------------------------
# Staff actions (bookkeeping only: no money moves from here)
# ---------------------------------------------------------------------------


def release_unpaid(order, *, user=None, api_key=None):
    """Staff checked Stripe and the session was not paid: release the seat.

    Refused when payment was ever seen (``paid_at``) and, when a session id is known,
    re-checked with Stripe first: a paid session is never released as unpaid (record a
    refund instead)."""
    if order.status != TicketOrder.Status.NEEDS_RECONCILIATION:
        raise ValidationError("Only orders that need reconciliation can be released by hand.")
    if order.paid_at is not None:
        raise ValidationError("Stripe reported this order as paid. Refund it in Stripe, then record the refund.")
    if order.checkout_session_id and order.account is not None:
        kind, data = stripe_request("GET", f"/v1/checkout/sessions/{order.checkout_session_id}", secret=_secret(order.account))
        if kind != "ok" or data.get("id") != order.checkout_session_id:
            raise ValidationError("Stripe could not be reached to confirm the checkout is unpaid; try again later.")
        if data.get("payment_status") == "paid" or data.get("status") == "complete":
            TicketOrder.objects.filter(pk=order.pk, paid_at__isnull=True).update(paid_at=timezone.now())
            raise ValidationError("Stripe shows this checkout as completed. Refund it in Stripe, then record the refund.")
    return _release(order.pk, TicketOrder.Status.EXPIRED, "released by staff after checking Stripe",
                    only_from=(TicketOrder.Status.NEEDS_RECONCILIATION,), user=user, api_key=api_key)


REFUNDABLE = (TicketOrder.Status.REFUND_REQUIRED, TicketOrder.Status.FULFILLED, TicketOrder.Status.NEEDS_RECONCILIATION)


def mark_refunded(order, *, user=None, api_key=None, source="staff"):
    """Record a refund made in Stripe (by staff, or seen via ``charge.refunded``).

    Cancels the registration only when no other fulfilled order still pays for it."""
    from webinars import outbox

    with transaction.atomic():
        occ = capacity.lock_occurrence(order.occurrence_id)
        locked = TicketOrder.objects.select_for_update(of=("self",)).get(pk=order.pk)
        if locked.status not in REFUNDABLE:
            raise ValidationError("Only paid or reconciling orders can be marked refunded.")
        locked.status = TicketOrder.Status.REFUNDED
        locked.pending_payload = {}
        locked.email_key = ""
        locked.save()
        still_paid = locked.registration_id and TicketOrder.objects.filter(
            registration_id=locked.registration_id, status=TicketOrder.Status.FULFILLED
        ).exclude(pk=locked.pk).exists()
        if locked.registration_id and not still_paid:
            reg = Registration.objects.select_for_update().get(pk=locked.registration_id)
            if reg.status == Registration.Status.REGISTERED:
                reg.status = Registration.Status.CANCELLED
                reg.save(update_fields=["status", "updated_at"])
                ReminderSchedule.objects.filter(registration=reg, status=ReminderSchedule.Status.PENDING).update(
                    status=ReminderSchedule.Status.SUPPRESSED, processed_at=timezone.now()
                )
                reg.occurrence = occ
                outbox.enqueue_event("registration.updated", registration=reg, occurrence=occ)
        audit("ticket.marked_refunded", user=user, api_key=api_key, obj=locked, source=source)
    return locked


def blocks_admission(registration):
    """True when this registration's ticket was refunded or flagged for refund.

    Only the latest order decides, and a fulfilled order always admits: an old refunded
    purchase never blocks a later paid re-purchase that reactivated the same registration."""
    orders = TicketOrder.objects.filter(registration=registration)
    if orders.filter(status=TicketOrder.Status.FULFILLED).exists():
        return False
    latest = orders.order_by("-created_at").first()
    return latest is not None and latest.status in (TicketOrder.Status.REFUND_REQUIRED, TicketOrder.Status.REFUNDED)


def serialize_order_admin(order):
    """Staff projection: no PII, no checkout URL, no status token."""
    return {
        "id": str(order.pk),
        "occurrenceId": str(order.occurrence_id),
        "status": order.status,
        "mode": order.mode,
        "amountMinor": order.amount_minor,
        "currency": order.currency,
        "display": format_amount(order.amount_minor, order.currency),
        "registrationId": str(order.registration_id) if order.registration_id else None,
        "hasCheckoutSession": bool(order.checkout_session_id),
        "createdAt": _iso(order.created_at),
        "holdExpiresAt": _iso(order.hold_expires_at),
        "paidAt": _iso(order.paid_at),
        "lastError": order.last_error,
    }


def order_counts(occ):
    from django.db.models import Count

    rows = TicketOrder.objects.filter(occurrence=occ).values("status").annotate(n=Count("id"))
    return {r["status"]: r["n"] for r in rows}


def update_event_price(event, *, price_minor, currency, user=None, api_key=None):
    p, c = clean_price(price_minor, currency)
    with transaction.atomic():
        locked = Event.objects.select_for_update().get(pk=event.pk)
        if (locked.ticket_price_minor, locked.ticket_currency) != (p, c):
            locked.ticket_price_minor, locked.ticket_currency = p, c
            locked.save(update_fields=["ticket_price_minor", "ticket_currency", "updated_at"])
            audit("event.price_updated", user=user, api_key=api_key, obj=locked, amount_minor=p, currency=c)
    event.ticket_price_minor, event.ticket_currency = p, c
    return event
