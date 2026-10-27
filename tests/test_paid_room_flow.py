"""Paid buyer flow through the server, end to end (docs/tickets.md "Buyer flow").

A session days ahead (doors not open): an anonymous or recognised-but-unverified
browser gets the uniform 202 ``verification_required``; the hosted waiting room emails
a code (preview mailbox: stored, never sent), the code verifies the browser, the form
posts and 303s to Stripe Checkout; only the signed ``paid`` webhook registers; entry
still waits for the room to open. Stripe and mail are mocked: no network.
"""
import re

import pytest
from django.test import Client

from tests.conftest import COOKIE, JOIN_URL, cookie_value, make_api_key, make_occurrence, make_person, payload, post_registration, sha256
from tests.test_tickets import SK, WHSEC, FakeStripe, _deliver, _event

PREVIEW = "webinars.mail.PreviewMailboxBackend"


@pytest.fixture(autouse=True)
def preview_mail(settings):
    settings.EMAIL_BACKEND = PREVIEW
    settings.WAITING_ROOM_VERIFICATION_TTL_MINUTES = 15
    settings.WAITING_ROOM_POLL_SECONDS = 15


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
def room(event):
    """Paid session five days ahead with the room on and doors opening 15 minutes before."""
    from webinars import waiting_room

    occ = make_occurrence(event, days=5, ticket_price_minor=2500, ticket_currency="usd")
    waiting_room.configure(occ, enabled=True, lead_minutes=15)
    occ.refresh_from_db()
    assert waiting_room.room_state(occ).state == waiting_room.WAITING
    return occ


def _path(occ, suffix=""):
    return f"/w/{occ.waiting_room_code}/{suffix}"


def _verify_in_room(client, occ, email):
    """identify -> code page -> code from the preview mailbox -> verify. Returns the new capability."""
    from webinars.models import BrowserCapability, PreviewMessage

    before = set(PreviewMessage.objects.values_list("pk", flat=True))
    sent = client.post(_path(occ, "identify"), {"email": email})
    assert sent.status_code == 303 and sent["Location"] == _path(occ, "?step=code")
    page = client.get(sent["Location"])
    assert page.status_code == 200 and b"6-digit code" in page.content
    new = list(PreviewMessage.objects.exclude(pk__in=before))
    assert len(new) == 1  # stored for the admin-only mailbox, nothing sent
    code = re.search(r"\b(\d{6})\b", new[0].subject).group(1)
    verified = client.post(_path(occ, "verify"), {"code": code})
    assert verified.status_code == 303 and verified["Location"] == _path(occ)
    cap = BrowserCapability.objects.get(token_hash=sha256(client.cookies[COOKIE].value))
    assert cap.mailbox_verified_at is not None
    return cap


def _assert_nothing_held(stripe):
    from webinars.models import Registration, TicketOrder

    assert not TicketOrder.objects.exists() and not Registration.objects.exists()
    assert stripe.calls == []


# ---------------------------------------------------------------------------
# API: uniform 202 before verification
# ---------------------------------------------------------------------------


def test_api_answer_is_uniform_and_points_at_the_room(client, room, account, stripe):
    from webinars import identity

    key = make_api_key(("proxy",))
    known = make_person(email="known.buyer@example.com", first_name="Kim")
    unverified = identity.issue_capability(known)  # recognised (e.g. a free registration), mailbox not proven
    anon = post_registration(client, payload(room), key)
    stranger_email = post_registration(client, payload(room, profile={"email": "known.buyer@example.com"}), key)
    recognised = post_registration(client, payload(room, profile={"email": "known.buyer@example.com"}), key,
                                   cookie=unverified)
    recognised_other = post_registration(client, payload(room), key, cookie=unverified)
    for resp in (anon, stranger_email, recognised, recognised_other):
        assert resp.status_code == 202
        assert resp.json() == {
            "status": "verification_required",
            "occurrenceId": str(room.pk),
            "verification": {"available": True, "verifyUrl": "https://webinars.example.com" + _path(room),
                             "message": resp.json()["verification"]["message"]},
        }
    assert anon.json() == stranger_email.json() == recognised.json() == recognised_other.json()
    # Cookie-less callers always get a (decoy) Set-Cookie; a recognised browser keeps its own.
    assert cookie_value(anon) and cookie_value(stranger_email)
    assert cookie_value(recognised) is None
    _assert_nothing_held(stripe)


# ---------------------------------------------------------------------------
# Hosted room: verify -> form -> Checkout -> webhook -> admission
# ---------------------------------------------------------------------------


def test_anonymous_buyer_days_ahead_full_flow(room, account, stripe):
    from webinars import waiting_room
    from webinars.models import CheckIn, Registration, TicketOrder

    browser = Client()
    start = browser.get(_path(room))
    assert start.status_code == 200 and b"Join with your email" in start.content
    cap = _verify_in_room(browser, room, "new.buyer@example.com")

    form = browser.get(_path(room))
    assert form.status_code == 200 and b"One more step" in form.content  # firstName still missing
    assert waiting_room.room_state(room).state == waiting_room.WAITING  # doors days away: buying is not gated
    key = "wr-" + "a" * 32
    checkout = browser.post(_path(room, "register"), {"firstName": "Nia", "idempotency_key": key})
    assert checkout.status_code == 303
    assert checkout["Location"].startswith("https://checkout.stripe.com/")
    assert checkout["Referrer-Policy"] == "no-referrer" and checkout["Cache-Control"] == "no-store"

    order = TicketOrder.objects.get(occurrence=room)
    assert order.status == "checkout_open" and order.caller_capability_id == cap.pk
    assert order.amount_minor == 2500 and order.currency == "usd"
    sent = dict(stripe.calls[0]["form"])
    assert sent["line_items[0][price_data][unit_amount]"] == "2500"  # full price, never bypassed
    assert not Registration.objects.exists()  # nothing registered before the verified webhook

    # Reload / double submit: same order, same Checkout, no second Stripe session.
    again = browser.post(_path(room, "register"), {"firstName": "Nia", "idempotency_key": key})
    assert again.status_code == 303 and again["Location"] == checkout["Location"]
    assert TicketOrder.objects.count() == 1 and len(stripe.calls) == 1

    # Entering before payment never reaches the join link.
    early = browser.post(_path(room, "enter"))
    assert early.status_code == 303 and early["Location"] == _path(room)

    # The return page confirms nothing; only the signed paid webhook registers.
    assert browser.get(f"/tickets/return?order={order.pk}&result=success").status_code == 200
    assert not Registration.objects.exists()
    order.refresh_from_db()
    assert _deliver(browser, _event(order)).status_code == 200
    order.refresh_from_db()
    reg = Registration.objects.get(occurrence=room, status="registered")
    assert order.status == "fulfilled" and order.registration_id == reg.pk and reg.person_id == cap.person_id
    assert reg.person.first_name == "Nia"

    page = browser.get(_path(room))
    assert b"on the list" in page.content and JOIN_URL.encode() not in page.content
    status = browser.get(_path(room, "status")).json()
    assert status["viewer"] == {"recognized": True, "registered": True}
    assert status["state"] == "waiting" and status["admit"] is False and status["enterPath"] is None

    # Paid and registered, but the doors are still closed: no join link yet.
    waiting = browser.post(_path(room, "enter"))
    assert waiting.status_code == 303 and waiting["Location"] == _path(room)
    assert not CheckIn.objects.exists()

    waiting_room.set_admission(room, "open")
    entered = browser.post(_path(room, "enter"))
    assert entered.status_code == 303 and entered["Location"] == JOIN_URL
    assert CheckIn.objects.filter(registration=reg).count() == 1


def test_recognised_unverified_browser_is_sent_to_email_step_then_buys(room, account, stripe):
    from webinars import identity
    from webinars.models import Registration, TicketOrder

    person = make_person(email="returning@example.com", first_name="Rae")
    browser = Client()
    browser.cookies[COOKIE] = identity.issue_capability(person)  # recognised from a free registration

    form = browser.get(_path(room))
    assert form.status_code == 200 and b"/register" in form.content
    first = browser.post(_path(room, "register"), {"idempotency_key": "wr-" + "b" * 32})
    assert first.status_code == 202
    assert b"To buy a ticket, confirm your email address first." in first.content
    assert b'action="/w/' + room.waiting_room_code.encode() + b'/identify"' in first.content
    _assert_nothing_held(stripe)

    cap = _verify_in_room(browser, room, "returning@example.com")
    assert cap.person_id == person.pk  # existing person, now mailbox-proven

    confirm = browser.get(_path(room))
    assert b"Your email is confirmed" in confirm.content
    # Required fields are already known; optional ones may stay blank on the way to Checkout.
    checkout = browser.post(_path(room, "register"), {"idempotency_key": "wr-" + "c" * 32})
    assert checkout.status_code == 303 and checkout["Location"].startswith("https://checkout.stripe.com/")
    order = TicketOrder.objects.get()
    assert order.person_id == person.pk and not Registration.objects.exists()

    assert _deliver(browser, _event(order)).status_code == 200
    assert Registration.objects.filter(occurrence=room, person=person, status="registered").count() == 1


def test_buying_works_while_host_holds_the_doors(room, account, stripe):
    from webinars import waiting_room

    waiting_room.set_admission(room, "hold")
    browser = Client()
    _verify_in_room(browser, room, "held.doors@example.com")
    checkout = browser.post(_path(room, "register"), {"firstName": "Hal", "idempotency_key": "wr-" + "d" * 32})
    assert checkout.status_code == 303 and checkout["Location"].startswith("https://checkout.stripe.com/")


def test_unsigned_or_wrong_amount_webhook_never_registers_room_buyer(room, account, stripe):
    from webinars.models import Registration, TicketOrder

    browser = Client()
    _verify_in_room(browser, room, "careful@example.com")
    browser.post(_path(room, "register"), {"firstName": "Cy", "idempotency_key": "wr-" + "e" * 32})
    order = TicketOrder.objects.get()
    assert _deliver(browser, _event(order), sig="t=1,v1=deadbeef").status_code == 400
    _deliver(browser, _event(order, amount_total=1))
    _deliver(browser, _event(order, payment_status="unpaid"))
    assert not Registration.objects.exists()
    assert browser.post(_path(room, "enter"))["Location"] == _path(room)


def test_verification_unavailable_when_mail_disabled(client, room, account, stripe, settings):
    settings.EMAIL_BACKEND = "webinars.mail.DisabledBackend"
    resp = post_registration(client, payload(room), make_api_key(("proxy",)))
    assert resp.status_code == 202
    assert resp.json()["verification"]["available"] is False and resp.json()["verification"]["verifyUrl"] is None
    _assert_nothing_held(stripe)


# ---------------------------------------------------------------------------
# Polling and public URLs
# ---------------------------------------------------------------------------


def test_status_poll_is_bounded_and_discloses_no_links(room, account, stripe):
    from webinars import waiting_room

    browser = Client()
    _verify_in_room(browser, room, "poller@example.com")
    browser.post(_path(room, "register"), {"firstName": "Po", "idempotency_key": "wr-" + "f" * 32})
    body = browser.get(_path(room, "status"))
    assert body["Cache-Control"] == "no-store"
    data = body.json()
    assert isinstance(data["pollAfterSeconds"], int) and 2 <= data["pollAfterSeconds"] <= 15
    text = body.content.decode()
    assert "checkout.stripe.com" not in text and JOIN_URL not in text
    waiting_room.set_admission(room, "ended")
    assert browser.get(_path(room, "status")).json()["pollAfterSeconds"] is None  # polling stops
    # An ended room refuses new purchases.
    late = browser.post(_path(room, "register"), {"firstName": "Po", "idempotency_key": "wr-" + "0" * 32})
    assert late.status_code == 410
