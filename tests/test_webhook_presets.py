"""Workflow-automation destination presets (Zapier, Make, n8n): URL and host rules, the
allowlist/SSRF interplay, the studio and MCP save paths, config_read output, unchanged
delivery, and test sends."""
import json

import pytest
from django.contrib.auth.models import Group
from django.test import Client

from tests.conftest import WEBHOOK_SECRET, make_api_key, make_destination
from tests.test_mcp import rpc
from webinars import webhook_presets as presets
from webinars.crypto import decrypt_secret, encrypt_secret, verify_webhook_signature
from webinars.models import Destination, OutboxMessage

pytestmark = pytest.mark.django_db

PASSWORD = "studio-pass-Quiet-Harbor-7"
ZAPIER_URL = "https://hooks.zapier.com/hooks/catch/123456/zbB61/"
MAKE_URL = "https://hook.eu1.make.com/abc123xyz"
N8N_URL = "https://automation.example.net/webhook/webinar-platform"
N8N_CLOUD_URL = "https://acme.app.n8n.cloud/webhook/webinar-platform"


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = [
        "hooks.example.net", "hooks.zapier.com", "hook.eu1.make.com", "hook.us2.make.com",
        "automation.example.net", "*.wild.example.net", "app.n8n.cloud", "us.i.posthog.com",
    ]


def creds(dest):
    dest.refresh_from_db()
    return json.loads(decrypt_secret(dest.credentials_encrypted)) if dest.credentials_encrypted else {}


# ---- URL and host rules -------------------------------------------------------------------


def test_presets_are_the_three_platforms_with_recipes_and_choices():
    assert list(presets.PRESETS) == ["zapier", "make", "n8n"]
    assert presets.CHOICES[0] == ("", "None (plain signed webhook)")
    for p in presets.PRESETS.values():
        assert p.recipe == f"sdk/kits/automation/{p.id}/"
        info = presets.describe(p.id)
        assert info["recipe"] == p.recipe and info["label"] == p.label and "Webinar-Delivery-Id" in info["retries"]
    assert presets.describe("") is None and presets.describe("nope") is None
    assert presets.url_problem("", "https://anything.example/x") is None
    assert presets.url_problem("nope", ZAPIER_URL) == "Unknown preset."


@pytest.mark.parametrize("url", [ZAPIER_URL, "https://hooks.zapier.com/hooks/catch/1/a", "https://HOOKS.zapier.com/hooks/catch/987654321/AbC123/"])
def test_zapier_accepts_catch_hook_urls(url):
    assert presets.url_problem("zapier", url) is None


@pytest.mark.parametrize("url", [
    "https://hooks.zapier.com/hooks/catch/123456/zbB61,kzXC4/",  # combined hook ids
    "https://hooks.zapier.com/hooks/standard/123456/zbB61/",
    "https://hooks.zapier.com/hooks/catch/abc/zbB61/",
    "https://zapier.com/hooks/catch/123456/zbB61",
    "https://hooks.zapier.com.evil.example/hooks/catch/123456/zbB61/",
    "https://hooks.zapier.com/hooks/catch/123456/zbB61/?x=1",
    "https://hooks.zapier.com/hooks/catch/123456/zbB61/#frag",
    "http://hooks.zapier.com/hooks/catch/123456/zbB61/",
    "https://hooks.zapier.com:8443/hooks/catch/123456/zbB61/",
    "https://hooks.zapier.com/hooks/catch/123456/zbB61/extra",
    "", "not a url", "https://",
])
def test_zapier_refuses_other_urls(url):
    problem = presets.url_problem("zapier", url)
    assert problem and "hooks.zapier.com/hooks/catch/" in problem


@pytest.mark.parametrize("zone", presets.MAKE_ZONES)
def test_make_accepts_every_documented_zone(zone):
    assert presets.url_problem("make", f"https://hook.{zone}.make.com/abc123xyz456DEF") is None


@pytest.mark.parametrize("url", [
    "https://hook.eu3.make.com/abc123",
    "https://eu1.make.com/abc123",
    "https://hook.eu1.make.com/abc123/extra",
    "https://hook.eu1.make.com/abc-123",
    "https://hook.eu1.make.com/",
    "https://hook.eu1.make.com/abc123?x=1",
    "https://hook.eu1.make.com.evil.example/abc123",
    "https://hook.eu1.make.celonis.com/abc123",
])
def test_make_refuses_other_urls(url):
    assert presets.url_problem("make", url)


def test_n8n_accepts_production_urls_on_allowlisted_or_cloud_hosts():
    assert presets.url_problem("n8n", N8N_URL) is None
    assert presets.url_problem("n8n", N8N_URL + "/") is None
    assert presets.url_problem("n8n", "https://automation.example.net/webhook/a/b-c.d") is None
    assert presets.url_problem("n8n", N8N_CLOUD_URL) is None  # suffix rule, no exact entry needed


def test_n8n_refuses_test_urls_and_other_paths():
    for url in ["https://automation.example.net/webhook-test/webinar-platform", "https://automation.example.net/webhook-test/",
                "https://acme.app.n8n.cloud/webhook-test/x"]:
        assert "test URL" in presets.url_problem("n8n", url)
    for url in ["https://automation.example.net/webinar-platform", "https://automation.example.net/webhook/",
                "https://automation.example.net/webhook/x?y=1", "https://automation.example.net/api/v1/webhook/x",
                "https://automation.example.net/webhook/../x", "https://automation.example.net/webhook/a b"]:
        assert presets.url_problem("n8n", url), url


def test_n8n_host_must_be_an_exact_allowlist_entry():
    # Wildcard entries and sub-domains of exact entries are not enough (same rule as native per-destination hosts).
    for host in ["sub.automation.example.net", "x.wild.example.net", "wild.example.net", "other.example.org"]:
        problem = presets.url_problem("n8n", f"https://{host}/webhook/x")
        assert problem and "exact WEBHOOK_HOST_ALLOWLIST entry" in problem, host
    # A native vendor host is refused even if allowlisted.
    assert "another vendor" in presets.url_problem("n8n", "https://us.i.posthog.com/webhook/x")
    # A cloud look-alike is not matched by the suffix rule and falls back to the exact-entry rule.
    assert "exact WEBHOOK_HOST_ALLOWLIST entry" in presets.url_problem("n8n", "https://acme.app.n8n.cloud.evil.example/webhook/x")
    assert presets.url_problem("n8n", "https://localhost/webhook/x")


def test_preset_shape_check_does_not_replace_ssrf_policy(settings):
    """A Zapier URL passes the shape check, but the destination form still needs the host allowlisted."""
    from django.core.exceptions import ValidationError

    from webinars import services

    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    assert presets.url_problem("zapier", ZAPIER_URL) is None
    with pytest.raises(ValidationError):
        services.save_destination({"name": "z", "kind": "generic", "url": ZAPIER_URL, "event_types": "[]",
                                   "allowed_groups": "[]", "required_consents": "[]", "field_mapping": "{}",
                                   "max_attempts": 8}, user=None)


def test_with_preset_and_preset_of():
    assert presets.with_preset({}, "zapier") == {"webhook_preset": "zapier"}
    assert presets.with_preset({"webhook_preset": "zapier", "api_token": "t"}, "") == {"api_token": "t"}
    assert presets.with_preset({"webhook_preset": "zapier"}, "make") == {"webhook_preset": "make"}
    assert presets.preset_of({"webhook_preset": "n8n"}) == "n8n"
    assert presets.preset_of({"webhook_preset": "bogus"}) == "" and presets.preset_of({}) == "" and presets.preset_of(None) == ""
    with pytest.raises(ValueError):
        presets.with_preset({}, "bogus")


# ---- studio --------------------------------------------------------------------------------


def _admin(django_user_model):
    user = django_user_model.objects.create_user("administrator", "admin@example.com", PASSWORD)
    user.groups.add(Group.objects.get(name="Administrator"))
    user.is_staff = True
    user.save()
    c = Client()
    c.force_login(user)
    return c


def _form(**over):
    data = {"name": "Zap", "provider": "", "preset": "zapier", "kind": "crm", "url": ZAPIER_URL,
            "event_types": ["registration.created"], "allowed_groups": ["identity", "registration", "event"],
            "max_attempts": "8", "include_private_links": "on"}
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


@pytest.fixture
def admin(django_user_model):
    return _admin(django_user_model)


def test_studio_create_with_preset_pins_defaults_and_records_marker(admin):
    resp = admin.post("/studio/integrations/new/", _form())
    assert resp.status_code == 302, resp.content[:1500]
    dest = Destination.objects.get(name="Zap")
    assert dest.kind == "generic" and dest.include_private_links is False and dest.url == ZAPIER_URL
    assert creds(dest) == {"webhook_preset": "zapier"}
    page = admin.get(f"/studio/integrations/{dest.pk}/").content.decode()
    assert "Zapier (Catch Raw Hook) recipe" in page and "sdk/kits/automation/zapier/" in page
    assert 'value="zapier" selected' in page
    assert "Node simulation" in page


def test_studio_form_offers_the_presets_with_help(admin):
    page = admin.get("/studio/integrations/new/").content.decode()
    assert 'name="preset"' in page
    for value, label in presets.CHOICES:
        assert f'value="{value}"' in page and label in page


@pytest.mark.parametrize("preset,url", [("zapier", MAKE_URL), ("make", ZAPIER_URL), ("n8n", "https://automation.example.net/webhook-test/x"),
                                        ("n8n", "https://x.wild.example.net/webhook/x")])
def test_studio_refuses_urls_that_do_not_match_the_preset(admin, preset, url):
    resp = admin.post("/studio/integrations/new/", _form(preset=preset, url=url))
    assert resp.status_code == 400
    assert Destination.objects.count() == 0
    page = resp.content.decode()
    assert 'id="f-url-err"' in page


def test_studio_refuses_preset_with_native_provider(admin):
    resp = admin.post("/studio/integrations/new/", _form(provider="posthog", url="", **{"native-region": "us"}))
    assert resp.status_code == 400
    assert "not both" in resp.content.decode()
    assert Destination.objects.count() == 0


def test_studio_edit_changing_url_off_the_preset_is_refused_and_clearing_is_allowed(admin):
    admin.post("/studio/integrations/new/", _form())
    dest = Destination.objects.get(name="Zap")
    # The preset is sticky: a URL that no longer matches is refused while it is selected.
    resp = admin.post(f"/studio/integrations/{dest.pk}/", _form(url="https://hooks.example.net/other"))
    assert resp.status_code == 400
    dest.refresh_from_db()
    assert dest.url == ZAPIER_URL
    # Clearing the preset allows any allowlisted URL and removes the marker; kind is free again.
    resp = admin.post(f"/studio/integrations/{dest.pk}/", _form(preset="", url="https://hooks.example.net/other", kind="crm"))
    assert resp.status_code == 302, resp.content[:1500]
    dest.refresh_from_db()
    assert dest.url == "https://hooks.example.net/other" and dest.kind == "crm" and dest.include_private_links is True
    assert creds(dest) == {}
    # Switching to another preset re-validates against the new rule.
    assert admin.post(f"/studio/integrations/{dest.pk}/", _form(preset="make", url=ZAPIER_URL)).status_code == 400
    assert admin.post(f"/studio/integrations/{dest.pk}/", _form(preset="make", url=MAKE_URL)).status_code == 302
    assert creds(dest) == {"webhook_preset": "make"}


def test_studio_generic_credentials_keep_the_preset_and_refuse_the_reserved_name(admin):
    admin.post("/studio/integrations/new/", _form())
    dest = Destination.objects.get(name="Zap")
    resp = admin.post(f"/studio/integrations/{dest.pk}/credentials", {"cred-0-name": "api_token", "cred-0-value": "t0k3n-value"})
    assert resp.status_code == 302
    assert creds(dest) == {"webhook_preset": "zapier", "api_token": "t0k3n-value"}
    page = admin.get(f"/studio/integrations/{dest.pk}/").content.decode()
    assert "<code>api_token</code>" in page and "<code>webhook_preset</code>" not in page
    resp = admin.post(f"/studio/integrations/{dest.pk}/credentials", {"cred-0-name": "webhook_preset", "cred-0-value": "make"}, follow=True)
    assert "reserved" in resp.content.decode()
    assert creds(dest) == {"webhook_preset": "zapier", "api_token": "t0k3n-value"}
    # Replacing with an empty set clears the credential but keeps the preset.
    admin.post(f"/studio/integrations/{dest.pk}/credentials", {})
    assert creds(dest) == {"webhook_preset": "zapier"}


def test_studio_unreadable_credentials_block_setting_a_preset(admin, settings):
    dest = make_destination(name="Old", credentials_encrypted=encrypt_secret('{"api_token": "x"}'))
    settings.INTEGRATION_MASTER_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDE="
    resp = admin.post(f"/studio/integrations/{dest.pk}/", _form(name="Old"))
    assert resp.status_code == 400
    assert "before setting a preset" in resp.content.decode()


# ---- MCP -----------------------------------------------------------------------------------


def run(client, key, name, arguments=None):
    data = rpc(client, key, "tools/call", {"name": name, "arguments": arguments or {}}).json()
    assert "result" in data, data
    return data["result"]


def confirmed(client, key, name, arguments):
    dry = run(client, key, name, arguments)
    assert not dry["isError"], dry
    done = run(client, key, name, {**arguments, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert not done["isError"], done
    return dry, done


@pytest.fixture
def delivery_key(db):
    return make_api_key(("delivery_admin",), name="dlv")


def test_mcp_save_destination_with_preset(client, delivery_key):
    args = {"name": "n8n", "url": N8N_URL, "preset": "n8n", "kind": "email", "includePrivateLinks": True,
            "eventTypes": ["registration.created"]}
    dry, done = confirmed(client, delivery_key, "save_destination", args)
    preview = dry["structuredContent"]["preview"]
    assert preview["preset"]["id"] == "n8n" and preview["preset"]["recipe"] == "sdk/kits/automation/n8n/"
    assert preview["includePrivateLinks"] is False
    out = done["structuredContent"]
    assert out["preset"] == "n8n" and out["kind"] == "generic" and out["includePrivateLinks"] is False
    assert out["credentialNames"] == [] and out["urlHost"] == "automation.example.net"
    dest = Destination.objects.get(pk=out["id"])
    assert creds(dest) == {"webhook_preset": "n8n"}
    # Visible to config_read via list_destinations.
    listing = run(client, make_api_key(("config_read",), name="cfg"), "list_destinations")["structuredContent"]["results"]
    assert listing[0]["preset"] == "n8n"
    # A later update without `preset` keeps it and still validates the URL against it.
    bad = run(client, delivery_key, "save_destination", {"destinationId": out["id"], "url": "https://hooks.example.net/x"})
    assert bad["isError"] and "n8n production webhook URL" in json.dumps(bad)
    # Clearing it allows the plain URL again.
    _, cleared = confirmed(client, delivery_key, "save_destination", {"destinationId": out["id"], "url": "https://hooks.example.net/x", "preset": ""})
    assert cleared["structuredContent"]["preset"] is None
    assert creds(dest) == {}


@pytest.mark.parametrize("preset,url,fragment", [
    ("zapier", MAKE_URL, "Zapier Catch Hook"),
    ("make", "https://hook.eu9.make.com/abc", "Make webhook URL"),
    ("n8n", "https://automation.example.net/webhook-test/x", "test URL"),
    ("n8n", "https://x.wild.example.net/webhook/x", "exact WEBHOOK_HOST_ALLOWLIST"),
    ("n8n", N8N_CLOUD_URL.replace("acme.app.n8n.cloud", "acme.app.n8n.cloud.evil.example"), "exact WEBHOOK_HOST_ALLOWLIST"),
])
def test_mcp_save_destination_refuses_mismatched_urls(client, delivery_key, preset, url, fragment):
    out = run(client, delivery_key, "save_destination", {"name": "x", "url": url, "preset": preset})
    assert out["isError"]
    assert fragment in json.dumps(out)
    assert Destination.objects.count() == 0


def test_mcp_preset_and_native_provider_are_exclusive(client, delivery_key):
    dest = make_destination(name="PH", url="https://us.i.posthog.com/batch/",
                            credentials_encrypted=encrypt_secret('{"native_provider": "posthog", "native_region": "us"}'))
    out = run(client, delivery_key, "save_destination", {"destinationId": str(dest.pk), "preset": "zapier", "url": ZAPIER_URL})
    assert out["isError"] and "native provider" in json.dumps(out)
    out = run(client, delivery_key, "save_destination", {"name": "y", "url": ZAPIER_URL, "preset": "bogus"})
    assert out["isError"]


def test_mcp_credentials_keep_the_preset_and_refuse_the_marker(client, delivery_key):
    _, done = confirmed(client, delivery_key, "save_destination", {"name": "Zap", "url": ZAPIER_URL, "preset": "zapier"})
    dest_id = done["structuredContent"]["id"]
    refused = run(client, delivery_key, "set_destination_credentials", {"destinationId": dest_id, "credentials": {"webhook_preset": "make"}})
    assert refused["isError"] and "save_destination" in json.dumps(refused)
    _, saved = confirmed(client, delivery_key, "set_destination_credentials", {"destinationId": dest_id, "credentials": {"api_token": "abc"}})
    assert saved["structuredContent"]["credentialNames"] == ["api_token"] and saved["structuredContent"]["preset"] == "zapier"
    assert creds(Destination.objects.get(pk=dest_id)) == {"webhook_preset": "zapier", "api_token": "abc"}


def test_mcp_preview_contract_and_test_send_with_preset(client, delivery_key):
    _, done = confirmed(client, delivery_key, "save_destination", {"name": "Make", "url": MAKE_URL, "preset": "make", "enabled": True,
                                                                   "eventTypes": ["registration.created"]})
    dest_id = done["structuredContent"]["id"]
    preview = run(client, delivery_key, "preview_destination_contract", {"destinationId": dest_id})["structuredContent"]
    assert preview["sent"] is False and preview["privateLinks"] is False
    assert preview["preset"]["id"] == "make" and preview["preset"]["recipe"] == "sdk/kits/automation/make/"
    assert "sha256" in preview["preset"]["verification"]
    plain = make_destination(name="Plain")
    assert run(client, delivery_key, "preview_destination_contract", {"destinationId": str(plain.pk)})["structuredContent"]["preset"] is None
    # Test sends queue the fixed fixture through the normal (generic) path.
    dry, queued = confirmed(client, delivery_key, "send_test_delivery", {"destinationId": dest_id})
    assert dry["structuredContent"]["preview"]["destination"]["preset"] == "make"
    assert queued["structuredContent"]["queued"] is True
    msg = OutboxMessage.objects.get(pk=queued["structuredContent"]["messageId"])
    assert msg.event_type == "webhook.test" and msg.destination_id.hex == dest_id.replace("-", "")


def test_mcp_webhook_contract_resource_lists_presets(client, delivery_key):
    from webinars.mcp_resources import webhook_contracts

    contract = webhook_contracts()
    assert set(contract["presets"]) == {"zapier", "make", "n8n"}
    assert contract["presets"]["n8n"]["recipe"] == "sdk/kits/automation/n8n/"


# ---- delivery is unchanged -------------------------------------------------------------------


def test_delivery_with_preset_is_the_same_signed_webhook(monkeypatch):
    import webinars.delivery
    import webinars.ssrf
    from webinars.delivery import deliver

    dest = make_destination(name="Zap", url=ZAPIER_URL, credentials_encrypted=encrypt_secret('{"webhook_preset": "zapier"}'))
    message = OutboxMessage.objects.create(destination=dest, event_type="webhook.test",
                                           payload={"schemaVersion": "1", "eventType": "webhook.test", "occurredAt": "2026-01-01T00:00:00Z",
                                                    "event": {"id": None, "occurrence": {"id": None, "test": True}},
                                                    "delivery_metadata": {"deliveryId": "x", "destinationId": str(dest.pk),
                                                                          "destinationKind": "generic", "eventType": "webhook.test",
                                                                          "createdAt": "2026-01-01T00:00:00Z", "test": True}})
    sent = {}

    def fake(url, body, headers, timeout, *a, **kw):
        sent.update(url=url, body=body, headers=headers)
        return 200, b""

    monkeypatch.setattr(webinars.delivery, "post_json_pinned", fake, raising=False)
    monkeypatch.setattr(webinars.ssrf, "post_json_pinned", fake)
    monkeypatch.setattr(webinars.delivery, "post_json_bounded", fake, raising=False)
    monkeypatch.setattr(webinars.ssrf, "post_json_bounded", fake)
    from webinars.delivery import claim_batch

    (claimed,) = claim_batch("worker-t", 1)
    assert claimed.pk == message.pk
    deliver(claimed)
    message.refresh_from_db()
    assert message.status == OutboxMessage.Status.DELIVERED, message.last_error
    assert sent["url"] == ZAPIER_URL
    headers = sent["headers"]
    assert headers["User-Agent"] == "webinar-platform-webhook/1"
    assert headers["Webinar-Delivery-Id"] == str(message.pk)
    assert verify_webhook_signature(WEBHOOK_SECRET, str(message.pk), headers["Webinar-Timestamp"], sent["body"], headers["Webinar-Signature"])
    assert not any(k.lower().startswith("x-preset") or "zapier" in v.lower() for k, v in headers.items())
    assert b"webhook_preset" not in sent["body"]


def test_fixture_matches_the_backend_signature():
    """The checked-in Node fixture (sdk/kits/automation/test/fixtures) was signed with the backend algorithm."""
    import pathlib

    fixture = json.loads((pathlib.Path(__file__).resolve().parents[1] / "sdk/kits/automation/test/fixtures/deliveries.json").read_text())
    from webinars.crypto import sign_webhook

    body = fixture["body"].encode("utf-8")
    assert sign_webhook(fixture["secret"], fixture["deliveryId"], int(fixture["timestamp"]), body) == fixture["signature"]
    assert verify_webhook_signature(fixture["secret"], fixture["deliveryId"], fixture["timestamp"], body, fixture["rotationSignature"], now=fixture["now"])
    assert not verify_webhook_signature(fixture["secret"], fixture["deliveryId"], fixture["timestamp"], body, fixture["wrongSecretSignature"], now=fixture["now"])
    assert json.loads(fixture["body"])["eventType"] == "registration.created"
