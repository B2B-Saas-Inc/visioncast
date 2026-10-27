"""Riverside registrant reconciliation over REST, MCP and the studio (docs/hosting.md).

Every route calls ``service.reconcile_registrant``; nothing here contacts Riverside: the
only provider traffic is the fake transport used to set registrants up, and a guard
transport fails the test if any listed/reconcile route tries a call.
"""
import json
import uuid

import pytest

from tests.conftest import make_api_key, make_event, make_form, make_occurrence, make_registration
from tests.test_hosting import Fake
from tests.test_mcp import tool_names
from tests.test_mcp_config import confirm, run, text_of
from webinars import crypto
from webinars.hosting import base, service
from webinars.models import HostedSession, HostingAccount, ProviderRegistrant

pytestmark = pytest.mark.django_db

LINK = "https://riverside.fm/studio/room?t=PRIVATE-REG-TOKEN"
RV_SECRET = "rv-api-key-SECRET-1"
URL = "/api/v1/manage/occurrences/{}/hosting/registrants"


@pytest.fixture(autouse=True)
def hosting_settings(settings, monkeypatch):
    settings.WEBHOOK_HOST_ALLOWLIST = ["platform.riverside.fm", "zoom.us", "api.zoom.us"]
    base.clear_token_cache()

    def no_network(*a, **kw):
        raise AssertionError("no provider call may be made by these routes")

    monkeypatch.setattr(base, "default_transport", no_network)
    yield settings
    base.clear_token_cache()


def _riverside_account():
    account, _w = service.save_account(provider="riverside", name="rv", config={},
                                       posted_secrets={"api_key": RV_SECRET}, enabled=True)
    return account


def _stalled(slug, account=None):
    """An attached Riverside session with one registrant whose create outcome is unknown."""
    occ = make_occurrence(make_event(make_form(version=f"rc-{slug}"), slug=slug), join_url="")
    account = account or _riverside_account()
    service.execute(occ, account, "attach", external_id=f"rv-event-{slug}", transport=Fake((200, {"id": f"rv-event-{slug}"})))
    reg = make_registration(occ)
    assert service.ensure_registrant(reg, transport=Fake(TimeoutError())) == "needs_reconciliation"
    return occ, reg, account


@pytest.fixture
def stalled():
    return _stalled("rv-a")


@pytest.fixture
def provision_key(db):
    return make_api_key(("hosting_provision",), name="hprov")


def _auth(raw, token=None):
    headers = {"Authorization": f"Bearer {raw}"}
    if token:
        headers["Webinar-Confirm-Token"] = token
    return headers


def _post(client, raw, occ, body, token=None):
    return client.post(URL.format(occ.pk) + "/reconcile", data=json.dumps(body), content_type="application/json",
                       headers=_auth(raw, token))


def _pr(reg):
    return ProviderRegistrant.objects.get(registration=reg)


# ---------------------------------------------------------------- REST: list


def test_list_scoped_to_occurrence_and_redacted(client, stalled, provision_key):
    occ, reg, account = stalled
    other_occ, other_reg, _a = _stalled("rv-b", account)
    for raw in (provision_key, make_api_key(("config_read",), name="ro")):
        resp = client.get(URL.format(occ.pk), headers=_auth(raw))
        assert resp.status_code == 200, resp.content
        rows = resp.json()["results"]
        assert [r["registrationId"] for r in rows] == [str(reg.pk)]
        assert set(rows[0]) == {"registrationId", "state", "reconcilable", "attempts", "boundToCurrentEvent",
                                "updatedAt"}
        text = resp.content.decode()
        assert reg.person.email not in text and "Pat" not in text and RV_SECRET not in text
        assert str(other_reg.pk) not in text


def test_list_and_reconcile_denied_without_scope(client, stalled):
    occ, reg, _a = stalled
    for scopes in (("hosting_admin",), ("payments_admin",), ("content_write",)):
        raw = make_api_key(scopes, name="x")
        assert client.get(URL.format(occ.pk), headers=_auth(raw)).status_code == 403
        assert _post(client, raw, occ, {"registrationId": str(reg.pk), "decision": "not_created"}).status_code == 403
    ro = make_api_key(("config_read",), name="ro")
    assert _post(client, ro, occ, {"registrationId": str(reg.pk), "decision": "not_created"}).status_code == 403
    assert _pr(reg).state == "needs_reconciliation"


# ---------------------------------------------------------------- REST: reconcile


def test_rest_preview_does_not_mutate_or_echo_link(client, stalled, provision_key):
    occ, reg, _a = stalled
    before = _pr(reg)
    resp = _post(client, provision_key, occ, {"registrationId": str(reg.pk), "decision": "created", "joinUrl": LINK})
    assert resp.status_code == 428, resp.content
    body = resp.json()
    assert body["preview"]["joinUrlProvided"] is True and body["confirmToken"]
    assert "PRIVATE-REG-TOKEN" not in resp.content.decode()
    after = _pr(reg)
    assert (after.state, after.attempts, after.join_url_encrypted, after.updated_at) == (
        before.state, before.attempts, before.join_url_encrypted, before.updated_at)


def test_rest_created_path_stores_link_privately(client, stalled, provision_key):
    occ, reg, _a = stalled
    body = {"registrationId": str(reg.pk), "decision": "created", "joinUrl": LINK}
    token = _post(client, provision_key, occ, body).json()["confirmToken"]
    resp = _post(client, provision_key, occ, body, token)
    assert resp.status_code == 200, resp.content
    assert resp.json()["state"] == "linked" and "PRIVATE-REG-TOKEN" not in resp.content.decode()
    pr = _pr(reg)
    assert pr.state == "linked" and crypto.decrypt_secret(pr.join_url_encrypted) == LINK
    assert service.attendee_join_url(occ, reg) == LINK
    # Replay of the same token is refused (and nothing is left to reconcile anyway).
    replay = _post(client, provision_key, occ, body, token)
    assert replay.status_code in (400, 409)
    assert "PRIVATE-REG-TOKEN" not in replay.content.decode()


def test_rest_not_created_path_returns_to_failed(client, stalled, provision_key):
    occ, reg, _a = stalled
    body = {"registrationId": str(reg.pk), "decision": "not_created"}
    token = _post(client, provision_key, occ, body).json()["confirmToken"]
    resp = _post(client, provision_key, occ, body, token)
    assert resp.status_code == 200, resp.content
    assert _pr(reg).state == "failed" and _pr(reg).attempts == 1


def test_rest_token_bound_to_args_key_and_state(client, stalled, provision_key):
    occ, reg, _a = stalled
    body = {"registrationId": str(reg.pk), "decision": "created", "joinUrl": LINK}
    token = _post(client, provision_key, occ, body).json()["confirmToken"]
    other_link = {**body, "joinUrl": "https://riverside.fm/studio/room?t=OTHER"}
    assert _post(client, provision_key, occ, other_link, token).json()["error"]["code"] == "confirmation_mismatch"
    other_key = make_api_key(("hosting_provision",), name="hprov2")
    assert _post(client, other_key, occ, body, token).json()["error"]["code"] == "confirmation_mismatch"
    # State changes (a new attempt) invalidate the token.
    ProviderRegistrant.objects.filter(registration=reg).update(attempts=2)
    assert _post(client, provision_key, occ, body, token).json()["error"]["code"] == "confirmation_mismatch"
    assert _pr(reg).state == "needs_reconciliation" and not _pr(reg).join_url_encrypted


def test_rest_replay_refused(client, stalled, provision_key):
    occ, reg, _a = stalled
    body = {"registrationId": str(reg.pk), "decision": "not_created"}
    token = _post(client, provision_key, occ, body).json()["confirmToken"]
    assert _post(client, provision_key, occ, body, token).status_code == 200
    # Back to needs_reconciliation by a new attempt: the old token must not resolve it.
    ProviderRegistrant.objects.filter(registration=reg).update(state="needs_reconciliation", attempts=2)
    again = _post(client, provision_key, occ, body, token)
    assert again.status_code == 400 and again.json()["error"]["code"] in ("confirmation_used", "confirmation_mismatch")
    assert _pr(reg).state == "needs_reconciliation"


def test_rest_refuses_wrong_occurrence_and_unknown_ids(client, stalled, provision_key):
    occ, reg, account = stalled
    other_occ, other_reg, _a = _stalled("rv-c", account)
    for rid in (str(other_reg.pk), str(uuid.uuid4())):
        resp = _post(client, provision_key, occ, {"registrationId": rid, "decision": "not_created"})
        assert resp.status_code == 404, resp.content
    assert _pr(other_reg).state == "needs_reconciliation"
    assert _post(client, provision_key, occ, {"registrationId": "nope", "decision": "not_created"}).status_code == 400


def test_rest_refuses_non_riverside_and_non_stalled(client, stalled, provision_key):
    occ, reg, _a = stalled
    ProviderRegistrant.objects.filter(registration=reg).update(state="linked")
    assert _post(client, provision_key, occ, {"registrationId": str(reg.pk), "decision": "not_created"}).status_code == 409
    zoom_occ = make_occurrence(make_event(make_form(version="rc-z"), slug="rc-zoom"), join_url="")
    zoom, _w = service.save_account(provider="zoom", name="z", config={"host_user_id": "h@example.com"},
                                    posted_secrets={"account_id": "acct_1", "client_id": "c", "client_secret": "s"},
                                    enabled=False)
    session = service.get_or_create_session(zoom_occ, zoom)
    zreg = make_registration(zoom_occ)
    ProviderRegistrant.objects.create(registration=zreg, session=session, state="needs_reconciliation", attempts=1)
    resp = _post(client, provision_key, zoom_occ, {"registrationId": str(zreg.pk), "decision": "not_created"})
    assert resp.status_code == 409
    assert client.get(URL.format(zoom_occ.pk), headers=_auth(provision_key)).json()["results"] == []


def test_rest_invalid_link_refused_before_token(client, stalled, provision_key):
    occ, reg, _a = stalled
    for body in ({"decision": "created", "joinUrl": "https://evil.example.com/x"},
                 {"decision": "created"}, {"decision": "not_created", "joinUrl": LINK}, {"decision": "maybe"}):
        resp = _post(client, provision_key, occ, {"registrationId": str(reg.pk), **body})
        assert resp.status_code == 400 and "confirmToken" not in resp.json(), body
    assert _pr(reg).state == "needs_reconciliation"


def test_service_fence_rejects_changed_attempt(stalled):
    _occ, reg, _a = stalled
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        service.reconcile_registrant(reg, "not_created", expected_attempts=7)
    assert _pr(reg).state == "needs_reconciliation"


# ---------------------------------------------------------------- MCP


def test_mcp_tools_scoped_and_parity(client, provision_key):
    from webinars.api import hosting
    from webinars.mcp_config import TOOLS

    tools = {t["name"]: t for t in TOOLS}
    assert {"list_session_registrants", "reconcile_session_registrant"} <= tool_names(client, provision_key)
    ro = tool_names(client, make_api_key(("config_read",), name="ro"))
    assert "list_session_registrants" in ro and "reconcile_session_registrant" not in ro
    assert not {"list_session_registrants", "reconcile_session_registrant"} & tool_names(
        client, make_api_key(("hosting_admin",), name="ha"))
    gated = tools["reconcile_session_registrant"]
    assert gated["gated"] and set(gated["scopes"]) == {"hosting_provision"}
    props = gated["inputSchema"]["properties"]
    assert set(hosting.REGISTRANT_RECONCILE_INPUT_SCHEMA["properties"]) | {"occurrenceId", "dryRun", "confirmToken"} == set(props)
    assert gated["inputSchema"]["additionalProperties"] is False


def test_openapi_documents_routes(client):
    from webinars.openapi import build_spec

    spec = build_spec()
    listing = spec["paths"]["/api/v1/manage/occurrences/{id}/hosting/registrants"]["get"]
    post = spec["paths"]["/api/v1/manage/occurrences/{id}/hosting/registrants/reconcile"]["post"]
    assert "config_read" in listing["description"] and "428" in post["responses"]
    assert "ProviderRegistrant" in spec["components"]["schemas"]
    assert "joinUrl" not in spec["components"]["schemas"]["ProviderRegistrant"]["properties"]


def test_mcp_list_and_dry_run_no_mutation(client, stalled, provision_key):
    occ, reg, _a = stalled
    listed = run(client, provision_key, "list_session_registrants", {"occurrenceId": str(occ.pk)})
    assert not listed["isError"] and reg.person.email not in text_of(listed)
    args = {"occurrenceId": str(occ.pk), "registrationId": str(reg.pk), "decision": "created", "joinUrl": LINK}
    dry = run(client, provision_key, "reconcile_session_registrant", args)
    assert not dry["isError"], dry
    assert "PRIVATE-REG-TOKEN" not in text_of(dry) and RV_SECRET not in text_of(dry)
    assert _pr(reg).state == "needs_reconciliation"


def test_mcp_created_and_replay(client, stalled, provision_key):
    occ, reg, _a = stalled
    args = {"occurrenceId": str(occ.pk), "registrationId": str(reg.pk), "decision": "created", "joinUrl": LINK}
    dry, done = confirm(client, provision_key, "reconcile_session_registrant", args)
    assert not done["isError"], done
    assert "PRIVATE-REG-TOKEN" not in text_of(done)
    assert _pr(reg).state == "linked"
    replay = run(client, provision_key, "reconcile_session_registrant",
                 {**args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert replay["isError"]


def test_mcp_not_created_state_changed_and_wrong_occurrence(client, stalled, provision_key):
    occ, reg, account = stalled
    args = {"occurrenceId": str(occ.pk), "registrationId": str(reg.pk), "decision": "not_created"}
    dry = run(client, provision_key, "reconcile_session_registrant", args)
    ProviderRegistrant.objects.filter(registration=reg).update(attempts=3)
    stale = run(client, provision_key, "reconcile_session_registrant",
                {**args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert stale["isError"] and _pr(reg).state == "needs_reconciliation"
    _dry, done = confirm(client, provision_key, "reconcile_session_registrant", args)
    assert not done["isError"] and _pr(reg).state == "failed"
    other_occ, other_reg, _a = _stalled("rv-d", account)
    wrong = run(client, provision_key, "reconcile_session_registrant",
                {"occurrenceId": str(occ.pk), "registrationId": str(other_reg.pk), "decision": "not_created"})
    assert wrong["isError"] and _pr(other_reg).state == "needs_reconciliation"


# ---------------------------------------------------------------- Studio


def _user(name, group=None, superuser=False):
    from django.contrib.auth.models import Group, User

    if superuser:
        return User.objects.create_superuser(name, f"{name}@example.com", f"pw-{name}-123")
    user = User.objects.create_user(name, password=f"pw-{name}-123")
    user.groups.add(Group.objects.get_or_create(name=group)[0])
    return user


def _studio_url(occ):
    return f"/studio/sessions/{occ.pk}/hosting/registrants"


def test_studio_unauthorized_roles_refused(client, stalled):
    from webinars.roles import ANALYST, EDITOR

    occ, reg, _a = stalled
    for user in (_user("ana", ANALYST), _user("eddie", EDITOR)):
        client.force_login(user)
        assert client.get(_studio_url(occ)).status_code == 403
        assert client.post(_studio_url(occ), {"registration": str(reg.pk), "decision": "not_created"}).status_code == 403
    client.logout()
    assert client.get(_studio_url(occ)).status_code in (302, 403)
    assert _pr(reg).state == "needs_reconciliation"


def test_studio_admin_lists_masked_and_reconciles(client, stalled):
    import re

    occ, reg, _a = stalled
    client.force_login(_user("root", superuser=True))
    page = client.get(_studio_url(occ))
    html = page.content.decode()
    assert page.status_code == 200 and str(reg.pk) in html
    assert reg.person.email not in html and "***@example.com" in html and RV_SECRET not in html
    token = re.search(r'name="state_token" value="([^"]+)"', html).group(1)
    bad = client.post(_studio_url(occ), {"registration": str(reg.pk), "decision": "created",
                                         "join_url": LINK, "state_token": "forged"})
    assert bad.status_code == 400 and _pr(reg).state == "needs_reconciliation"
    resp = client.post(_studio_url(occ), {"registration": str(reg.pk), "decision": "created",
                                          "join_url": LINK, "state_token": token})
    assert resp.status_code == 302
    assert _pr(reg).state == "linked"
    after = client.get(_studio_url(occ)).content.decode()
    assert "PRIVATE-REG-TOKEN" not in after
    replay = client.post(_studio_url(occ), {"registration": str(reg.pk), "decision": "not_created", "state_token": token})
    assert replay.status_code == 400 and _pr(reg).state == "linked"


def test_studio_state_changed_and_wrong_occurrence(client, stalled):
    import re

    occ, reg, account = stalled
    other_occ, other_reg, _a = _stalled("rv-e", account)
    client.force_login(_user("root", superuser=True))
    token = re.search(r'name="state_token" value="([^"]+)"', client.get(_studio_url(occ)).content.decode()).group(1)
    ProviderRegistrant.objects.filter(registration=reg).update(attempts=4)
    stale = client.post(_studio_url(occ), {"registration": str(reg.pk), "decision": "not_created", "state_token": token})
    assert stale.status_code == 400 and _pr(reg).state == "needs_reconciliation"
    wrong = client.post(_studio_url(occ), {"registration": str(other_reg.pk), "decision": "not_created",
                                           "state_token": token})
    assert wrong.status_code == 400 and _pr(other_reg).state == "needs_reconciliation"


def test_studio_csrf_enforced(stalled):
    from django.test import Client

    occ, reg, _a = stalled
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(_user("root", superuser=True))
    resp = csrf_client.post(_studio_url(occ), {"registration": str(reg.pk), "decision": "not_created"})
    assert resp.status_code == 403 and _pr(reg).state == "needs_reconciliation"


def test_session_unaffected_by_registrant_routes(stalled):
    occ, _reg, _a = stalled
    assert HostedSession.objects.get(occurrence=occ).state == "linked"
    assert HostingAccount.objects.filter(provider="riverside").count() == 1
