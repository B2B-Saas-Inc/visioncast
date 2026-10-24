"""MCP configuration parity: scopes, gated dry runs, confirmation binding, secret
projections, team administration, test sends, resources and prompts."""
import base64
import json

import pytest

from tests.conftest import (
    JOIN_URL,
    WEBHOOK_SECRET,
    make_api_key,
    make_destination,
    make_event,
    make_form,
    make_occurrence,
    make_person,
    make_registration,
)
from tests.test_mcp import rpc, tool_names

pytestmark = pytest.mark.django_db


def call(client, key, name, arguments=None):
    resp = rpc(client, key, "tools/call", {"name": name, "arguments": arguments or {}})
    assert resp.status_code == 200
    return resp.json()


def result(data):
    assert "result" in data, data
    return data["result"]


def run(client, key, name, arguments=None):
    return result(call(client, key, name, arguments))


def confirm(client, key, name, arguments):
    """Dry run, then the identical call with its token. Returns (dry, done)."""
    dry = run(client, key, name, arguments)
    assert not dry["isError"], dry
    token = dry["structuredContent"]["confirmToken"]
    done = run(client, key, name, {**arguments, "dryRun": False, "confirmToken": token})
    return dry, done


def text_of(data):
    return json.dumps(data)


@pytest.fixture
def config_key(db):
    return make_api_key(("config_read",), name="cfg")


@pytest.fixture
def publish_key(db):
    return make_api_key(("publication",), name="pub")


@pytest.fixture
def delivery_key(db):
    return make_api_key(("delivery_admin",), name="dlv")


@pytest.fixture
def team_key(db):
    # team_admin may only grant roles whose privileges the key itself holds:
    # Editor = content_write + publication, Analyst = reporting.
    return make_api_key(("team_admin", "content_write", "publication", "reporting"), name="team")


def ready_event(slug="ev-pub", visibility="draft"):
    event = make_event(make_form(version=f"f-{slug}"), slug=slug, visibility=visibility, action="Join", attention="Hook")
    make_occurrence(event)
    return event


# ---- scope visibility ---------------------------------------------------------


def test_low_scopes_do_not_see_config_tools(client):
    read = make_api_key(("public_read",))
    names = tool_names(client, read)
    for hidden in ("get_configuration_readiness", "list_destinations", "save_destination", "create_api_key",
                   "set_event_visibility", "set_destination_credentials", "list_team", "invite_member",
                   "send_test_delivery"):
        assert hidden not in names


def test_scoped_tool_lists(client, config_key, publish_key, delivery_key, team_key):
    assert {"get_configuration_readiness", "list_destinations", "get_delivery_status"} <= tool_names(client, config_key)
    assert "save_destination" not in tool_names(client, config_key)
    assert {"set_event_visibility", "publish_recording"} <= tool_names(client, publish_key)
    assert "create_event" not in tool_names(client, publish_key)
    assert {"save_destination", "set_destination_credentials", "requeue_dead_messages",
            "send_test_delivery"} <= tool_names(client, delivery_key)
    assert {"list_team", "invite_member", "update_member", "revoke_invitation"} <= tool_names(client, team_key)
    assert "create_api_key" not in tool_names(client, team_key)


def test_call_without_scope_rejected(client, config_key):
    data = call(client, config_key, "save_destination", {"name": "x", "url": "https://hooks.example.net/x"})
    assert "error" in data


# ---- redaction ------------------------------------------------------------------


def test_readiness_and_destinations_redacted(client, config_key, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    make_destination(url="https://hooks.example.net/webhook/path-token-abc")
    out = text_of(call(client, config_key, "get_configuration_readiness"))
    assert settings.SECRET_KEY not in out
    assert settings.INTEGRATION_MASTER_KEY not in out
    dests = call(client, config_key, "list_destinations")
    body = text_of(dests)
    assert "path-token-abc" not in body
    assert WEBHOOK_SECRET not in body
    row = result(dests)["structuredContent"]["results"][0]
    assert row["urlHost"] == "hooks.example.net"
    assert row["hasSigningSecret"] is True


def test_management_projection_has_no_join_url(client):
    key = make_api_key(("content_write",))
    event = make_event(make_form(), slug="ev-proj")
    make_occurrence(event, join_url=JOIN_URL)
    assert JOIN_URL not in text_of(call(client, key, "get_event", {"slug": "ev-proj"}))


# ---- publication gate -------------------------------------------------------------


def test_update_event_cannot_change_visibility(client):
    from webinars.models import Event

    key = make_api_key(("content_write", "publication"))
    event = ready_event("ev-legacy")
    out = run(client, key, "update_event", {"eventId": str(event.pk), "changes": {"visibility": "public"}})
    assert out["isError"] and "set_event_visibility" in text_of(out)
    assert Event.objects.get(pk=event.pk).visibility == "draft"
    # Same visibility plus content changes is still an ordinary content update.
    ok = run(client, key, "update_event", {"eventId": str(event.pk), "changes": {"visibility": "draft", "summary": "New"}})
    assert not ok["isError"], ok
    assert Event.objects.get(pk=event.pk).summary == "New"


def test_publish_dry_run_does_not_mutate_and_token_is_bound(client, publish_key):
    from webinars.models import AuditLog, ConfirmationUse

    event = ready_event()
    audits = AuditLog.objects.count()
    args = {"eventId": str(event.pk), "visibility": "public"}
    preview = run(client, publish_key, "set_event_visibility", args)["structuredContent"]
    assert preview["dryRun"] is True and preview["confirmToken"]
    assert "not independent human approval" in preview["note"]
    event.refresh_from_db()
    assert event.visibility == "draft"
    assert AuditLog.objects.count() == audits and ConfirmationUse.objects.count() == 0
    token = preview["confirmToken"]

    missing = run(client, publish_key, "set_event_visibility", {**args, "dryRun": False})
    assert missing["isError"]
    tampered = run(client, publish_key, "set_event_visibility",
                   {**args, "visibility": "unlisted", "dryRun": False, "confirmToken": token})
    assert tampered["isError"] and "confirmation_mismatch" in text_of(tampered)
    other_key = make_api_key(("publication",), name="other")
    stolen = run(client, other_key, "set_event_visibility", {**args, "dryRun": False, "confirmToken": token})
    assert stolen["isError"] and "confirmation_mismatch" in text_of(stolen)
    event.refresh_from_db()
    assert event.visibility == "draft"

    done = run(client, publish_key, "set_event_visibility", {**args, "dryRun": False, "confirmToken": token})
    assert not done["isError"], done
    event.refresh_from_db()
    assert event.visibility == "public"
    # Single use: replaying the same token is refused even for a matching request.
    unpublish = {"eventId": str(event.pk), "visibility": "draft"}
    run(client, publish_key, "set_event_visibility", unpublish)
    replay = run(client, publish_key, "set_event_visibility", {**args, "dryRun": False, "confirmToken": token})
    assert replay["isError"]


def test_confirmation_expires(client, publish_key, monkeypatch):
    from webinars import confirmation

    event = ready_event("ev-exp")
    args = {"eventId": str(event.pk), "visibility": "public"}
    token = run(client, publish_key, "set_event_visibility", args)["structuredContent"]["confirmToken"]
    monkeypatch.setattr(confirmation, "MAX_AGE_SECONDS", -1)
    out = run(client, publish_key, "set_event_visibility", {**args, "dryRun": False, "confirmToken": token})
    assert out["isError"] and "confirmation_expired" in text_of(out)


def test_confirmation_single_use_unit(db):
    from webinars import confirmation
    from webinars.models import ApiKey

    key = ApiKey.objects.get(prefix=make_api_key(("publication",)).split("_")[1])
    token = confirmation.issue("t", key, {"a": 1}, state=3)
    with pytest.raises(confirmation.ConfirmationError):
        confirmation.consume("t", key, {"a": 1}, token, state=4)
    confirmation.consume("t", key, {"a": 1}, token, state=3)
    with pytest.raises(confirmation.ConfirmationError) as exc:
        confirmation.consume("t", key, {"a": 1}, token, state=3)
    assert exc.value.code == "confirmation_used"


def test_content_key_cannot_publish_recording_over_mcp(client):
    key = make_api_key(("content_write",))
    assert "publish_recording" not in tool_names(client, key)


def test_delete_draft_is_gated(client):
    from webinars.models import Event

    key = make_api_key(("content_write",))
    event = make_event(make_form(), slug="ev-del", visibility="draft")
    dry, done = confirm(client, key, "delete_draft_event", {"eventId": str(event.pk)})
    assert not done["isError"], done
    assert not Event.objects.filter(pk=event.pk).exists()


# ---- destinations, secrets and test sends --------------------------------------------------


def test_destination_create_dry_run_then_confirm(client, delivery_key, settings):
    from webinars.models import Destination

    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    args = {"name": "CRM", "url": "https://hooks.example.net/in", "eventTypes": ["registration.created"]}
    dry = run(client, delivery_key, "save_destination", args)
    assert not dry["isError"], dry
    assert Destination.objects.count() == 0
    done = run(client, delivery_key, "save_destination",
               {**args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert not done["isError"], done
    assert Destination.objects.count() == 1


def test_credentials_set_only_and_never_echoed(client, delivery_key):
    from webinars.models import Destination

    dest = make_destination()
    creds = {"api_token": "super-secret-credential-value"}
    args = {"destinationId": str(dest.pk), "credentials": creds}
    dry = run(client, delivery_key, "set_destination_credentials", args)
    assert "super-secret-credential-value" not in text_of(dry)
    assert "super-secret" not in dry["structuredContent"]["confirmToken"]
    assert Destination.objects.get(pk=dest.pk).credentials_encrypted == ""
    done = run(client, delivery_key, "set_destination_credentials",
               {**args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert not done["isError"], done
    assert "super-secret-credential-value" not in text_of(done)
    assert done["structuredContent"]["credentialNames"] == ["api_token"]
    stored = Destination.objects.get(pk=dest.pk).credentials_encrypted
    assert stored and "super-secret" not in stored


def test_signing_secret_rotation_gated(client, delivery_key):
    from webinars.crypto import decrypt_secret
    from webinars.models import Destination

    dest = make_destination()
    new = "n" * 40
    dry, done = confirm(client, delivery_key, "set_destination_signing_secret", {"destinationId": str(dest.pk), "secret": new})
    assert new not in text_of(dry) and new not in text_of(done)
    assert decrypt_secret(Destination.objects.get(pk=dest.pk).signing_secret_encrypted) == new


def test_send_test_delivery_queues_only_fixture(client, delivery_key):
    from webinars.models import OutboxMessage

    dest = make_destination(field_mapping={"event.title": "title"})
    args = {"destinationId": str(dest.pk)}
    dry = run(client, delivery_key, "send_test_delivery", args)
    assert OutboxMessage.objects.count() == 0
    assert dry["structuredContent"]["preview"]["payload"]["eventType"] == "webhook.test"
    done = run(client, delivery_key, "send_test_delivery",
               {**args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert not done["isError"], done
    message = OutboxMessage.objects.get()
    assert message.event_type == "webhook.test" and message.person_id is None and message.destination_id == dest.pk
    assert not {"identity", "profile", "registration", "consents", "attribution", "mapped"} & set(message.payload)
    # No URL/payload may be supplied by the caller.
    bad = run(client, delivery_key, "send_test_delivery", {**args, "url": "https://evil.example/"})
    assert bad["isError"]


def test_send_test_delivery_refuses_disabled(client, delivery_key):
    dest = make_destination(enabled=False)
    assert run(client, delivery_key, "send_test_delivery", {"destinationId": str(dest.pk)})["isError"]


# ---- API key delegation and privileged scopes -------------------------------------------------


def test_key_creation_cannot_escalate(client):
    admin = make_api_key(("keys_admin", "public_read", "publication", "team_admin"), name="ka")
    for scopes in (["content_write"], ["keys_admin"], ["delivery_admin"], ["trusted"], ["publication"], ["team_admin"]):
        out = run(client, admin, "create_api_key", {"name": "x", "scopes": scopes})
        assert out["isError"], scopes


def test_key_creation_dry_run_then_create(client):
    from webinars.models import ApiKey

    admin = make_api_key(("keys_admin", "public_read"), name="ka")
    before = ApiKey.objects.count()
    dry = run(client, admin, "create_api_key", {"name": "site", "scopes": ["public_read"]})
    assert ApiKey.objects.count() == before
    done = run(client, admin, "create_api_key", {"name": "site", "scopes": ["public_read"], "dryRun": False,
                                                 "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert done["structuredContent"]["secret"].startswith("wbk_")
    assert ApiKey.objects.count() == before + 1
    assert done["structuredContent"]["secret"] not in text_of(call(client, admin, "list_api_keys"))


def test_privileged_scopes_need_superuser_in_studio_service(db):
    from django.contrib.auth import get_user_model
    from django.core.exceptions import ValidationError

    from webinars import services

    User = get_user_model()
    admin = User.objects.create_user("admin1", "a1@example.com", "x-Long-pass-123", is_staff=True)
    owner = User.objects.create_superuser("owner", "o@example.com", "x-Long-pass-123")
    for scope in services.PRIVILEGED_SCOPES:
        with pytest.raises(ValidationError):
            services.create_api_key("agent", [scope], user=admin)
        key, raw = services.create_api_key("agent", [scope], user=owner)
        assert key.scopes == [scope] and raw.startswith("wbk_")
    key, _raw = services.create_api_key("plain", ["content_write"], user=admin)
    assert key.scopes == ["content_write"]


# ---- team administration ----------------------------------------------------------------------


def _user(username, role=None, **kw):
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group

    user = get_user_model().objects.create_user(username, f"{username}@example.com", "x-Long-pass-123", **kw)
    if role:
        user.groups.set([Group.objects.get(name=role)])
    return user


def test_invite_member_gated_and_token_never_returned(client, team_key):
    from webinars.models import AuditLog, Invitation

    args = {"email": "new.editor@example.com", "role": "Editor"}
    dry = run(client, team_key, "invite_member", args)
    assert Invitation.objects.count() == 0
    done = run(client, team_key, "invite_member",
               {**args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert not done["isError"], done
    invitation = Invitation.objects.get()
    assert invitation.role == "Editor" and invitation.invited_by is None
    body = text_of(done)
    assert "/studio/invite/" not in body and "token" not in done["structuredContent"]
    assert AuditLog.objects.filter(action="invitation.created", actor_api_key__isnull=False).exists()


def test_team_key_cannot_grant_administrator_or_touch_admins(client, team_key):
    from django.core.exceptions import PermissionDenied

    from webinars import invitations
    from webinars.models import ApiKey

    assert run(client, team_key, "invite_member", {"email": "boss@example.com", "role": "Administrator"})["isError"]
    key = ApiKey.objects.get(prefix=team_key.split("_")[1])
    with pytest.raises(PermissionDenied):
        invitations.create_invitation(actor=None, api_key=key, email="boss@example.com", role="Administrator")
    admin = _user("admin2", "Administrator", is_staff=True)
    owner = _user("root2", is_superuser=True, is_staff=True)
    for member in (admin, owner):
        out = run(client, team_key, "update_member", {"memberId": member.pk, "isActive": False})
        assert out["isError"]
        member.refresh_from_db()
        assert member.is_active
    editor = _user("ed1", "Editor")
    assert run(client, team_key, "update_member", {"memberId": editor.pk, "role": "Administrator"})["isError"]
    with pytest.raises(PermissionDenied):
        invitations.update_member(actor=None, api_key=key, member=editor, role="Administrator")
    editor.refresh_from_db()
    assert not editor.is_staff and not editor.is_superuser


def test_team_key_manages_editor_with_gate(client, team_key):
    from webinars.invitations import role_of

    editor = _user("ed2", "Editor")
    args = {"memberId": editor.pk, "role": "Analyst", "isActive": False}
    dry, done = confirm(client, team_key, "update_member", args)
    assert not done["isError"], done
    editor.refresh_from_db()
    assert role_of(editor) == "Analyst" and not editor.is_active and not editor.is_staff


def test_team_key_cannot_mint_human_beyond_its_scopes(client):
    """R3: a bare team_admin key cannot create an Editor (studio publisher) or an Analyst (reports)."""
    from django.core.exceptions import PermissionDenied

    from webinars import invitations
    from webinars.invitations import role_of
    from webinars.models import ApiKey, Invitation

    bare = make_api_key(("team_admin",), name="bare")
    for role in ("Editor", "Analyst"):
        out = run(client, bare, "invite_member", {"email": f"{role.lower()}@example.com", "role": role})
        assert out["isError"] and "scope_escalation" in text_of(out)
    assert Invitation.objects.count() == 0
    analyst_only = make_api_key(("team_admin", "reporting"), name="an")
    assert not run(client, analyst_only, "invite_member", {"email": "a@example.com", "role": "Analyst"})["isError"]
    assert run(client, analyst_only, "invite_member", {"email": "e@example.com", "role": "Editor"})["isError"]
    # Cannot promote an Analyst to Editor, nor touch an existing Editor, without Editor's scopes.
    analyst = _user("an1", "Analyst")
    editor = _user("ed3", "Editor")
    assert run(client, analyst_only, "update_member", {"memberId": analyst.pk, "role": "Editor"})["isError"]
    assert run(client, analyst_only, "update_member", {"memberId": editor.pk, "isActive": False})["isError"]
    key = ApiKey.objects.get(prefix=analyst_only.split("_")[1])
    with pytest.raises(PermissionDenied):
        invitations.update_member(actor=None, api_key=key, member=editor, is_active=False)
    with pytest.raises(PermissionDenied):
        invitations.create_invitation(actor=None, api_key=key, email="x2@example.com", role="Editor")
    analyst.refresh_from_db()
    editor.refresh_from_db()
    assert role_of(analyst) == "Analyst" and editor.is_active


def test_team_key_refuses_accounts_with_direct_perms_or_foreign_groups(client, team_key):
    """R4: no-role accounts holding direct permissions or unknown groups are off limits."""
    from django.contrib.auth.models import Group, Permission

    perm_user = _user("perm1", is_active=False)
    perm_user.user_permissions.add(Permission.objects.get(codename="view_registration"))
    group_user = _user("grp1", is_active=False)
    group_user.groups.add(Group.objects.create(name="Custom ops"))
    mixed = _user("mix1", "Analyst")
    mixed.groups.add(Group.objects.get(name="Editor"))
    for member in (perm_user, group_user, mixed):
        out = run(client, team_key, "update_member", {"memberId": member.pk, "isActive": True, "email": "new@example.com"})
        assert out["isError"], member.username
        member.refresh_from_db()
        assert member.email == f"{member.username}@example.com"
    assert not perm_user.is_active and not group_user.is_active
    plain = _user("plain1", is_active=False)
    dry, done = confirm(client, team_key, "update_member", {"memberId": plain.pk, "isActive": True})
    assert not done["isError"], done


def test_studio_service_refuses_trusted_for_administrator(db):
    """R1: trusted is privileged (verified organization IDs): superuser or owner shell only."""
    from django.contrib.auth import get_user_model
    from django.core.exceptions import ValidationError

    from webinars import services

    admin = get_user_model().objects.create_user("admin3", "a3@example.com", "x-Long-pass-123", is_staff=True)
    assert "trusted" in services.PRIVILEGED_SCOPES
    with pytest.raises(ValidationError):
        services.create_api_key("crm", ["trusted"], user=admin)
    with pytest.raises(ValidationError):
        services.create_api_key("crm", ["proxy", "trusted"], user=admin)


def test_whitespace_key_name_rejected(client):
    from webinars.models import ApiKey

    admin = make_api_key(("keys_admin", "public_read"), name="ka")
    before = ApiKey.objects.count()
    out = run(client, admin, "create_api_key", {"name": "   ", "scopes": ["public_read"]})
    assert out["isError"]
    assert ApiKey.objects.count() == before


def test_non_team_keys_cannot_manage_team(client):
    from django.core.exceptions import PermissionDenied

    from webinars import invitations
    from webinars.models import ApiKey

    raw = make_api_key(("keys_admin", "content_write"))
    assert "invite_member" not in tool_names(client, raw)
    key = ApiKey.objects.get(prefix=raw.split("_")[1])
    with pytest.raises(PermissionDenied):
        invitations.create_invitation(actor=None, api_key=key, email="x@example.com", role="Editor")


# ---- malicious input ------------------------------------------------------------------------


def test_unknown_and_malformed_arguments_rejected(client, delivery_key):
    out = run(client, delivery_key, "save_destination", {"name": "x", "url": "https://h.example/x", "secret": "s"})
    assert out["isError"]
    assert run(client, delivery_key, "requeue_dead_messages", {"messageIds": ["../../etc/passwd"]})["isError"]


def test_cover_rejects_non_image(client):
    key = make_api_key(("content_write",))
    event = make_event(make_form(), slug="ev-cover", visibility="draft")
    out = run(client, key, "set_event_cover",
              {"eventId": str(event.pk), "imageBase64": base64.b64encode(b"<svg onload=alert(1)>").decode()})
    assert out["isError"]


# ---- resources and prompts ---------------------------------------------------------------------


def test_initialize_advertises_resources_and_prompts(client, config_key):
    caps = rpc(client, config_key, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "t", "version": "1"}}).json()["result"]["capabilities"]
    assert "resources" in caps and "prompts" in caps and "tools" in caps


def test_resources_scoped_and_allowlisted(client, config_key):
    from django.conf import settings

    from webinars.openapi import build_spec

    read = make_api_key(("public_read",))
    make_form(version="f-res")
    low = {r["uri"] for r in rpc(client, read, "resources/list").json()["result"]["resources"]}
    assert "webinar://config/current" not in low
    assert {"webinar://docs/field-dictionary", "webinar://openapi", "webinar://forms/f-res",
            "webinar://guides/operations"} <= low
    denied = rpc(client, read, "resources/read", {"uri": "webinar://config/current"}).json()
    assert denied["error"]["code"] == -32002
    for uri in ("file:///etc/passwd", "webinar://docs/../../config/settings.py", "webinar://forms/../../x",
                "webinar://guides/../config/settings.py", "webinar://guides/README.md", 5):
        assert "error" in rpc(client, config_key, "resources/read", {"uri": uri}).json(), uri
    ok = rpc(client, config_key, "resources/read", {"uri": "webinar://config/current"}).json()["result"]["contents"][0]
    assert settings.SECRET_KEY not in ok["text"]
    spec = json.loads(rpc(client, read, "resources/read", {"uri": "webinar://openapi"}).json()["result"]["contents"][0]["text"])
    assert spec == json.loads(json.dumps(build_spec(), default=str))


def test_playbook_contains_full_guides(client):
    from pathlib import Path

    from django.conf import settings

    read = make_api_key(("public_read",))
    text = rpc(client, read, "resources/read", {"uri": "webinar://playbook/implementation"}).json()["result"]["contents"][0]["text"]
    operations = (Path(settings.BASE_DIR) / "docs" / "operations.md").read_text(encoding="utf-8")
    assert operations.strip()[:200] in text
    assert "manage.py migrate" in text


def test_guides_are_packaged_in_image():
    from pathlib import Path

    from django.conf import settings

    from webinars.mcp_resources import GUIDES

    base = Path(settings.BASE_DIR)
    dockerfile = (base / "Dockerfile").read_text()
    dockerignore = (base / ".dockerignore").read_text().splitlines()
    for rel in GUIDES.values():
        assert (base / rel).is_file(), rel
        assert rel in dockerfile, rel
        assert f"!{rel}" in dockerignore, rel


def test_resources_and_prompts_do_not_leak_pii(client, config_key):
    occ = make_occurrence(make_event(make_form(version="f-pii"), slug="ev-pii"), join_url=JOIN_URL)
    person = make_person(email="private.person@example.com", first_name="Secretname")
    make_registration(occ, person=person)
    blob = ""
    for res in rpc(client, config_key, "resources/list").json()["result"]["resources"]:
        data = rpc(client, config_key, "resources/read", {"uri": res["uri"]}).json()
        blob += text_of(data)
    for prompt in ("guided_deployment", "website_launch", "delivery_diagnosis"):
        blob += text_of(rpc(client, config_key, "prompts/get", {"name": prompt, "arguments": {"topic": "AI"}}).json())
    for secret in ("private.person@example.com", "Secretname", JOIN_URL):
        assert secret not in blob


def test_prompts(client, config_key):
    names = {p["name"] for p in rpc(client, config_key, "prompts/list").json()["result"]["prompts"]}
    assert names == {"guided_deployment", "website_launch", "delivery_diagnosis"}
    got = rpc(client, config_key, "prompts/get", {"name": "website_launch", "arguments": {"topic": "{0.__class__}"}}).json()
    assert "{0.__class__}" in got["result"]["messages"][0]["content"]["text"]
    assert "error" in rpc(client, config_key, "prompts/get", {"name": "website_launch", "arguments": {}}).json()
    assert "error" in rpc(client, config_key, "prompts/get", {"name": "nope"}).json()
