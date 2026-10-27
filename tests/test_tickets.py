"""Capacity and paid tickets (docs/tickets.md). Stripe is always mocked: no network."""
import hashlib
import hmac
import json
import time
import uuid

import pytest

from tests.conftest import make_api_key, make_occurrence, make_registration, payload, post_registration, register_with_cookie

SK = "sk_test_" + "a" * 24
WHSEC = "whsec_" + "b" * 24


@pytest.fixture
def account(db):
    from webinars import payments

    return payments.save_account(mode="test", secret_key=SK, webhook_secret=WHSEC, enabled=True)


@pytest.fixture
def paid(occurrence):
    occurrence.ticket_price_minor = 2500
    occurrence.ticket_currency = "usd"
    occurrence.save()
    return occurrence


class FakeStripe:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, method, path, *, secret, form=None, idempotency_key=None):
        self.calls.append({"method": method, "path": path, "secret": secret, "form": form, "idem": idempotency_key})
        if self.responses:
            return self.responses.pop(0)
        # Like Stripe: one session per Idempotency-Key (so per order).
        sid = "cs_test_" + hashlib.sha256((idempotency_key or "").encode()).hexdigest()[:16]
        return "ok", {"id": sid, "url": f"https://checkout.stripe.com/c/pay/{sid}", "amount_total": 2500,
                      "currency": "usd", "expires_at": int(time.time()) + 1900}


@pytest.fixture
def stripe(monkeypatch):
    from webinars import payments

    fake = FakeStripe()
    monkeypatch.setattr(payments, "stripe_request", fake)
    return fake


def _sign(raw, secret=WHSEC, ts=None):
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _event(order, etype="checkout.session.completed", **obj_overrides):
    obj = {"object": "checkout.session", "id": order.checkout_session_id or "cs_test_1", "client_reference_id": str(order.pk),
           "metadata": {"order_id": str(order.pk)}, "mode": "payment", "livemode": False, "payment_status": "paid",
           "amount_total": order.amount_minor, "currency": order.currency, "payment_intent": "pi_1"}
    obj.update(obj_overrides)
    return {"id": f"evt_{uuid.uuid4().hex}", "type": etype, "livemode": False, "data": {"object": obj}}


def _deliver(client, event, sig=None):
    raw = json.dumps(event).encode()
    return client.post("/api/v1/payments/stripe/webhook", data=raw, content_type="application/json",
                       HTTP_STRIPE_SIGNATURE=sig if sig is not None else _sign(raw))


def verified_cookie(email=None):
    """Raw capability token for a browser whose mailbox for ``email`` was proven (the
    waiting-room code/link path). Paid holds require one (docs/tickets.md)."""
    from webinars import identity
    from webinars.models import Person
    from webinars.normalization import email_key

    email = email or payload(None)["profile"]["email"]
    person = Person.objects.filter(email_key=email_key(email)).first()
    if person is None:
        person = Person.objects.create(email=email, email_key=email_key(email))
    return identity.issue_capability(person, mailbox_verified=True)


def verified_capability(email=None):
    from webinars.models import BrowserCapability

    token = verified_cookie(email)
    return BrowserCapability.objects.select_related("person").get(token_hash=hashlib.sha256(token.encode()).hexdigest())


def _hold(client, occ, key=None, cookie=None, body=None):
    from webinars.models import TicketOrder

    key = key or make_api_key(("proxy",))
    body = body or payload(occ)
    cookie = cookie or verified_cookie(body["profile"]["email"])
    resp = post_registration(client, body, key, cookie=cookie)
    assert resp.status_code == 202, resp.content
    return resp, TicketOrder.objects.get(pk=resp.json()["order"]["id"])


# ---------------------------------------------------------------------------
# Free path and capacity
# ---------------------------------------------------------------------------


def test_free_unlimited_path_unchanged(client, occurrence, proxy_key):
    from webinars.models import TicketOrder

    resp = post_registration(client, payload(occurrence), proxy_key)
    assert resp.status_code == 201 and resp.json()["status"] == "registered"
    assert not TicketOrder.objects.exists()
    detail = client.get(f"/api/v1/events/{occurrence.event.slug}").json()["occurrences"][0]
    assert "availability" not in detail and "ticket" not in detail


def test_capacity_full_is_sold_out_but_existing_registrant_ok(client, occurrence, proxy_key):
    occurrence.capacity = 1
    occurrence.save()
    _resp, token = register_with_cookie(client, occurrence, proxy_key)
    other = payload(occurrence, profile={"email": "someone.else@example.com", "firstName": "Bo"})
    resp = post_registration(client, other, proxy_key)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "sold_out"
    again = post_registration(client, payload(occurrence), proxy_key, cookie=token)
    assert again.status_code == 200


def test_remaining_only_when_opted_in(client, occurrence, proxy_key):
    occurrence.capacity = 5
    occurrence.save()
    make_registration(occurrence)
    data = client.get(f"/api/v1/events/{occurrence.event.slug}").json()["occurrences"][0]["availability"]
    assert data == {"remaining": None, "soldOut": False}
    occurrence.show_remaining = True
    occurrence.save()
    data = client.get(f"/api/v1/events/{occurrence.event.slug}").json()["occurrences"][0]["availability"]
    assert data == {"remaining": 4, "soldOut": False}


# ---------------------------------------------------------------------------
# Paid holds and checkout
# ---------------------------------------------------------------------------


def test_paid_without_account_is_unavailable_not_fake(client, paid, proxy_key):
    from webinars.models import Registration, TicketOrder

    resp = post_registration(client, payload(paid), proxy_key)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "payments_unavailable"
    assert not Registration.objects.exists() and not TicketOrder.objects.exists()


def test_paid_registration_returns_checkout_not_registration(client, paid, account, stripe):
    from webinars.models import OutboxMessage, Registration

    resp, order = _hold(client, paid)
    body = resp.json()
    assert body["status"] == "payment_required" and "calendar" not in body
    assert body["order"]["checkoutUrl"].startswith("https://checkout.stripe.com/")
    assert body["order"]["statusToken"]
    assert not Registration.objects.exists() and not OutboxMessage.objects.exists()
    call = stripe.calls[0]
    assert call["path"] == "/v1/checkout/sessions" and call["idem"] == f"order-{order.pk}"
    form = dict(call["form"])
    assert form["payment_method_types[0]"] == "card" and form["line_items[0][price_data][unit_amount]"] == "2500"
    assert form["client_reference_id"] == str(order.pk) and order.status == "checkout_open"


def test_replay_returns_same_order_without_new_checkout(client, paid, account, stripe):
    key = make_api_key(("proxy",))
    body = payload(paid)
    cookie = verified_cookie()
    first = post_registration(client, body, key, cookie=cookie)
    second = post_registration(client, body, key, cookie=cookie)
    assert first.json()["order"]["id"] == second.json()["order"]["id"]
    assert len(stripe.calls) == 1


def test_unknown_create_keeps_seat_for_reconciliation(client, paid, account, monkeypatch):
    from webinars import capacity, payments

    fake = FakeStripe([("unknown", {"error": "TimeoutError"}), ("unknown", {"error": "TimeoutError"})])
    monkeypatch.setattr(payments, "stripe_request", fake)
    paid.capacity = 1
    paid.save()
    resp, order = _hold(client, paid)
    assert order.status == "needs_reconciliation" and resp.json()["order"]["checkoutUrl"] is None
    assert [c["idem"] for c in fake.calls] == [f"order-{order.pk}"] * 2  # retried with the same key only
    assert capacity.remaining(paid) == 0


def test_definitive_create_failure_releases_seat(client, paid, account, monkeypatch):
    from webinars import payments

    monkeypatch.setattr(payments, "stripe_request", FakeStripe([("failed", {"error": "http 400"})]))
    resp = post_registration(client, payload(paid), make_api_key(("proxy",)), cookie=verified_cookie())
    # 409 (not 5xx) so the Node proxy passes the code through and the SDK rotates its key.
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "checkout_failed"
    from webinars.models import TicketOrder

    order = TicketOrder.objects.get()
    assert order.status == "cancelled" and order.email_key == "" and order.pending_payload == {}


def test_price_change_does_not_change_held_order(client, paid, account, stripe):
    _resp, order = _hold(client, paid)
    paid.ticket_price_minor = 9900
    paid.save()
    order.refresh_from_db()
    assert order.amount_minor == 2500 and order.price_snapshot["amountMinor"] == 2500


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def test_verified_webhook_fulfils_once(client, paid, account, stripe):
    from webinars.models import OutboxMessage, Registration

    _resp, order = _hold(client, paid)
    event = _event(order)
    assert _deliver(client, event).status_code == 200
    order.refresh_from_db()
    assert order.status == "fulfilled" and order.pending_payload == {}
    assert Registration.objects.filter(occurrence=paid, status="registered").count() == 1
    dup = _deliver(client, event)
    assert dup.status_code == 200 and dup.json().get("duplicate") is True
    other = _event(order)  # new event id, same session: idempotent by order state
    assert _deliver(client, other).status_code == 200
    assert Registration.objects.count() == 1
    assert OutboxMessage.objects.filter(event_type="registration.created").count() <= 1


@pytest.mark.parametrize("sig", ["", "t=1,v1=deadbeef", "garbage"])
def test_bad_signature_rejected(client, paid, account, stripe, sig):
    _resp, order = _hold(client, paid)
    assert _deliver(client, _event(order), sig=sig).status_code == 400


def test_stale_signature_rejected(client, paid, account, stripe):
    _resp, order = _hold(client, paid)
    raw = json.dumps(_event(order)).encode()
    resp = client.post("/api/v1/payments/stripe/webhook", data=raw, content_type="application/json",
                       HTTP_STRIPE_SIGNATURE=_sign(raw, ts=int(time.time()) - 3600))
    assert resp.status_code == 400


@pytest.mark.parametrize("override", [
    {"amount_total": 1}, {"currency": "eur"}, {"id": "cs_other"}, {"metadata": {"order_id": str(uuid.uuid4())}},
    {"client_reference_id": "x"}, {"livemode": True},
])
def test_mismatched_session_never_registers(client, paid, account, stripe, override):
    from webinars.models import Registration

    _resp, order = _hold(client, paid)
    event = _event(order, **override)
    _deliver(client, event)
    assert not Registration.objects.exists()


def test_unpaid_status_does_not_fulfil(client, paid, account, stripe):
    from webinars.models import Registration

    _resp, order = _hold(client, paid)
    _deliver(client, _event(order, payment_status="unpaid"))
    assert not Registration.objects.exists()


def test_payment_after_release_is_refund_required(client, paid, account, stripe):
    from webinars.models import Registration

    _resp, order = _hold(client, paid)
    _deliver(client, _event(order, etype="checkout.session.expired", payment_status="unpaid"))
    order.refresh_from_db()
    assert order.status == "expired"
    _deliver(client, _event(order))
    order.refresh_from_db()
    assert order.status == "refund_required" and not Registration.objects.exists()


def test_success_redirect_does_not_register(client, paid, account, stripe):
    from webinars.models import Registration

    _resp, order = _hold(client, paid)
    page = client.get(f"/tickets/return?order={order.pk}&result=success")
    assert page.status_code == 200 and not Registration.objects.exists()


def test_order_status_requires_token(client, paid, account, stripe):
    resp, order = _hold(client, paid)
    token = resp.json()["order"]["statusToken"]
    assert client.get(f"/api/v1/orders/{order.pk}").status_code == 404
    assert client.get(f"/api/v1/orders/{order.pk}", headers={"Webinar-Order-Token": "x" * 40}).status_code == 404
    ok = client.get(f"/api/v1/orders/{order.pk}", headers={"Webinar-Order-Token": token})
    assert ok.status_code == 200 and ok.json()["registered"] is False


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def _expire(order):
    from datetime import timedelta

    from django.utils import timezone

    from webinars.models import TicketOrder

    TicketOrder.objects.filter(pk=order.pk).update(hold_expires_at=timezone.now() - timedelta(minutes=1),
                                                   updated_at=timezone.now() - timedelta(hours=1))


def test_sweep_releases_only_when_stripe_says_expired(client, paid, account, stripe):
    from webinars import payments

    _resp, order = _hold(client, paid)
    _expire(order)
    stripe.responses = [("unknown", {"error": "TimeoutError"})]
    payments.sweep()
    order.refresh_from_db()
    assert order.status == "needs_reconciliation"  # seat kept
    _expire(order)
    stripe.responses = [("ok", {"id": order.checkout_session_id, "status": "expired", "payment_status": "unpaid"})]
    payments.sweep()
    order.refresh_from_db()
    assert order.status == "expired"


def test_sweep_keeps_paid_session_awaiting_webhook(client, paid, account, stripe):
    from webinars import payments

    _resp, order = _hold(client, paid)
    _expire(order)
    stripe.responses = [("ok", {"id": order.checkout_session_id, "status": "complete", "payment_status": "paid"})]
    payments.sweep()
    order.refresh_from_db()
    assert order.status == "needs_reconciliation" and order.registration_id is None


# ---------------------------------------------------------------------------
# Configuration and scopes
# ---------------------------------------------------------------------------


def test_live_key_rejected_in_test_mode(db):
    from django.core.exceptions import ValidationError

    from webinars import payments

    with pytest.raises(ValidationError):
        payments.save_account(mode="test", secret_key="sk_live_" + "a" * 24)


def test_secrets_never_returned(client, account):
    key = make_api_key(("payments_admin",))
    resp = client.get("/api/v1/manage/payments/account", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    text = resp.content.decode()
    assert SK not in text and WHSEC not in text and resp.json()["secretKeySet"] is True


def test_price_change_needs_payments_admin(client, occurrence):
    content = make_api_key(("content_write",))
    url = f"/api/v1/manage/occurrences/{occurrence.pk}/tickets"
    resp = client.patch(url, data=json.dumps({"priceMinor": 1000, "currency": "usd"}), content_type="application/json",
                        headers={"Authorization": f"Bearer {content}"})
    assert resp.status_code == 403
    ok = client.patch(url, data=json.dumps({"capacity": 10}), content_type="application/json",
                      headers={"Authorization": f"Bearer {content}"})
    assert ok.status_code == 200 and ok.json()["capacity"] == 10


def test_refund_flag_blocks_admission(db, occurrence):
    from webinars import payments
    from webinars.models import TicketOrder

    reg = make_registration(occurrence)
    TicketOrder.objects.create(
        occurrence=occurrence, status="refund_required", mode="test", amount_minor=2500, currency="usd",
        idempotency_key=f"k-{uuid.uuid4().hex}", request_hash="x", capability_hash=uuid.uuid4().hex,
        capability_expires_at=occurrence.starts_at, hold_expires_at=occurrence.starts_at, registration=reg,
    )
    assert payments.blocks_admission(reg)


def test_second_session_unaffected_by_full_first(client, event, proxy_key):
    first = make_occurrence(event, days=5, capacity=1)
    second = make_occurrence(event, days=6)
    make_registration(first)
    assert post_registration(client, payload(second), proxy_key).status_code == 201
