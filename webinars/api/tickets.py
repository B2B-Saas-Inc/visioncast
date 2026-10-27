"""Ticket HTTP endpoints. Contract: docs/tickets.md.

* ``stripe_webhook``: signature-authenticated only (no session, cookie or API key), so
  ``csrf_exempt``; the raw body is verified before it is parsed.
* ``order_status``: public, authorised only by the opaque order status token returned
  with ``payment_required`` (hash compared, expiring). Uniform 404 otherwise.
* ``tickets_return``: the Checkout success/cancel landing page. It reads nothing and
  confirms nothing; fulfillment happens only through the verified webhook.
* Management (``management._endpoint``, so MCP can ``invoke`` them): Stripe account
  (``payments_admin``, set-only secrets), per-session capacity (``content_write``) and
  prices (``payments_admin``), order list and staff bookkeeping (``payments_admin``).
"""
from django.http import HttpResponse
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_safe

from webinars import capacity, payments
from webinars.api import management
from webinars.api.auth import has_scope
from webinars.api.management import _Invalid, _endpoint, _parse
from webinars.models import ApiKey, Event, Occurrence, TicketOrder

from .views import _err, _json

S = ApiKey.Scope
PAYMENTS = (S.PAYMENTS_ADMIN,)
TICKETS_WRITE = (S.CONTENT_WRITE, S.PAYMENTS_ADMIN)
TICKETS_READ = (S.CONFIG_READ,)

ORDER_TOKEN_HEADER = "Webinar-Order-Token"

# ---------------------------------------------------------------------------
# Input schemas (single source for OpenAPI and MCP)
# ---------------------------------------------------------------------------

PAYMENT_ACCOUNT_FIELDS = {
    "mode": ("mode", "str"),
    "secretKey": ("secret_key", "str"),
    "webhookSecret": ("webhook_secret", "str"),
    "expectedAccountId": ("expected_account_id", "str"),
    "enabled": ("enabled", "bool"),
}
PAYMENT_ACCOUNT_INPUT_SCHEMA = management.input_schema(
    PAYMENT_ACCOUNT_FIELDS,
    extra={
        "mode": {"enum": ["test", "live"], "description": "Changing mode requires a secret key of the new mode."},
        "secretKey": {"writeOnly": True, "maxLength": 255, "description": "sk_test_/rk_test_ or sk_live_/rk_live_. Set-only; never returned."},
        "webhookSecret": {"writeOnly": True, "maxLength": 255, "description": "whsec_... Set-only; never returned."},
        "expectedAccountId": {"maxLength": 64, "description": "Optional acct_...; events from other accounts are rejected."},
    },
)

OCCURRENCE_TICKET_FIELDS = {
    "capacity": ("capacity", "int_or_null"),
    "showRemaining": ("show_remaining", "bool"),
    "priceMinor": ("ticket_price_minor", "int_or_null"),
    "currency": ("ticket_currency", "str"),
}
_PRICE_KEYS = {"priceMinor", "currency"}
_INT_OR_NULL = {"type": ["integer", "null"]}
OCCURRENCE_TICKETS_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "capacity": {**_INT_OR_NULL, "minimum": 1, "maximum": capacity.MAX_CAPACITY,
                     "description": "Hard seat limit; null = unlimited (default). Needs content_write."},
        "showRemaining": {"type": "boolean", "description": "Publish the remaining-seat count. Needs content_write."},
        "priceMinor": {**_INT_OR_NULL, "minimum": payments.MIN_AMOUNT_MINOR, "maximum": payments.MAX_AMOUNT_MINOR,
                       "description": "Session price override in minor units; null = use the event price. Needs payments_admin."},
        "currency": {"type": "string", "enum": [""] + sorted(payments.CURRENCIES), "description": "ISO 4217 lower case. Needs payments_admin."},
    },
}
EVENT_TICKETS_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["priceMinor", "currency"],
    "properties": {
        "priceMinor": {**_INT_OR_NULL, "minimum": payments.MIN_AMOUNT_MINOR, "maximum": payments.MAX_AMOUNT_MINOR,
                       "description": "Event ticket price in minor units; null = free."},
        "currency": {"type": "string", "enum": [""] + sorted(payments.CURRENCIES)},
    },
}
ORDER_ACTION_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["confirm"],
    "properties": {"confirm": {"const": True, "description": "Explicit acknowledgement; bookkeeping only, no money moves."}},
}


def _check(body, allowed):
    """``management._parse`` plus the int-or-null kind used here."""
    if not isinstance(body, dict):
        raise _Invalid(None, "Request body must be a JSON object.")
    errors = {}
    simple = {}
    for key, value in body.items():
        if key not in allowed:
            errors[key] = "Unknown field."
        elif allowed[key][1] == "int_or_null":
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                errors[key] = f"Invalid type for {key}."
        else:
            simple[key] = allowed[key]
    if errors:
        raise _Invalid(errors)
    _parse({k: body[k] for k in simple}, simple)
    return dict(body)


def _validation_fields(exc, mapping):
    reverse = {model: camel for camel, (model, _k) in mapping.items()}
    if hasattr(exc, "message_dict"):
        return {reverse.get(k, k): (v[0] if v else "Invalid value.") for k, v in exc.message_dict.items()}
    return {"_": "; ".join(exc.messages)}


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


@csrf_exempt
def stripe_webhook(request):
    if request.method != "POST":
        response = _err(405, "method_not_allowed", "Method not allowed.")
        response["Allow"] = "POST"
        return response
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared = 0
    if declared > payments.MAX_WEBHOOK_BYTES:
        return _json({"error": "too_large"}, status=413, no_store=True)
    status, body = payments.process_webhook(request.body, request.META.get("HTTP_STRIPE_SIGNATURE", ""))
    return _json(body, status=status, no_store=True)


@require_safe
def order_status(request, pk):
    # Header only: a query-string token would end up in access logs and Referer headers.
    token = request.headers.get(ORDER_TOKEN_HEADER, "")
    data = payments.order_status(pk, token)
    if data is None:
        return _err(404, "not_found", "Not found.")
    return _json(data, no_store=True)


_RETURN_TEXT = {
    "success": ("Thanks — we're confirming your payment",
                "Your seat is confirmed once Stripe tells us the payment succeeded. "
                "You'll get a confirmation shortly; you can close this page."),
    "cancelled": ("Checkout cancelled", "No payment was taken. Your held seat is released when the checkout expires."),
}


@require_safe
def tickets_return(request):
    title, text = _RETURN_TEXT.get(request.GET.get("result"), _RETURN_TEXT["success"])
    html = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
        "content=\"width=device-width,initial-scale=1\"><meta name=\"robots\" content=\"noindex\">"
        f"<title>{escape(title)}</title></head><body style=\"font-family:system-ui;max-width:36rem;margin:4rem auto;padding:0 1rem\">"
        f"<h1>{escape(title)}</h1><p>{escape(text)}</p></body></html>"
    )
    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


# ---------------------------------------------------------------------------
# Management
# ---------------------------------------------------------------------------


@_endpoint(PAYMENTS, ("GET", "PATCH"))
def payment_account(request, key, body):
    from django.core.exceptions import ValidationError

    if request.method == "GET":
        return 200, payments.serialize_account(payments.active_account() or _any_account())
    values = _parse(body, PAYMENT_ACCOUNT_FIELDS)
    try:
        account = payments.save_account(
            mode=values.get("mode"),
            secret_key=values.get("secretKey", ""),
            webhook_secret=values.get("webhookSecret", ""),
            expected_account_id=values.get("expectedAccountId"),
            enabled=values.get("enabled"),
            api_key=key,
        )
    except ValidationError as exc:
        raise _Invalid(_validation_fields(exc, PAYMENT_ACCOUNT_FIELDS))
    return 200, payments.serialize_account(account)


def _any_account():
    from webinars.models import PaymentAccount

    return PaymentAccount.objects.order_by("created_at").first()


def serialize_tickets(occ):
    price = payments.effective_price(occ)
    return {
        "occurrenceId": str(occ.pk),
        "capacity": occ.capacity,
        "showRemaining": occ.show_remaining,
        "remaining": capacity.remaining(occ),
        "priceMinor": occ.ticket_price_minor,
        "currency": occ.ticket_currency,
        "effectivePrice": price,
        "paymentsAvailable": payments.payments_available(payments.active_account()),
        "orderCounts": payments.order_counts(occ),
    }


@_endpoint(TICKETS_WRITE, ("GET", "PATCH"), read_scopes=TICKETS_WRITE + TICKETS_READ)
def occurrence_tickets(request, key, body, pk):
    from django.core.exceptions import ValidationError

    occ = Occurrence.objects.select_related("event").get(pk=pk)
    if request.method == "GET":
        return 200, serialize_tickets(occ)
    values = _check(body, OCCURRENCE_TICKET_FIELDS)
    if set(values) & _PRICE_KEYS and not has_scope(key, S.PAYMENTS_ADMIN):
        raise _Invalid(None, "Changing prices requires the payments_admin scope.", status=403, code="forbidden")
    if set(values) - _PRICE_KEYS and not has_scope(key, S.CONTENT_WRITE):
        raise _Invalid(None, "Changing capacity requires the content_write scope.", status=403, code="forbidden")
    kwargs = {}
    if "capacity" in values:
        kwargs["capacity"] = values["capacity"]
    if "showRemaining" in values:
        kwargs["show_remaining"] = values["showRemaining"]
    if "priceMinor" in values:
        kwargs["price_minor"] = values["priceMinor"]
    if "currency" in values:
        kwargs["currency"] = values["currency"]
    try:
        capacity.update_ticket_settings(occ, api_key=key, **kwargs)
    except ValidationError as exc:
        raise _Invalid(_validation_fields(exc, OCCURRENCE_TICKET_FIELDS))
    return 200, serialize_tickets(occ)


@_endpoint(PAYMENTS, ("GET", "PATCH"))
def event_tickets(request, key, body, pk):
    from django.core.exceptions import ValidationError

    event = Event.objects.get(pk=pk)
    if request.method == "PATCH":
        values = _check(body, {"priceMinor": ("ticket_price_minor", "int_or_null"), "currency": ("ticket_currency", "str")})
        if set(values) != _PRICE_KEYS:
            raise _Invalid({k: "This field is required." for k in _PRICE_KEYS - set(values)})
        try:
            payments.update_event_price(event, price_minor=values["priceMinor"], currency=values["currency"], api_key=key)
        except ValidationError as exc:
            raise _Invalid(_validation_fields(exc, {"priceMinor": ("ticket_price_minor", ""), "currency": ("ticket_currency", "")}))
    return 200, {"eventId": str(event.pk), "priceMinor": event.ticket_price_minor, "currency": event.ticket_currency,
                 "display": payments.format_amount(event.ticket_price_minor, event.ticket_currency)}


@_endpoint(PAYMENTS, ("GET",))
def occurrence_orders(request, key, body, pk):
    occ = Occurrence.objects.get(pk=pk)
    orders = TicketOrder.objects.filter(occurrence=occ).order_by("-created_at")[:500]
    return 200, {"occurrenceId": str(occ.pk), "counts": payments.order_counts(occ),
                 "results": [payments.serialize_order_admin(o) for o in orders]}


def _order_action(body, pk):
    values = _parse(body, {"confirm": ("confirm", "bool")}, required=("confirm",))
    if values["confirm"] is not True:
        raise _Invalid({"confirm": "Must be true."})
    order = TicketOrder.objects.filter(pk=pk).first()
    if order is None:
        raise _Invalid(None, "Not found.", status=404, code="not_found")
    return order


@_endpoint(PAYMENTS, ("POST",))
def order_mark_refunded(request, key, body, pk):
    from django.core.exceptions import ValidationError

    order = _order_action(body, pk)
    try:
        order = payments.mark_refunded(order, api_key=key)
    except ValidationError as exc:
        raise _Invalid(None, "; ".join(exc.messages), status=409, code="conflict")
    return 200, payments.serialize_order_admin(order)


@_endpoint(PAYMENTS, ("POST",))
def order_release(request, key, body, pk):
    from django.core.exceptions import ValidationError

    order = _order_action(body, pk)
    try:
        payments.release_unpaid(order, api_key=key)
    except ValidationError as exc:
        raise _Invalid(None, "; ".join(exc.messages), status=409, code="conflict")
    return 200, payments.serialize_order_admin(TicketOrder.objects.get(pk=order.pk))
