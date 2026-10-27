"""Regression tests for .runs/next-security-result.md F1: disabling the Stripe account
stops new sales only. Signed events for orders already taken (payment, refund, dispute)
are still verified and applied. Stripe is always mocked; nothing here touches the network.
"""
import json
import uuid

import pytest

from tests.conftest import make_api_key, payload, post_registration
from tests.test_tickets import SK, WHSEC, FakeStripe, _deliver, _event, _hold, _sign, verified_cookie


@pytest.fixture
def account(db):
    from webinars import payments

    return payments.save_account(mode="test", secret_key=SK, webhook_secret=WHSEC, enabled=True)


@pytest.fixture
def stripe(monkeypatch):
    from webinars import payments

    fake = FakeStripe()
    monkeypatch.setattr(payments, "stripe_request", fake)
    return fake


@pytest.fixture
def paid(occurrence):
    occurrence.ticket_price_minor = 2500
    occurrence.ticket_currency = "usd"
    occurrence.save()
    return occurrence


def _disable():
    from webinars import payments

    account = payments.save_account(enabled=False)
    assert account.enabled is False and payments.active_account() is None
    return account


def _charge_event(etype, intent, **obj):
    body = {"object": "dispute" if etype == "charge.dispute.created" else "charge", "payment_intent": intent,
            "livemode": False, **obj}
    return {"id": f"evt_{uuid.uuid4().hex}", "type": etype, "livemode": False, "data": {"object": body}}


def _fulfilled_order(client, paid):
    _resp, order = _hold(client, paid)
    assert _deliver(client, _event(order)).status_code == 200
    order.refresh_from_db()
    assert order.status == "fulfilled" and order.payment_intent_id == "pi_1"
    return order


def test_disabled_account_still_fulfils_open_checkout(client, paid, account, stripe):
    from webinars.models import Registration, StripeEventRecord

    _resp, order = _hold(client, paid)
    assert order.status == "checkout_open"
    _disable()
    event = _event(order)
    resp = _deliver(client, event)
    assert resp.status_code == 200, resp.content
    order.refresh_from_db()
    assert order.status == "fulfilled"
    assert Registration.objects.filter(occurrence=paid, status="registered").count() == 1
    assert StripeEventRecord.objects.get(event_id=event["id"]).outcome == "fulfilled"


def test_disabled_account_still_flags_dispute(client, paid, account, stripe):
    from webinars.models import StripeEventRecord

    order = _fulfilled_order(client, paid)
    _disable()
    event = _charge_event("charge.dispute.created", "pi_1")
    assert _deliver(client, event).status_code == 200
    order.refresh_from_db()
    assert order.status == "refund_required" and order.last_error == "dispute opened in Stripe"
    assert StripeEventRecord.objects.get(event_id=event["id"]).outcome == "disputed"


def test_disabled_account_still_records_refund(client, paid, account, stripe):
    from webinars.models import Registration

    order = _fulfilled_order(client, paid)
    _disable()
    assert _deliver(client, _charge_event("charge.refunded", "pi_1", refunded=True)).status_code == 200
    order.refresh_from_db()
    assert order.status == "refunded"
    assert Registration.objects.get(pk=order.registration_id).status == "cancelled"


def test_disabled_account_refuses_new_checkout(client, paid, account, stripe):
    from webinars.models import TicketOrder

    _disable()
    body = payload(paid)
    resp = post_registration(client, body, make_api_key(("proxy",)), cookie=verified_cookie(body["profile"]["email"]))
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "payments_unavailable"
    assert not TicketOrder.objects.exists() and stripe.calls == []


@pytest.mark.parametrize("sig", [
    "",
    "garbage",
    "t=1,v1=deadbeef",
    "wrong-secret",
])
def test_disabled_account_still_denies_bad_signature(client, paid, account, stripe, sig):
    from webinars.models import Registration, StripeEventRecord

    _resp, order = _hold(client, paid)
    _disable()
    event = _event(order)
    if sig == "wrong-secret":
        sig = _sign(json.dumps(event).encode(), secret="whsec_" + "c" * 24)
    resp = _deliver(client, event, sig=sig)
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_signature"
    order.refresh_from_db()
    assert order.status == "checkout_open"
    assert not StripeEventRecord.objects.exists() and not Registration.objects.exists()
