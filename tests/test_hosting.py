"""Hosting providers: adapters, provisioning state machine, API and studio.

All provider HTTP is replaced by a fake transport; nothing touches the network.
"""
import base64
import json
from datetime import datetime

import pytest

from tests.conftest import JOIN_URL, make_api_key, make_event, make_form, make_occurrence, make_registration
from webinars import crypto
from webinars.hosting import base, service
from webinars.models import HostedSession, HostingAccount, Occurrence, ProviderRegistrant, ProvisioningOperation

ZOOM_SECRETS = {"account_id": "acct_123456", "client_id": "client-id-abc", "client_secret": "client-secret-xyz"}
ALL_HOSTS = ["zoom.us", "api.zoom.us", "api.wistia.com", "api.introvoke.com", "platform.riverside.fm", "hooks.example.net"]


@pytest.fixture(autouse=True)
def hosting_settings(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = list(ALL_HOSTS)
    base.clear_token_cache()
    yield settings
    base.clear_token_cache()


class Fake:
    """Scripted transport: each entry is (status, dict|bytes) or an exception instance."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, body, headers, *, allowed_hosts):
        self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers), "allowed": set(allowed_hosts)})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, data = item
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        return status, raw, None, False


TOKEN = (200, {"access_token": "zoom-access-token", "expires_in": 3600})


def _account(provider="zoom", secrets=None, config=None, enabled=True):
    secrets = ZOOM_SECRETS if secrets is None and provider == "zoom" else (secrets or {})
    config = {"host_user_id": "host@example.com"} if config is None and provider == "zoom" else (config or {})
    account, _w = service.save_account(provider=provider, name=f"{provider} acct", config=config,
                                       posted_secrets=secrets, enabled=enabled)
    return account


@pytest.fixture
def occ(db):
    return make_occurrence(make_event(make_form(version="fh1"), slug="hosted"), join_url="")


# ---------------------------------------------------------------- Zoom


def test_zoom_create_request_shape_and_linkage(occ):
    account = _account()
    fake = Fake(TOKEN, (201, {"id": 81234567890, "join_url": "https://us02web.zoom.us/w/81234567890?tk=abc",
                              "start_url": "https://us02web.zoom.us/s/81234567890?zak=host-secret"}))
    result = service.execute(occ, account, "create", transport=fake)
    token_call, create_call = fake.calls
    assert token_call["url"] == "https://zoom.us/oauth/token" and token_call["method"] == "POST"
    basic = base64.b64encode(b"client-id-abc:client-secret-xyz").decode()
    assert token_call["headers"]["Authorization"] == f"Basic {basic}"
    assert b"grant_type=account_credentials" in token_call["body"] and b"account_id=acct_123456" in token_call["body"]
    assert create_call["url"] == "https://api.zoom.us/v2/users/host@example.com/webinars"
    assert create_call["headers"]["Authorization"] == "Bearer zoom-access-token"
    body = json.loads(create_call["body"])
    assert body["type"] == 5 and body["duration"] == 60 and body["timezone"] == "Europe/London"
    assert body["start_time"].endswith("Z")
    assert all(c["allowed"] == {"zoom.us", "api.zoom.us"} for c in fake.calls)
    assert result["state"] == "linked" and result["externalId"] == "81234567890"
    assert result["hasJoinUrl"] and result["hasHostUrl"]
    dumped = json.dumps(result)
    assert "zak=host-secret" not in dumped and "tk=abc" not in dumped
    occ.refresh_from_db()
    assert occ.join_url.startswith("https://us02web.zoom.us/w/")
    session = HostedSession.objects.get(occurrence=occ)
    assert "host-secret" not in session.host_url_encrypted
    assert crypto.decrypt_secret(session.host_url_encrypted).endswith("zak=host-secret")


def test_unknown_outcome_requires_reconciliation_and_never_resends(occ):
    account = _account()
    fake = Fake(TOKEN, TimeoutError("slow"))
    result = service.execute(occ, account, "create", transport=fake)
    assert result["state"] == "needs_reconciliation"
    assert ProvisioningOperation.objects.get().status == "unknown"
    again = Fake()
    with pytest.raises(Exception):
        service.execute(occ, account, "create", transport=again)
    assert again.calls == []
    assert service.preview(occ, account, "create")["problems"]


def test_5xx_is_unknown_for_create(occ):
    result = service.execute(occ, _account(), "create", transport=Fake(TOKEN, (503, {"message": "busy"})))
    assert result["state"] == "needs_reconciliation"


def test_4xx_failed_then_retry_allowed(occ):
    account = _account()
    result = service.execute(occ, account, "create", transport=Fake(TOKEN, (400, {"message": "bad"})))
    assert result["state"] == "failed"
    ok = Fake((201, {"id": 81234567891, "join_url": "https://zoom.us/w/81234567891"}))  # token cached
    assert service.execute(occ, account, "create", transport=ok)["state"] == "linked"
    assert len(ok.calls) == 1


def test_reconcile_created_and_not_created(occ):
    account = _account()
    service.execute(occ, account, "create", transport=Fake(TOKEN, TimeoutError()))
    with pytest.raises(Exception):
        service.reconcile(occ, "created", external_id="")
    out = service.reconcile(occ, "not_created")
    assert out["state"] == "failed"
    assert not service.preview(occ, account, "create")["problems"]
    service.execute(occ, account, "create", transport=Fake(TimeoutError()))
    out = service.reconcile(occ, "created", external_id="81234567892")
    assert out["state"] == "linked" and out["externalId"] == "81234567892"


def test_reschedule_marks_out_of_date_and_update_patches(occ):
    from webinars.scheduling import reschedule_occurrence

    account = _account()
    service.execute(occ, account, "create", transport=Fake(TOKEN, (201, {"id": 81234567893})))
    reschedule_occurrence(occ, local_start=datetime(2031, 5, 1, 15, 0), tz_name="Europe/London")
    occ.refresh_from_db()
    session = HostedSession.objects.get(occurrence=occ)
    assert session.state == "out_of_date"
    fake = Fake((204, b""))
    out = service.execute(occ, account, "update", transport=fake)
    assert fake.calls[0]["method"] == "PATCH" and fake.calls[0]["url"] == "https://api.zoom.us/v2/webinars/81234567893"
    assert out["state"] == "linked" and not out["outOfDate"]


def test_cancel_never_calls_provider(occ):
    from webinars.scheduling import cancel_occurrence

    account = _account()
    service.execute(occ, account, "create", transport=Fake(TOKEN, (201, {"id": 81234567894})))
    cancel_occurrence(occ)
    session = HostedSession.objects.get(occurrence=occ)
    assert session.state == "linked" and "cancel it in the provider" in session.last_error


def test_unallowlisted_host_is_a_problem(occ, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    problems = service.preview(occ, _account(), "create")["problems"]
    assert any("WEBHOOK_HOST_ALLOWLIST" in p for p in problems)


# ---------------------------------------------------------------- Wistia / Sequel


def test_wistia_minimum_duration_and_body(db):
    form = make_form(version="fw1")
    short = make_occurrence(make_event(form, slug="w-short"), duration=10)
    account = _account("wistia", secrets={"api_token": "wistia-token-1"})
    assert any("15 minutes" in p for p in service.preview(short, account, "create")["problems"])
    ok = make_occurrence(make_event(form, slug="w-ok"), duration=45)
    fake = Fake((201, {"id": "abc123"}))
    out = service.execute(ok, account, "create", transport=fake)
    call = fake.calls[0]
    assert call["url"] == "https://api.wistia.com/modern/webinars"
    assert call["headers"]["X-Wistia-API-Version"] == "2026-09"
    body = json.loads(call["body"])
    assert set(body) == {"title", "scheduled_for", "event_duration", "time_zone"} and body["event_duration"] == 45
    assert out["state"] == "linked"
    assert service.preview(ok, account, "update")["problems"]  # update not offered


def test_sequel_body(occ):
    account = _account("sequel", secrets={"client_id": "seq-client", "client_secret": "seq-secret"},
                       config={"company_id": "comp-1"})
    fake = Fake((200, {"access_token": "seq-token", "expires_in": 3600}), (200, {"id": "evt-9"}))
    out = service.execute(occ, account, "create", transport=fake)
    create = fake.calls[1]
    assert create["url"] == "https://api.introvoke.com/api/v1/event/create"
    body = json.loads(create["body"])
    assert body["companyId"] == "comp-1" and body["type"] == "Virtual" and body["startDate"] < body["endDate"]
    assert out["externalId"] == "evt-9"


# ---------------------------------------------------------------- Riverside


def test_riverside_attach_registrant_and_attendee_link(occ):
    account = _account("riverside", secrets={"api_key": "rv-api-key-1"})
    assert service.preview(occ, account, "create")["problems"]  # creation not supported
    out = service.execute(occ, account, "attach", external_id="rv-event-1", transport=Fake((200, {"id": "rv-event-1"})))
    assert out["state"] == "linked" and out["mode"] == "attached"
    reg = make_registration(occ)
    fake = Fake((201, {"id": "reg-1", "join_link": "https://riverside.fm/studio/x?t=personal"}))
    assert service.ensure_registrant(reg, transport=fake) == "linked"
    assert json.loads(fake.calls[0]["body"])["email"] == reg.person.email
    assert service.attendee_join_url(occ, reg) == "https://riverside.fm/studio/x?t=personal"
    again = Fake()
    assert service.ensure_registrant(reg, transport=again) == "linked" and again.calls == []


def test_riverside_unknown_registrant_not_resent(occ):
    account = _account("riverside", secrets={"api_key": "rv-api-key-1"})
    service.execute(occ, account, "attach", external_id="rv-event-2", transport=Fake((200, {"id": "rv-event-2"})))
    reg = make_registration(occ)
    assert service.ensure_registrant(reg, transport=Fake(TimeoutError())) == "needs_reconciliation"
    again = Fake()
    assert service.sweep_registrants(transport=again) == 0 and again.calls == []
    assert ProviderRegistrant.objects.get().state == "needs_reconciliation"


# ---------------------------------------------------------------- Audit / late outcomes (R6)


class During(Fake):
    """Fake transport that runs ``hooks[i]`` just before answering call ``i``: simulates
    work that happens elsewhere while a provider call is still out."""

    def __init__(self, *responses, hooks=None):
        super().__init__(*responses)
        self.hooks = hooks or {}

    def __call__(self, method, url, body, headers, *, allowed_hosts):
        hook = self.hooks.get(len(self.calls))
        if hook is not None:
            hook()
        return super().__call__(method, url, body, headers, allowed_hosts=allowed_hosts)


def _lost_and_reconciled(occ):
    from datetime import timedelta

    from django.utils import timezone

    assert service.recover_stale_operations(now=timezone.now() + timedelta(minutes=6)) >= 1
    assert HostedSession.objects.get(occurrence=occ).state == "needs_reconciliation"
    assert service.reconcile(occ, "not_created")["state"] == "failed"


def test_audit_metadata_keeps_provider_action(occ):
    from webinars.models import AuditLog

    service.execute(occ, _account(), "create", transport=Fake(TOKEN, TimeoutError()))
    started = AuditLog.objects.get(action="hosting.operation_started")
    finished = AuditLog.objects.get(action="hosting.operation_finished")
    assert started.metadata["operation_action"] == "create" and finished.metadata["outcome"] == "unknown"


def test_late_success_after_recovery_and_reconcile_does_not_link(occ):
    account = _account()
    fake = During(TOKEN, (201, {"id": 81234567895, "join_url": "https://zoom.us/w/81234567895?tk=late",
                                "start_url": "https://zoom.us/s/81234567895?zak=late"}),
                  hooks={1: lambda: _lost_and_reconciled(occ)})
    out = service.execute(occ, account, "create", transport=fake)
    assert out["state"] == "needs_reconciliation" and out["externalId"] is None
    assert not out["hasJoinUrl"] and not out["hasHostUrl"]
    occ.refresh_from_db()
    assert occ.join_url == ""
    op = ProvisioningOperation.objects.get()
    assert op.status == "unknown" and "late outcome: succeeded" in op.error
    assert service.preview(occ, account, "create")["problems"]  # no silent duplicate create
    assert service.reconcile(occ, "created", external_id="81234567895")["state"] == "linked"


def test_late_failure_after_reconcile_leaves_state(occ):
    fake = During(TOKEN, (400, {"message": "bad"}), hooks={1: lambda: _lost_and_reconciled(occ)})
    out = service.execute(occ, _account(), "create", transport=fake)
    assert out["state"] == "failed"
    assert "late outcome: failed" in ProvisioningOperation.objects.get().error


def test_late_success_does_not_clobber_newer_operation(occ):
    account = _account()

    def rerun():
        _lost_and_reconciled(occ)
        newer = Fake((201, {"id": 81234567897, "join_url": "https://zoom.us/w/81234567897"}))  # token cached
        assert service.execute(occ, account, "create", transport=newer)["state"] == "linked"

    fake = During(TOKEN, (201, {"id": 81234567896, "join_url": "https://zoom.us/w/81234567896"}), hooks={1: rerun})
    out = service.execute(occ, account, "create", transport=fake)
    assert out["state"] == "linked" and out["externalId"] == "81234567897"
    occ.refresh_from_db()
    assert occ.join_url == "https://zoom.us/w/81234567897"
    old, = ProvisioningOperation.objects.filter(error__contains="late outcome")
    assert old.status == "failed"  # as reconciled; not flipped while a newer op owns the session


def test_late_registrant_outcome_is_fenced_and_reconcilable(occ):
    from datetime import timedelta

    from django.utils import timezone

    account = _account("riverside", secrets={"api_key": "rv-api-key-1"})
    service.execute(occ, account, "attach", external_id="rv-event-3", transport=Fake((200, {"id": "rv-event-3"})))
    reg = make_registration(occ)
    fake = During((201, {"id": "reg-late", "join_link": "https://riverside.fm/studio/x?t=late"}),
                  hooks={0: lambda: service.recover_stale_operations(now=timezone.now() + timedelta(minutes=6))})
    assert service.ensure_registrant(reg, transport=fake) == "needs_reconciliation"
    pr = ProviderRegistrant.objects.get()
    assert not pr.join_url_encrypted and "late outcome: succeeded" in pr.last_error
    assert service.attendee_join_url(occ, reg) is None

    with pytest.raises(Exception):
        service.reconcile_registrant(reg, "created", join_url="https://evil.example.com/x")
    with pytest.raises(Exception):
        service.reconcile_registrant(reg, "maybe")
    assert service.reconcile_registrant(reg, "created", join_url="https://riverside.fm/studio/x?t=found") == "linked"
    assert service.attendee_join_url(occ, reg) == "https://riverside.fm/studio/x?t=found"
    with pytest.raises(Exception):
        service.reconcile_registrant(reg, "not_created")  # nothing left to reconcile


def test_reconcile_registrant_not_created_allows_sweep_retry(occ):
    account = _account("riverside", secrets={"api_key": "rv-api-key-1"})
    service.execute(occ, account, "attach", external_id="rv-event-4", transport=Fake((200, {"id": "rv-event-4"})))
    reg = make_registration(occ)
    assert service.ensure_registrant(reg, transport=Fake(TimeoutError())) == "needs_reconciliation"
    assert service.reconcile_registrant(reg, "not_created") == "failed"
    retry = Fake((201, {"id": "reg-4", "join_link": "https://riverside.fm/studio/x?t=retry"}))
    assert service.sweep_registrants(transport=retry) == 1 and len(retry.calls) == 1
    assert ProviderRegistrant.objects.get().state == "linked"


# ---------------------------------------------------------------- StreamYard


def test_streamyard_manual_link_no_outbound(occ, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no outbound call allowed")

    monkeypatch.setattr(base, "default_transport", boom)
    account = _account("streamyard")
    assert service.preview(occ, account, "create")["problems"]
    with pytest.raises(Exception):
        service.set_manual_link(occ, account, join_url="https://evil.example.com/x")
    out = service.set_manual_link(occ, account, join_url="https://streamyard.com/watch/abc")
    assert out["state"] == "linked" and out["mode"] == "manual" and out["hasJoinUrl"]
    assert Occurrence.objects.get(pk=occ.pk).join_url == "https://streamyard.com/watch/abc"


# ---------------------------------------------------------------- Accounts


def test_secrets_set_only_and_never_serialized(db):
    account = _account()
    data = service.serialize_account(account)
    assert data["secretsSet"] == {"account_id": True, "client_id": True, "client_secret": True}
    assert "client-secret-xyz" not in json.dumps(data) and "client-secret-xyz" not in account.credentials_encrypted
    account, _ = service.save_account(account, provider="zoom", name="renamed", config={"host_user_id": "host@example.com"},
                                      posted_secrets={"client_secret": ""}, enabled=True)
    assert service.load_secrets(account)["client_secret"] == "client-secret-xyz"


def test_enable_requires_credentials(db):
    with pytest.raises(Exception):
        service.save_account(provider="zoom", name="z", config={"host_user_id": "host@example.com"}, posted_secrets={}, enabled=True)


# ---------------------------------------------------------------- REST API


def _auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def test_api_execute_needs_confirmation_and_hides_secrets(client, occ, monkeypatch):
    account = _account()
    raw = make_api_key(("hosting_provision", "hosting_admin"), name="hosting")
    resp = client.get("/api/v1/manage/hosting/accounts", headers=_auth(raw))
    assert resp.status_code == 200 and "client-secret-xyz" not in resp.content.decode()
    body = {"accountId": str(account.pk), "action": "create"}
    resp = client.post(f"/api/v1/manage/occurrences/{occ.pk}/hosting/execute", data=json.dumps(body),
                       content_type="application/json", headers=_auth(raw))
    assert resp.status_code == 428
    token = resp.json()["confirmToken"]
    assert not HostedSession.objects.filter(occurrence=occ).exists()
    fake = Fake(TOKEN, (201, {"id": 81234567895, "start_url": "https://zoom.us/s/1?zak=SECRETHOST"}))
    monkeypatch.setattr(base, "default_transport", fake)
    resp = client.post(f"/api/v1/manage/occurrences/{occ.pk}/hosting/execute", data=json.dumps(body),
                       content_type="application/json", headers={**_auth(raw), "Webinar-Confirm-Token": token})
    assert resp.status_code == 200, resp.content
    assert resp.json()["state"] == "linked" and "SECRETHOST" not in resp.content.decode()


def test_config_read_can_get_not_post(client, occ):
    account = _account()
    raw = make_api_key(("config_read",), name="ro")
    assert client.get("/api/v1/manage/hosting/providers", headers=_auth(raw)).status_code == 200
    assert client.get(f"/api/v1/manage/occurrences/{occ.pk}/hosting", headers=_auth(raw)).status_code == 200
    resp = client.post(f"/api/v1/manage/occurrences/{occ.pk}/hosting/preview",
                       data=json.dumps({"accountId": str(account.pk), "action": "create"}),
                       content_type="application/json", headers=_auth(raw))
    assert resp.status_code == 403
    assert client.post("/api/v1/manage/hosting/accounts", data="{}", content_type="application/json",
                       headers=_auth(raw)).status_code == 403


def test_public_event_api_has_no_provider_links(client, occ):
    account = _account()
    service.execute(occ, account, "create", transport=Fake(TOKEN, (201, {
        "id": 81234567896, "join_url": "https://zoom.us/w/PRIVATEJOIN", "start_url": "https://zoom.us/s/PRIVATEHOST"})))
    content = client.get(f"/api/v1/events/{occ.event.slug}").content.decode()
    assert "PRIVATEJOIN" not in content and "PRIVATEHOST" not in content


# ---------------------------------------------------------------- Studio


def test_studio_analyst_forbidden_and_admin_page_hides_secrets(client, occ):
    from django.contrib.auth.models import Group, User

    from webinars.roles import ANALYST

    _account()
    analyst = User.objects.create_user("ana", password="pw-ana-123")
    analyst.groups.add(Group.objects.get_or_create(name=ANALYST)[0])
    client.force_login(analyst)
    assert client.get("/studio/hosting/").status_code == 403
    assert client.get(f"/studio/sessions/{occ.pk}/hosting").status_code == 403
    admin = User.objects.create_superuser("root", "root@example.com", "pw-root-123")
    client.force_login(admin)
    page = client.get("/studio/hosting/")
    assert page.status_code == 200
    account = HostingAccount.objects.get()
    edit = client.get(f"/studio/hosting/{account.pk}/").content.decode()
    assert "client-secret-xyz" not in edit and "Set" in edit
    panel = client.get(f"/studio/sessions/{occ.pk}/hosting")
    assert panel.status_code == 200 and JOIN_URL not in panel.content.decode()
