"""Riverside personal links are bound to the provider account + event they were made for
(docs/hosting.md, "Riverside registrants").

The HostedSession row is reused across detach / re-attach, so a ProviderRegistrant only
counts while its binding matches the session's current account and event ID. Nothing
here contacts Riverside: scripted fake transports only, and a guard fails any other call.
"""
import json

import pytest

from tests.conftest import make_api_key
from tests.test_hosting import Fake
from tests.test_riverside_admission import _denied
from tests.test_waiting_room import registered_viewer, room_occurrence
from webinars import crypto, waiting_room
from webinars.hosting import base, service
from webinars.models import HostedSession, ProviderRegistrant

pytestmark = pytest.mark.django_db

LINK_A = "https://riverside.fm/studio/a?t=OLD-EVENT-TOKEN"
LINK_B = "https://riverside.fm/studio/b?t=NEW-EVENT-TOKEN"
URL = "/api/v1/manage/occurrences/{}/hosting/registrants/reconcile"


@pytest.fixture(autouse=True)
def guard(settings, monkeypatch):
    settings.WEBHOOK_HOST_ALLOWLIST = ["platform.riverside.fm"]
    base.clear_token_cache()

    def no_network(*a, **kw):
        raise AssertionError("unexpected provider call")

    monkeypatch.setattr(base, "default_transport", no_network)
    yield
    base.clear_token_cache()


def _account(name="rv"):
    account, _w = service.save_account(provider="riverside", name=name, config={},
                                       posted_secrets={"api_key": "rv-api-key-SECRET-1"}, enabled=True)
    return account


def _attach(occ, account, event_id):
    return service.execute(occ, account, "attach", external_id=event_id, transport=Fake((200, {"id": event_id})))


def _switch(occ, account, event_id):
    """Detach and re-attach (same HostedSession row) to ``event_id`` on ``account``."""
    session_pk = service.session_for(occ).pk
    service.detach(occ)
    _attach(occ, account, event_id)
    session = service.session_for(occ)
    assert session.pk == session_pk and session.external_id == event_id and session.account_id == account.pk
    return session


def _registered(event_id="rv-event-A", account=None):
    occ = room_occurrence(join_url="")
    account = account or _account()
    _attach(occ, account, event_id)
    waiting_room.set_admission(occ, "open")
    reg, _token, cap = registered_viewer(occ)
    return occ, account, reg, cap


def _link_via_provider(reg, link=LINK_A):
    assert service.ensure_registrant(reg, transport=Fake((201, {"id": "rv-reg-1", "join_link": link}))) == "linked"
    return ProviderRegistrant.objects.get(registration=reg)


def _pr(reg):
    return ProviderRegistrant.objects.get(registration=reg)


# ---------------------------------------------------------------- creation captures the binding


def test_creation_captures_binding_and_admits():
    occ, account, reg, cap = _registered()
    pr = _link_via_provider(reg)
    assert (pr.event_account_id, pr.event_external_id) == (account.pk, "rv-event-A")
    result = waiting_room.admit(occ, cap)
    assert result.admitted and result.join_url == LINK_A


# ---------------------------------------------------------------- event switch on the same session row


def test_old_linked_denied_after_switch_and_flagged_without_calls():
    occ, account, reg, cap = _registered()
    _link_via_provider(reg)
    _switch(occ, account, "rv-event-B")
    assert service.attendee_join_url(occ, reg) is None
    _denied(occ, cap, reg, "no_link")
    # The sweep (guard transport) never re-sends: the row is flagged for a person instead.
    service.sweep_registrants()
    pr = _pr(reg)
    assert pr.state == "needs_reconciliation" and not pr.join_url_encrypted
    assert pr.event_external_id == "rv-event-A"  # not backfilled
    assert service.ensure_registrant(reg) == "needs_reconciliation"
    assert _pr(reg).attempts == 1


def test_old_inflight_completion_fenced_after_switch():
    occ, account, reg, cap = _registered()

    def switch_then_succeed(method, url, body, headers, *, allowed_hosts):
        _switch(occ, account, "rv-event-B")
        return 201, json.dumps({"id": "rv-reg-1", "join_link": LINK_A}).encode(), None, False

    assert service.ensure_registrant(reg, transport=switch_then_succeed) == "needs_reconciliation"
    pr = _pr(reg)
    assert not pr.join_url_encrypted and pr.attempts == 1 and pr.event_external_id == "rv-event-A"
    _denied(occ, cap, reg, "no_link")


def test_reconcile_rebinds_without_resetting_attempts_and_old_callback_stays_fenced():
    """Attempt 1 is in flight for event A; meanwhile the session moves to event B, attempt 1
    is recovered, a person says not_created (binding B) and attempt 2 links at B. The late
    attempt-1 success must not claim the row."""
    occ, account, reg, cap = _registered()

    def meanwhile(method, url, body, headers, *, allowed_hosts):
        _switch(occ, account, "rv-event-B")
        ProviderRegistrant.objects.filter(registration=reg).update(state="needs_reconciliation")  # recovered as lost
        session = service.session_for(occ)
        assert service.reconcile_registrant(reg, "not_created", expected_attempts=1,
                                            expected_binding=(session.account_id, session.external_id)) == "failed"
        rebound = _pr(reg)
        assert rebound.attempts == 1 and rebound.event_external_id == "rv-event-B"
        assert service.ensure_registrant(
            reg, transport=Fake((201, {"id": "rv-reg-2", "join_link": LINK_B}))) == "linked"
        return 201, json.dumps({"id": "rv-reg-1", "join_link": LINK_A}).encode(), None, False

    assert service.ensure_registrant(reg, transport=meanwhile) == "linked"
    pr = _pr(reg)
    assert pr.attempts == 2 and pr.event_external_id == "rv-event-B"
    assert crypto.decrypt_secret(pr.join_url_encrypted) == LINK_B
    result = waiting_room.admit(occ, cap)
    assert result.admitted and result.join_url == LINK_B


def test_reconcile_created_binds_new_event_and_admits_new_link():
    occ, account, reg, cap = _registered()
    _link_via_provider(reg)
    session = _switch(occ, account, "rv-event-B")
    service.recover_stale_operations()
    assert _pr(reg).state == "needs_reconciliation"
    # A confirmation issued for event A cannot be applied after the switch.
    with pytest.raises(Exception):
        service.reconcile_registrant(reg, "created", join_url=LINK_B, expected_binding=(account.pk, "rv-event-A"))
    assert _pr(reg).state == "needs_reconciliation"
    service.reconcile_registrant(reg, "created", join_url=LINK_B, expected_attempts=1,
                                 expected_binding=(session.account_id, session.external_id))
    pr = _pr(reg)
    assert (pr.state, pr.attempts, pr.event_account_id, pr.event_external_id) == ("linked", 1, account.pk, "rv-event-B")
    result = waiting_room.admit(occ, cap)
    assert result.admitted and result.join_url == LINK_B


def test_reconcile_not_created_retries_at_new_event_within_cap():
    occ, account, reg, cap = _registered()
    _link_via_provider(reg)
    _switch(occ, account, "rv-event-B")
    service.recover_stale_operations()
    service.reconcile_registrant(reg, "not_created")
    transport = Fake((201, {"id": "rv-reg-2", "join_link": LINK_B}))
    assert service.sweep_registrants(transport=transport) == 1
    assert "rv-event-B" in transport.calls[0]["url"] and "rv-event-A" not in transport.calls[0]["url"]
    pr = _pr(reg)
    assert pr.attempts == 2 and pr.state == "linked" and pr.event_external_id == "rv-event-B"
    assert service.attendee_join_url(occ, reg) == LINK_B
    # The cap still applies after rebinding.
    ProviderRegistrant.objects.filter(registration=reg).update(state="failed", attempts=service.MAX_REGISTRANT_ATTEMPTS)
    assert service.ensure_registrant(reg) == "failed"


def test_reconcile_refused_while_detached():
    occ, account, reg, _cap = _registered()
    _link_via_provider(reg)
    ProviderRegistrant.objects.filter(registration=reg).update(state="needs_reconciliation")
    service.detach(occ)
    with pytest.raises(Exception):
        service.reconcile_registrant(reg, "created", join_url=LINK_B)
    assert _pr(reg).state == "needs_reconciliation"


# ---------------------------------------------------------------- legacy rows and account identity


def test_legacy_unbound_row_denied_and_never_backfilled():
    occ, account, reg, cap = _registered()
    session = service.session_for(occ)
    ProviderRegistrant.objects.create(registration=reg, session=session, state="linked", attempts=1,
                                      join_url_encrypted=crypto.encrypt_secret(LINK_A))
    assert service.attendee_join_url(occ, reg) is None
    _denied(occ, cap, reg, "no_link")
    service.recover_stale_operations()
    pr = _pr(reg)
    assert pr.state == "needs_reconciliation" and not pr.join_url_encrypted
    assert pr.event_account_id is None and pr.event_external_id == ""
    assert service.ensure_registrant(reg) == "needs_reconciliation"  # guard: no call
    service.reconcile_registrant(reg, "created", join_url=LINK_B)
    assert service.attendee_join_url(occ, reg) == LINK_B


def test_same_external_id_different_account_denied():
    occ, account, reg, cap = _registered()
    _link_via_provider(reg)
    other = _account("rv-other")
    _switch(occ, other, "rv-event-A")
    assert service.attendee_join_url(occ, reg) is None
    _denied(occ, cap, reg, "no_link")
    service.sweep_registrants()
    assert _pr(reg).state == "needs_reconciliation"


def test_same_event_reattach_keeps_link():
    occ, account, reg, cap = _registered()
    _link_via_provider(reg)
    _switch(occ, account, "rv-event-A")
    service.recover_stale_operations()
    assert _pr(reg).state == "linked"
    assert waiting_room.admit(occ, cap).join_url == LINK_A


# ---------------------------------------------------------------- confirmation tokens


def test_rest_confirmation_invalidated_by_reattach(client):
    occ, account, reg, _cap = _registered()
    assert service.ensure_registrant(reg, transport=Fake(TimeoutError())) == "needs_reconciliation"
    key = make_api_key(("hosting_provision",), name="hprov")
    body = {"registrationId": str(reg.pk), "decision": "created", "joinUrl": LINK_A}

    def post(token=None):
        headers = {"Authorization": f"Bearer {key}"}
        if token:
            headers["Webinar-Confirm-Token"] = token
        return client.post(URL.format(occ.pk), data=json.dumps(body), content_type="application/json", headers=headers)

    first = post()
    assert first.status_code == 428
    assert first.json()["preview"]["providerEventId"] == "rv-event-A"
    token = first.json()["confirmToken"]
    _switch(occ, account, "rv-event-B")
    stale = post(token)
    assert stale.status_code == 400, stale.content
    pr = _pr(reg)
    assert pr.state == "needs_reconciliation" and not pr.join_url_encrypted and pr.event_external_id == "rv-event-A"
    assert service.attendee_join_url(occ, reg) is None


def test_registrant_state_covers_binding_and_session():
    from webinars.api import hosting as api

    occ, account, reg, _cap = _registered()
    pr = _link_via_provider(reg)
    before = api.registrant_state(pr)
    HostedSession.objects.filter(pk=pr.session_id).update(external_id="rv-event-B")
    assert api.registrant_state(ProviderRegistrant.objects.select_related("session").get(pk=pr.pk)) != before
