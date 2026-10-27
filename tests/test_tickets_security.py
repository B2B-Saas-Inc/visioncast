"""Regression tests for .runs/expansion-review-result.md findings (tickets, hosting).

Stripe and provider HTTP are always mocked; nothing here touches the network.
"""
import json
import uuid
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from tests.conftest import (
    DEFAULT_EMAIL,
    cookie_value,
    make_api_key,
    make_occurrence,
    make_person,
    make_registration,
    payload,
    post_registration,
)
from tests.test_tickets import SK, WHSEC, FakeStripe, _deliver, _event, _hold, verified_capability


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


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _order(occ, **kw):
    from webinars.models import TicketOrder

    defaults = dict(occurrence=occ, status="fulfilled", mode="test", amount_minor=2500, currency="usd",
                    idempotency_key=f"k-{uuid.uuid4().hex}", request_hash="x", capability_hash=uuid.uuid4().hex,
                    capability_expires_at=occ.starts_at, hold_expires_at=occ.starts_at)
    defaults.update(kw)
    return TicketOrder.objects.create(**defaults)


# ---------------------------------------------------------------------------
# P0-1: an ordinary event edit never changes the price
# ---------------------------------------------------------------------------


def test_event_edit_keeps_price(client, event):
    from webinars import payments
    from webinars.models import Event

    payments.update_event_price(event, price_minor=2500, currency="usd")
    key = make_api_key(("content_write",))
    resp = client.patch(f"/api/v1/manage/events/{event.pk}", data=json.dumps({"summary": "Edited summary"}),
                        content_type="application/json", headers=_auth(key))
    assert resp.status_code == 200, resp.content
    event = Event.objects.get(pk=event.pk)
    assert event.summary == "Edited summary"
    assert (event.ticket_price_minor, event.ticket_currency) == (2500, "usd")


def test_event_form_has_no_price_fields():
    from webinars.admin import AdminEventForm, EventForm

    for form in (EventForm, AdminEventForm):
        assert "ticket_price_minor" not in form.base_fields and "ticket_currency" not in form.base_fields


def test_event_with_orders_cannot_be_deleted(client, form):
    from tests.conftest import make_event

    draft = make_event(form, slug="draft-with-orders", visibility="draft")
    _order(make_occurrence(draft), status="expired")
    key = make_api_key(("content_write",))
    resp = client.delete(f"/api/v1/manage/events/{draft.pk}", headers=_auth(key))
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# P1-1: hold exhaustion
# ---------------------------------------------------------------------------


def test_paid_holds_rate_limited_per_client(settings, paid, account, stripe):
    from webinars.registration import register

    settings.RATE_LIMIT_ENABLED = True
    statuses = []
    for i in range(7):
        email = f"x{i}@example.com"
        body = payload(paid, profile={"email": email, "firstName": "X"})
        statuses.append(register(payload=body, capability=verified_capability(email), api_key=None,
                                 rate_key=("paid_hold", "ip:203.0.113.9")).status_code)
    assert statuses[:5] == [202] * 5 and statuses[5:] == [429, 429]


def _verification_body(resp):
    body = resp.json()
    assert resp.status_code == 202 and body["status"] == "verification_required", body
    assert "order" not in body and "checkoutUrl" not in json.dumps(body)
    return body


def test_r1_unverified_proxy_callers_cannot_hold_seats(client, paid, account, stripe):
    """R1: 50 random-email POSTs through one proxy key never hold a seat or call Stripe."""
    from webinars import capacity
    from webinars.models import TicketOrder

    paid.capacity = 50
    paid.save()
    key = make_api_key(("proxy",))
    for i in range(50):
        _verification_body(post_registration(client, payload(paid, profile={"email": f"r{i}@example.com", "firstName": "R"}), key))
    assert not TicketOrder.objects.exists() and not stripe.calls
    assert capacity.remaining(paid) == 50
    # A forged forwarding header does not help either.
    _verification_body(post_registration(client, payload(paid), key, **{"X-Forwarded-For": "198.51.100.7"}))
    assert not TicketOrder.objects.exists()


def test_r1_unverified_cookie_cannot_hold(client, paid, account, stripe):
    """A capability from an unverified free registration is recognition, not mailbox proof."""
    from webinars import identity
    from webinars.models import TicketOrder

    token = identity.issue_capability(make_person(email=DEFAULT_EMAIL))
    _verification_body(post_registration(client, payload(paid), make_api_key(("proxy",)), cookie=token))
    assert not TicketOrder.objects.exists() and not stripe.calls


def test_r2_paid_response_is_uniform_for_unrecognised_browsers(client, paid, account, stripe):
    """R2: registered email, open-checkout email and unknown email all get the same answer."""
    from webinars.models import TicketOrder

    key = make_api_key(("proxy",))
    make_registration(paid, person=make_person(email="taken@example.com", first_name="T"))
    _hold(client, paid, key=key, body=payload(paid, profile={"email": "buying@example.com", "firstName": "B"}))
    stripe_calls = len(stripe.calls)
    bodies = []
    for email in ("taken@example.com", "buying@example.com", "nobody@example.com"):
        resp = post_registration(client, payload(paid, profile={"email": email, "firstName": "X"}), key)
        body = _verification_body(resp)
        body.pop("occurrenceId")
        bodies.append(body)
        assert cookie_value(resp)  # decoy Set-Cookie for every cookie-less caller
    assert bodies[0] == bodies[1] == bodies[2]
    assert TicketOrder.objects.count() == 1 and len(stripe.calls) == stripe_calls


def test_r2_verified_victim_never_adopts_stranger_order(client, paid, account, stripe):
    """A hold not made by a mailbox-verified browser of the victim is never handed over,
    so its (attacker-chosen) envelope is never fulfilled for the victim."""
    from webinars.models import TicketOrder
    from webinars.normalization import email_key
    from webinars.registration import register

    stranger = _order(paid, status="checkout_open", email_key=email_key(DEFAULT_EMAIL),
                      caller_capability_id=None, pending_payload={"envelope": payload(paid, profile={"firstName": "Evil"})},
                      checkout_url="https://checkout.stripe.com/c/pay/cs_attacker", checkout_session_id="cs_attacker")
    victim = verified_capability(DEFAULT_EMAIL)
    resp = register(payload=payload(paid), capability=victim, api_key=None)
    assert resp.status_code == 409 and resp.body["error"]["code"] == "checkout_in_progress"
    assert "cs_attacker" not in json.dumps(resp.body)
    assert TicketOrder.objects.get(pk=stranger.pk).caller_capability_id is None


def test_same_browser_retry_gets_same_order(client, paid, account, stripe):
    from webinars.models import TicketOrder
    from webinars.registration import register

    cap = verified_capability(DEFAULT_EMAIL)
    a = register(payload=payload(paid), capability=cap, api_key=None)
    b = register(payload=payload(paid), capability=cap, api_key=None)  # fresh idempotency key
    assert a.status_code == b.status_code == 202
    assert a.body["order"]["id"] == b.body["order"]["id"]
    assert TicketOrder.objects.count() == 1


def test_same_key_retry_after_commit_returns_same_order(client, paid, account, stripe):
    """Serial version of the PG concurrency case: a retry that only sees the committed
    order through ``open_hold_for`` (not the idempotency lookup) gets it back, not 409."""
    from webinars.models import TicketOrder
    from webinars.registration import register

    cap = verified_capability(DEFAULT_EMAIL)
    body = payload(paid)
    a = register(payload=body, capability=cap, api_key=None)
    b = register(payload=body, capability=cap, api_key=None)
    assert a.status_code == b.status_code == 202 and a.body["order"]["id"] == b.body["order"]["id"]
    assert TicketOrder.objects.count() == 1


def test_verified_existing_registrant_is_not_charged(client, paid, account, stripe):
    from webinars.models import TicketOrder
    from webinars.registration import register

    cap = verified_capability("taken@example.com")
    make_registration(paid, person=cap.person)
    resp = register(payload=payload(paid, profile={"email": "taken@example.com", "firstName": "T"}), capability=cap, api_key=None)
    assert resp.status_code == 200
    assert not TicketOrder.objects.exists() and not stripe.calls


def test_replay_from_another_browser_is_refused(paid, account, stripe):
    from webinars.registration import register

    body = payload(paid)
    assert register(payload=body, capability=verified_capability(DEFAULT_EMAIL), api_key=None).status_code == 202
    other = verified_capability("other@example.com")
    replay = register(payload=body, capability=other, api_key=None)
    assert replay.status_code == 409 and "order" not in replay.body


def test_verification_unavailable_without_room_or_mail(client, paid, account, stripe, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    body = _verification_body(post_registration(client, payload(paid), make_api_key(("proxy",))))
    assert body["verification"]["available"] is False and body["verification"]["verifyUrl"] is None


# ---------------------------------------------------------------------------
# P1-2: a concurrent same-key retry never discards a created session
# ---------------------------------------------------------------------------


def test_stripe_409_keeps_hold_and_later_ok_applies(client, paid, account, monkeypatch):
    from webinars import payments
    from webinars.models import TicketOrder

    monkeypatch.setattr(payments, "stripe_request", FakeStripe([("unknown", {"error": "http 409"})] * 2))
    resp, order = _hold(client, paid)
    assert order.status == "hold" and resp.json()["order"]["checkoutUrl"] is None
    TicketOrder.objects.filter(pk=order.pk).update(status="needs_reconciliation")
    locked = TicketOrder.objects.get(pk=order.pk)
    ok = {"id": "cs_test_9", "url": "https://checkout.stripe.com/c/pay/cs_test_9", "amount_total": 2500, "currency": "usd"}
    assert payments._apply_create_outcome(locked, "ok", ok)
    locked.refresh_from_db()
    assert locked.status == "checkout_open" and locked.checkout_session_id == "cs_test_9"


def test_sweep_replays_sent_create_before_hold_expiry(client, paid, account, monkeypatch):
    from webinars import payments
    from webinars.models import TicketOrder

    fake = FakeStripe([("unknown", {"error": "http 409"})] * 2)
    monkeypatch.setattr(payments, "stripe_request", fake)
    _resp, order = _hold(client, paid)
    TicketOrder.objects.filter(pk=order.pk).update(updated_at=timezone.now() - timedelta(minutes=5))
    payments.sweep()  # FakeStripe now answers ok with the same key
    order.refresh_from_db()
    assert order.status == "checkout_open"
    assert {c["idem"] for c in fake.calls} == {f"order-{order.pk}"}


def test_created_session_with_wrong_livemode_not_applied(occurrence):
    from webinars import payments

    order = _order(occurrence, status="hold")
    data = {"id": "cs_live_1", "url": "https://checkout.stripe.com/c/pay/cs_live_1", "livemode": True}
    assert payments._apply_session(order, data) is False
    assert not payments.valid_session_id("cs_../../v1/x")


# ---------------------------------------------------------------------------
# P1-3: paid orders are refundable and never released as unpaid
# ---------------------------------------------------------------------------


def test_paid_mismatch_is_refund_required(client, paid, account, stripe):
    from webinars.models import Registration

    _resp, order = _hold(client, paid)
    _deliver(client, _event(order, amount_total=1))
    order.refresh_from_db()
    assert order.status == "refund_required" and order.paid_at is not None
    assert not Registration.objects.exists()


def test_release_refused_when_paid_seen_and_refund_recordable(occurrence, account, stripe):
    from webinars import payments

    order = _order(occurrence, status="needs_reconciliation", paid_at=timezone.now(), account=account)
    with pytest.raises(ValidationError):
        payments.release_unpaid(order)
    assert payments.mark_refunded(order).status == "refunded"


def test_release_rechecks_stripe(occurrence, account, monkeypatch):
    from webinars import payments

    order = _order(occurrence, status="needs_reconciliation", account=account, checkout_session_id="cs_test_x")
    monkeypatch.setattr(payments, "stripe_request", FakeStripe([("ok", {"id": "cs_test_x", "status": "complete", "payment_status": "paid"})]))
    with pytest.raises(ValidationError):
        payments.release_unpaid(order)
    order.refresh_from_db()
    assert order.status == "needs_reconciliation" and order.paid_at is not None
    monkeypatch.setattr(payments, "stripe_request", FakeStripe([("ok", {"id": "cs_test_x", "status": "expired", "payment_status": "unpaid"})]))
    order.paid_at = None
    order.save()
    assert payments.release_unpaid(order)


# ---------------------------------------------------------------------------
# P1-4: an old refund never blocks a later paid re-purchase
# ---------------------------------------------------------------------------


def test_refunded_then_repurchased_is_admitted(occurrence):
    from webinars import payments

    reg = make_registration(occurrence)
    _order(occurrence, status="refunded", registration=reg)
    assert payments.blocks_admission(reg)
    _order(occurrence, status="fulfilled", registration=reg)
    assert not payments.blocks_admission(reg)


def test_refunding_one_order_keeps_registration_paid_by_another(occurrence):
    from webinars import payments

    reg = make_registration(occurrence)
    first = _order(occurrence, status="fulfilled", registration=reg)
    _order(occurrence, status="fulfilled", registration=reg)
    payments.mark_refunded(first)
    reg.refresh_from_db()
    assert reg.status == "registered"


# ---------------------------------------------------------------------------
# P2: account mutation, erasure, refunds/disputes from Stripe, cancellation
# ---------------------------------------------------------------------------


def test_mode_or_key_change_refused_with_open_orders(client, paid, account, stripe):
    from webinars import payments

    _hold(client, paid)
    with pytest.raises(ValidationError):
        payments.save_account(mode="live", secret_key="sk_live_" + "c" * 24)
    with pytest.raises(ValidationError):
        payments.save_account(secret_key="sk_test_" + "d" * 24)


def test_erasure_finds_hold_by_email_key_and_keeps_financial_row(client, paid, account, stripe):
    from webinars.management.commands.purge_personal_data import erase_person
    from webinars.models import TicketOrder
    from webinars.normalization import email_key

    from webinars.models import Person

    _resp, order = _hold(client, paid)
    assert order.email_key == email_key(DEFAULT_EMAIL)
    person = Person.objects.get(email_key=email_key(DEFAULT_EMAIL))
    erase_person(person.pk)
    order = TicketOrder.objects.get(pk=order.pk)
    assert order.pending_payload == {} and order.email_key == ""
    assert order.amount_minor == 2500


def test_person_delete_keeps_order(occurrence):
    from webinars.models import TicketOrder

    person = make_person(email="gone@example.com")
    order = _order(occurrence, person=person)
    person.delete()
    assert TicketOrder.objects.get(pk=order.pk).person_id is None


def test_stripe_refund_and_dispute_events(client, paid, account, stripe):
    from webinars import payments
    from webinars.models import Registration

    _resp, order = _hold(client, paid)
    _deliver(client, _event(order))
    order.refresh_from_db()
    reg = Registration.objects.get(pk=order.registration_id)
    dispute = {"id": f"evt_{uuid.uuid4().hex}", "type": "charge.dispute.created", "livemode": False,
               "data": {"object": {"object": "dispute", "payment_intent": "pi_1", "livemode": False}}}
    assert _deliver(client, dispute).status_code == 200
    order.refresh_from_db()
    assert order.status == "refund_required" and payments.blocks_admission(reg)
    refund = {"id": f"evt_{uuid.uuid4().hex}", "type": "charge.refunded", "livemode": False,
              "data": {"object": {"object": "charge", "payment_intent": "pi_1", "refunded": True, "livemode": False}}}
    assert _deliver(client, refund).status_code == 200
    order.refresh_from_db()
    reg.refresh_from_db()
    assert order.status == "refunded" and reg.status == "cancelled"


def test_cancelled_session_flags_paid_orders(occurrence):
    from webinars.scheduling import cancel_occurrence

    order = _order(occurrence, status="fulfilled", registration=make_registration(occurrence))
    cancel_occurrence(occurrence)
    order.refresh_from_db()
    assert order.status == "refund_required"


def test_r4_amount_mismatch_links_intent_so_stripe_refund_matches(client, paid, account, stripe):
    _resp, order = _hold(client, paid)
    assert _deliver(client, _event(order, amount_total=9999, payment_intent="pi_mm")).status_code == 200
    order.refresh_from_db()
    assert order.status == "refund_required" and order.payment_intent_id == "pi_mm"
    refund = {"id": f"evt_{uuid.uuid4().hex}", "type": "charge.refunded", "livemode": False,
              "data": {"object": {"object": "charge", "payment_intent": "pi_mm", "refunded": True, "livemode": False}}}
    assert _deliver(client, refund).status_code == 200
    order.refresh_from_db()
    assert order.status == "refunded"


def test_r4_foreign_session_never_links_intent(client, paid, account, stripe):
    _resp, order = _hold(client, paid)
    _deliver(client, _event(order, id="cs_someone_else", payment_intent="pi_foreign"))
    order.refresh_from_db()
    assert order.status == "refund_required" and order.payment_intent_id != "pi_foreign"


def test_r5_email_key_cleared_when_order_closes(client, paid, account, stripe):
    _resp, order = _hold(client, paid)
    assert order.email_key
    assert _deliver(client, _event(order, "checkout.session.expired", payment_status="unpaid")).status_code == 200
    order.refresh_from_db()
    assert order.status == "expired" and order.email_key == "" and order.pending_payload == {}


def test_r5_email_key_cleared_on_fulfilment(client, paid, account, stripe):
    _resp, order = _hold(client, paid)
    _deliver(client, _event(order))
    order.refresh_from_db()
    assert order.status == "fulfilled" and order.email_key == ""


def test_r7_sweep_expires_open_checkout_of_cancelled_session(client, paid, account, monkeypatch, stripe):
    from webinars import payments
    from webinars.scheduling import cancel_occurrence

    _resp, order = _hold(client, paid)
    assert order.hold_expires_at > timezone.now() + timedelta(minutes=10)
    cancel_occurrence(paid)
    fake = FakeStripe([("ok", {"id": order.checkout_session_id, "status": "open", "payment_status": "unpaid"}),
                       ("ok", {"id": order.checkout_session_id, "status": "expired"})])
    monkeypatch.setattr(payments, "stripe_request", fake)
    payments.sweep(now=timezone.now() + timedelta(minutes=3))
    assert [(c["method"], c["path"]) for c in fake.calls] == [
        ("GET", f"/v1/checkout/sessions/{order.checkout_session_id}"),
        ("POST", f"/v1/checkout/sessions/{order.checkout_session_id}/expire"),
    ]
    assert _deliver(client, _event(order, "checkout.session.expired", payment_status="unpaid")).status_code == 200
    order.refresh_from_db()
    assert order.status == "expired"


def test_r7_unsent_hold_of_cancelled_session_released_by_sweep(paid, account):
    from webinars import payments
    from webinars.models import TicketOrder
    from webinars.scheduling import cancel_occurrence

    order = _order(paid, status="hold", hold_expires_at=timezone.now() + timedelta(hours=1))
    TicketOrder.objects.filter(pk=order.pk).update(updated_at=timezone.now() - timedelta(minutes=10))
    cancel_occurrence(paid)
    payments.sweep()
    order.refresh_from_db()
    assert order.status == "expired"


def test_order_status_token_not_accepted_in_query(client, paid, account, stripe):
    resp, order = _hold(client, paid)
    token = resp.json()["order"]["statusToken"]
    assert client.get(f"/api/v1/orders/{order.pk}?token={token}").status_code == 404
    assert client.get(f"/api/v1/orders/{order.pk}", headers={"Webinar-Order-Token": token}).status_code == 200


def test_sweep_error_does_not_stall(occurrence, account, monkeypatch):
    from webinars import payments
    from webinars.models import TicketOrder

    order = _order(occurrence, status="checkout_open", account=account, checkout_session_id="cs_test_e",
                   hold_expires_at=timezone.now() - timedelta(minutes=5))
    TicketOrder.objects.filter(pk=order.pk).update(updated_at=timezone.now() - timedelta(hours=1))

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(payments, "stripe_request", boom)
    payments.sweep()
    order.refresh_from_db()
    assert order.status == "needs_reconciliation" and "sweep error" in order.last_error
    assert order.updated_at > timezone.now() - timedelta(minutes=1)


# ---------------------------------------------------------------------------
# P1-5 / P2 hosting
# ---------------------------------------------------------------------------


def _hosted(occ, provider="zoom", mode="provisioned", state="in_flight"):
    from webinars.models import HostedSession, HostingAccount

    acct = HostingAccount.objects.create(provider=provider, name=provider, enabled=True)
    return HostedSession.objects.create(occurrence=occ, account=acct, provider=provider, mode=mode, state=state,
                                        external_id="123456789")


def test_stale_in_flight_operation_becomes_reconcilable(occurrence):
    from webinars.hosting import service
    from webinars.models import ProvisioningOperation

    session = _hosted(occurrence)
    op = ProvisioningOperation.objects.create(session=session, action="create", status="in_flight",
                                              idempotency_key=f"x-{uuid.uuid4()}")
    ProvisioningOperation.objects.filter(pk=op.pk).update(created_at=timezone.now() - timedelta(minutes=10))
    assert service.recover_stale_operations() >= 1
    session.refresh_from_db()
    op.refresh_from_db()
    assert session.state == "needs_reconciliation" and op.status == "unknown"
    service.reconcile(occurrence, "not_created")
    session.refresh_from_db()
    assert session.state == "failed"


def test_fresh_in_flight_operation_is_left_alone(occurrence):
    from webinars.hosting import service
    from webinars.models import ProvisioningOperation

    session = _hosted(occurrence)
    ProvisioningOperation.objects.create(session=session, action="create", status="in_flight", idempotency_key=f"y-{uuid.uuid4()}")
    assert service.recover_stale_operations() == 0
    session.refresh_from_db()
    assert session.state == "in_flight"


def test_riverside_never_falls_back_to_shared_link(occurrence):
    from webinars.hosting import service

    _hosted(occurrence, provider="riverside", mode="attached", state="linked")
    reg = make_registration(occurrence)
    assert occurrence.join_url
    assert service.attendee_join_url(occurrence, reg) is None


def test_detached_session_uses_occurrence_link(occurrence):
    from webinars.hosting import service

    _hosted(occurrence, provider="riverside", mode="attached", state="detached")
    assert service.attendee_join_url(occurrence, make_registration(occurrence)) == occurrence.join_url
