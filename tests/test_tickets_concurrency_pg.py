"""PostgreSQL-only concurrency tests for capacity and paid checkout (docs/tickets.md).

SQLite serialises writers and ignores SELECT ... FOR UPDATE, so these are skipped
unless the backend is PostgreSQL. Stripe is mocked (``payments.stripe_request``).
"""
import json
import uuid

import pytest
from django.db import connection
from django.test import Client

from tests.conftest import make_api_key, make_event, make_form, make_occurrence, payload
from tests.test_concurrency_pg import _run_parallel
from tests.conftest import COOKIE
from tests.test_tickets import SK, WHSEC, FakeStripe, _event, _sign, verified_cookie
from webinars.models import Registration, StripeEventRecord, TicketOrder

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL"),
]


def _post(key, body, cookie=None):
    """Paid bodies get a mailbox-verified browser for their email (required for a hold);
    pass ``cookie=False`` for an anonymous browser."""
    client = Client()
    if cookie is None and body.get("_paid"):
        cookie = body["_paid"]
    if cookie:
        client.cookies[COOKIE] = cookie
    body = {k: v for k, v in body.items() if k != "_paid"}
    resp = client.post("/api/v1/registrations", data=json.dumps(body), content_type="application/json",
                       headers={"Authorization": f"Bearer {key}"})
    return resp.status_code, resp.json()


def _paid_payload(occ, i):
    body = _email_payload(occ, i)
    body["_paid"] = verified_cookie(body["profile"]["email"])  # created before any thread starts
    return body


def _email_payload(occ, i):
    return payload(occ, profile={"email": f"buyer{i}-{uuid.uuid4().hex[:6]}@example.com", "firstName": "B"})


def _paid_occurrence(capacity=1):
    from webinars import payments

    occ = make_occurrence(make_event(make_form(), slug=f"paid-{uuid.uuid4().hex[:6]}"), capacity=capacity,
                          ticket_price_minor=2500, ticket_currency="usd")
    payments.save_account(mode="test", secret_key=SK, webhook_secret=WHSEC, enabled=True)
    return occ


def test_free_capacity_one_admits_exactly_one():
    occ = make_occurrence(make_event(make_form(), slug="cap-free"), capacity=1)
    key = make_api_key(("proxy",))
    results = _run_parallel(lambda i: _post(key, _email_payload(occ, i)))
    statuses = sorted(s for s, _ in results)
    assert statuses.count(201) == 1, results
    assert all(s == 409 for s in statuses if s != 201)
    assert all(b["error"]["code"] == "sold_out" for s, b in results if s == 409)
    assert Registration.objects.filter(occurrence=occ, status="registered").count() == 1


def test_paid_capacity_one_holds_exactly_one_seat(monkeypatch):
    from webinars import payments

    monkeypatch.setattr(payments, "stripe_request", FakeStripe())
    occ = _paid_occurrence()
    key = make_api_key(("proxy",))
    results = _run_parallel(lambda i: _post(key, _paid_payload(occ, i)))
    statuses = [s for s, _ in results]
    assert statuses.count(202) == 1, results
    assert all(s == 409 for s in statuses if s != 202)
    assert TicketOrder.objects.filter(occurrence=occ, status__in=TicketOrder.HOLDING).count() == 1
    assert not Registration.objects.exists()


def test_paid_and_free_paths_share_one_seat(monkeypatch):
    """A held checkout counts against capacity for a concurrent free registration too
    (the session is paid, so every path is the paid path; the hold blocks the rest)."""
    from webinars import payments

    monkeypatch.setattr(payments, "stripe_request", FakeStripe())
    occ = _paid_occurrence()
    key = make_api_key(("proxy",))
    assert _post(key, _paid_payload(occ, 0))[0] == 202
    occ.ticket_price_minor = None
    occ.ticket_currency = ""
    occ.save()  # now free: the open hold still occupies the only seat
    results = _run_parallel(lambda i: _post(key, _email_payload(occ, i + 1)), n=4)
    assert all(s == 409 for s, _ in results), results
    assert not Registration.objects.exists()


def _deliver(event):
    raw = json.dumps(event).encode()
    resp = Client().post("/api/v1/payments/stripe/webhook", data=raw, content_type="application/json",
                         HTTP_STRIPE_SIGNATURE=_sign(raw))
    return resp.status_code


def test_concurrent_completed_webhooks_fulfil_once(monkeypatch):
    from webinars import payments

    monkeypatch.setattr(payments, "stripe_request", FakeStripe())
    occ = _paid_occurrence()
    key = make_api_key(("proxy",))
    status, body = _post(key, _paid_payload(occ, 0))
    assert status == 202
    order = TicketOrder.objects.get(pk=body["order"]["id"])
    events = [_event(order) for _ in range(6)]  # different event ids, same session
    results = _run_parallel(lambda i: _deliver(events[i]))
    assert all(s == 200 for s in results), results
    order.refresh_from_db()
    assert order.status == "fulfilled"
    assert Registration.objects.filter(occurrence=occ).count() == 1
    assert StripeEventRecord.objects.count() == 6


def test_same_event_id_delivered_concurrently_recorded_once(monkeypatch):
    from webinars import payments

    monkeypatch.setattr(payments, "stripe_request", FakeStripe())
    occ = _paid_occurrence()
    key = make_api_key(("proxy",))
    _status, body = _post(key, _paid_payload(occ, 0))
    order = TicketOrder.objects.get(pk=body["order"]["id"])
    event = _event(order)
    results = _run_parallel(lambda i: _deliver(event))
    assert all(s == 200 for s in results), results
    assert StripeEventRecord.objects.filter(event_id=event["id"]).count() == 1
    assert Registration.objects.count() == 1


def test_same_key_concurrent_retries_one_order_one_seat(monkeypatch):
    from webinars import payments

    monkeypatch.setattr(payments, "stripe_request", FakeStripe())
    occ = _paid_occurrence(capacity=3)
    key = make_api_key(("proxy",))
    body = _paid_payload(occ, 0)  # one verified browser retrying the same request
    results = _run_parallel(lambda i: _post(key, body))
    assert all(s == 202 for s, _ in results), results
    assert len({b["order"]["id"] for _, b in results}) == 1
    assert TicketOrder.objects.filter(occurrence=occ).count() == 1


def test_concurrent_unverified_callers_hold_nothing(monkeypatch):
    """R1 under concurrency: anonymous proxy callers never hold a seat."""
    from webinars import payments

    fake = FakeStripe()
    monkeypatch.setattr(payments, "stripe_request", fake)
    occ = _paid_occurrence(capacity=2)
    key = make_api_key(("proxy",))
    results = _run_parallel(lambda i: _post(key, _email_payload(occ, i), cookie=False))
    assert all(s == 202 and b["status"] == "verification_required" for s, b in results), results
    assert not TicketOrder.objects.exists() and not fake.calls
