"""Waiting-room admission for linked Riverside sessions (docs/hosting.md, "Riverside registrants").

A linked attached Riverside session opens its doors without a shared
``Occurrence.join_url`` and admits only a registrant with their own linked, decryptable,
https Riverside link. Nothing here contacts Riverside: sessions and registrant links are
set up directly in the database, and a guard transport fails any provider call.
"""
import uuid
from datetime import timedelta

import pytest

from tests.conftest import JOIN_URL, make_person, make_registration
from tests.test_waiting_room import _use_cookie, capability_for, registered_viewer, room_occurrence
from webinars import crypto, scheduling, waiting_room
from webinars.hosting import base, service
from webinars.models import (
    AttendanceEvent,
    CheckIn,
    HostedSession,
    HostingAccount,
    ProviderRegistrant,
    Registration,
    TicketOrder,
)

pytestmark = pytest.mark.django_db

LINK = "https://riverside.fm/studio/room?t=PRIVATE-PERSONAL-TOKEN"


@pytest.fixture(autouse=True)
def no_provider_calls(monkeypatch):
    def no_network(*a, **kw):
        raise AssertionError("admission must never call the provider")

    monkeypatch.setattr(base, "default_transport", no_network)


def _riverside(occ, *, enabled=True, state=HostedSession.State.LINKED):
    account = HostingAccount.objects.create(provider="riverside", name=f"rv-{uuid.uuid4().hex[:6]}", enabled=enabled)
    return HostedSession.objects.create(occurrence=occ, account=account, provider="riverside",
                                        mode=HostedSession.Mode.ATTACHED, state=state, external_id="rv-event-1")


def _link(reg, session, link=LINK, *, state=ProviderRegistrant.State.LINKED, raw=None):
    # Bound to the session's current account + event (tests/test_riverside_binding.py
    # covers unbound and stale bindings).
    return ProviderRegistrant.objects.create(
        registration=reg, session=session, state=state, external_id="rv-reg-1",
        event_account_id=session.account_id, event_external_id=session.external_id,
        join_url_encrypted=raw if raw is not None else (crypto.encrypt_secret(link) if link else ""),
    )


def _open_room(join_url="", **kw):
    occ = room_occurrence(join_url=join_url, **kw)
    session = _riverside(occ)
    waiting_room.set_admission(occ, "open")
    return occ, session


def _denied(occ, cap, reg, reason=None):
    result = waiting_room.admit(occ, cap)
    assert not result.admitted and result.join_url is None
    if reason:
        assert result.reason == reason
    assert not CheckIn.objects.filter(registration=reg).exists()
    assert not AttendanceEvent.objects.filter(registration=reg, kind=AttendanceEvent.Kind.CHECKED_IN).exists()
    return result


# ---------------------------------------------------------------- doors and the personal link


def test_no_shared_link_personal_link_admits():
    occ, session = _open_room(join_url="")
    reg, _token, cap = registered_viewer(occ)
    _link(reg, session)
    assert waiting_room.room_state(occ).state == waiting_room.OPEN
    result = waiting_room.admit(occ, cap)
    assert result.admitted and result.created and result.join_url == LINK
    assert CheckIn.objects.filter(occurrence=occ, registration=reg).count() == 1


def test_no_shared_link_http_redirects_only_at_enter(client):
    occ, session = _open_room(join_url="")
    reg, token, _cap = registered_viewer(occ)
    _link(reg, session)
    _use_cookie(client, token)
    resp = client.post(f"/w/{occ.waiting_room_code}/enter")
    assert resp.status_code == 303 and resp["Location"] == LINK
    assert resp.get("Referrer-Policy") == "no-referrer" and "no-store" in resp.get("Cache-Control", "")
    assert CheckIn.objects.filter(registration=reg).count() == 1


def test_without_riverside_session_no_shared_link_stays_preparing():
    occ = room_occurrence(join_url="")
    waiting_room.set_admission(occ, "open")
    assert waiting_room.room_state(occ).state == waiting_room.PREPARING


def test_missing_personal_link_denied_even_with_shared_link():
    occ, session = _open_room(join_url=JOIN_URL)
    reg, _token, cap = registered_viewer(occ)
    assert waiting_room.room_state(occ).state == waiting_room.OPEN
    _denied(occ, cap, reg, "no_link")


@pytest.mark.parametrize("state", ["pending", "needs_reconciliation", "failed"])
def test_unlinked_registrant_states_denied(state):
    occ, session = _open_room(join_url=JOIN_URL)
    reg, _token, cap = registered_viewer(occ)
    _link(reg, session, state=state)
    _denied(occ, cap, reg, "no_link")


@pytest.mark.parametrize("bad", [
    "http://riverside.fm/studio/room?t=x",       # not https
    "https://evil.example.com/studio/room",      # foreign host
    "https://riverside.fm.evil.example/x",       # suffix trick
    "https://user:pw@riverside.fm/x",            # credentials in URL
    "javascript:alert(1)",
    "not a url",
    "",
])
def test_malformed_personal_link_denied(bad):
    occ, session = _open_room(join_url=JOIN_URL)
    reg, _token, cap = registered_viewer(occ)
    _link(reg, session, bad)
    _denied(occ, cap, reg, "no_link")


def test_undecryptable_personal_link_denied():
    occ, session = _open_room(join_url=JOIN_URL)
    reg, _token, cap = registered_viewer(occ)
    _link(reg, session, raw="gAAAAA-not-a-valid-token")
    _denied(occ, cap, reg, "no_link")


# ---------------------------------------------------------------- stale / mismatched records


def test_someone_elses_link_is_not_mine():
    occ, session = _open_room(join_url="")
    other, _t, _c = registered_viewer(occ)
    _link(other, session)
    reg, _token, cap = registered_viewer(occ)
    _denied(occ, cap, reg, "no_link")


def test_link_on_another_occurrences_session_denied():
    occ, _session = _open_room(join_url="")
    elsewhere = room_occurrence(join_url="")
    elsewhere_session = _riverside(elsewhere)
    reg, _token, cap = registered_viewer(occ)
    _link(reg, elsewhere_session)
    _denied(occ, cap, reg, "no_link")
    assert service.attendee_join_url(occ, reg) is None


def test_registration_for_another_occurrence_gets_no_link():
    occ, session = _open_room(join_url="")
    elsewhere = room_occurrence(join_url="")
    foreign = make_registration(elsewhere)
    _link(foreign, session)
    assert service.attendee_join_url(occ, foreign) is None


def test_cancelled_registration_with_link_denied():
    occ, session = _open_room(join_url="")
    reg, _token, cap = registered_viewer(occ)
    _link(reg, session)
    Registration.objects.filter(pk=reg.pk).update(status=Registration.Status.CANCELLED)
    _denied(occ, cap, reg, "not_registered")
    reg.refresh_from_db()
    assert service.attendee_join_url(occ, reg) is None


def test_detached_session_uses_shared_link_not_personal():
    occ, session = _open_room(join_url=JOIN_URL)
    reg, _token, cap = registered_viewer(occ)
    _link(reg, session)
    HostedSession.objects.filter(pk=session.pk).update(state=HostedSession.State.DETACHED)
    assert waiting_room.admit(occ, cap).join_url == JOIN_URL


# ---------------------------------------------------------------- disabled provider config


def test_disabled_account_denies_and_is_not_reenabled():
    occ = room_occurrence(join_url=JOIN_URL)
    session = _riverside(occ, enabled=False)
    waiting_room.set_admission(occ, "open")
    reg, _token, cap = registered_viewer(occ)
    _link(reg, session)
    assert waiting_room.room_state(occ).state == waiting_room.PREPARING
    _denied(occ, cap, reg)
    assert service.attendee_join_url(occ, reg) is None
    assert HostingAccount.objects.get(pk=session.account_id).enabled is False


def test_account_disabled_after_doors_open_denies():
    occ, session = _open_room(join_url="")
    reg, _token, cap = registered_viewer(occ)
    _link(reg, session)
    HostingAccount.objects.filter(pk=session.account_id).update(enabled=False)
    _denied(occ, cap, reg)


# ---------------------------------------------------------------- paid tickets


def _order(occ, reg=None, status="fulfilled"):
    return TicketOrder.objects.create(
        occurrence=occ, registration=reg, status=status, mode="test", amount_minor=2500, currency="usd",
        idempotency_key=f"k-{uuid.uuid4().hex}", request_hash="x", capability_hash=uuid.uuid4().hex,
        capability_expires_at=occ.starts_at, hold_expires_at=occ.starts_at,
    )


@pytest.mark.parametrize("status", ["refunded", "refund_required"])
def test_refunded_ticket_denied_despite_personal_link(status):
    occ, session = _open_room(join_url="")
    reg, _token, cap = registered_viewer(occ)
    _link(reg, session)
    _order(occ, reg, status)
    _denied(occ, cap, reg, "refund")


def test_unpaid_checkout_has_no_registration_and_is_denied():
    # A Registration exists only after verified-webhook fulfillment; an open checkout
    # leaves the buyer unregistered, so admission stops before any link lookup.
    occ, _session = _open_room(join_url="")
    person = make_person(email=f"buyer-{uuid.uuid4().hex[:8]}@example.com")
    _order(occ, None, "checkout_open")
    _t, cap = capability_for(person)
    result = waiting_room.admit(occ, cap)
    assert not result.admitted and result.reason == "not_registered" and result.join_url is None
    assert not CheckIn.objects.filter(occurrence=occ).exists()


def test_fulfilled_ticket_with_personal_link_admits():
    occ, session = _open_room(join_url="")
    reg, _token, cap = registered_viewer(occ)
    _link(reg, session)
    _order(occ, reg, "fulfilled")
    assert waiting_room.admit(occ, cap).join_url == LINK


# ---------------------------------------------------------------- room controls still apply


def _linked_viewer(occ, session):
    reg, token, cap = registered_viewer(occ)
    _link(reg, session)
    return reg, cap


def test_before_doors_open_denied():
    occ = room_occurrence(join_url="", starts_in=timedelta(days=2))
    session = _riverside(occ)
    reg, cap = _linked_viewer(occ, session)
    assert waiting_room.room_state(occ).state == waiting_room.WAITING
    _denied(occ, cap, reg, "not_open")


def test_auto_doors_open_at_lead_without_shared_link():
    occ = room_occurrence(join_url="", starts_in=timedelta(minutes=5), lead=15)
    session = _riverside(occ)
    reg, cap = _linked_viewer(occ, session)
    assert waiting_room.room_state(occ).state == waiting_room.OPEN
    assert waiting_room.admit(occ, cap).join_url == LINK


def test_hold_denied():
    occ, session = _open_room(join_url="")
    reg, cap = _linked_viewer(occ, session)
    waiting_room.set_admission(occ, "hold")
    _denied(occ, cap, reg, "not_open")


def test_ended_denied():
    occ, session = _open_room(join_url="")
    reg, cap = _linked_viewer(occ, session)
    waiting_room.set_admission(occ, "ended")
    _denied(occ, cap, reg, "not_open")


def test_scheduled_end_passed_denied():
    occ = room_occurrence(join_url="", starts_in=timedelta(hours=-3))
    session = _riverside(occ)
    reg, cap = _linked_viewer(occ, session)
    assert waiting_room.room_state(occ).state == waiting_room.ENDED
    _denied(occ, cap, reg, "not_open")


def test_cancelled_denied():
    occ, session = _open_room(join_url="")
    reg, cap = _linked_viewer(occ, session)
    scheduling.cancel_occurrence(occ)
    occ.refresh_from_db()
    _denied(occ, cap, reg, "not_open")


# ---------------------------------------------------------------- read-only surfaces


def test_status_and_page_never_expose_link_or_check_in(client):
    occ, session = _open_room(join_url=JOIN_URL)
    reg, token, cap = registered_viewer(occ)
    _link(reg, session)
    for mode in ("auto", "open", "hold"):
        waiting_room.set_admission(occ, mode)
        for viewer in (None, cap):
            payload = waiting_room.status_payload(occ, viewer)
            assert "PRIVATE-PERSONAL-TOKEN" not in repr(payload) and JOIN_URL not in repr(payload)
    waiting_room.set_admission(occ, "open")
    _use_cookie(client, token)
    status = client.get(f"/w/{occ.waiting_room_code}/status")
    page = client.get(f"/w/{occ.waiting_room_code}/")
    assert status.status_code == 200 and status.json()["admit"] is True
    for resp in (status, page):
        body = resp.content.decode()
        assert "PRIVATE-PERSONAL-TOKEN" not in body and LINK not in body and JOIN_URL not in body
    assert not CheckIn.objects.filter(registration=reg).exists()
    assert not AttendanceEvent.objects.filter(registration=reg).exists()


def test_denied_enter_redirects_back_to_room(client):
    occ, _session = _open_room(join_url=JOIN_URL)
    reg, token, _cap = registered_viewer(occ)
    _use_cookie(client, token)
    resp = client.post(f"/w/{occ.waiting_room_code}/enter")
    assert resp.status_code == 303 and resp["Location"] != JOIN_URL
    assert f"/w/{occ.waiting_room_code}/" in resp["Location"]
    assert not CheckIn.objects.filter(registration=reg).exists()
