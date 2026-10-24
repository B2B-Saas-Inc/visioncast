"""Studio configuration of native providers: provider choice, secrets, validation, redaction."""
import json

import pytest
from django.contrib.auth.models import Group
from django.test import Client

from webinars.crypto import decrypt_secret
from webinars.models import Destination

pytestmark = pytest.mark.django_db

PASSWORD = "studio-pass-Quiet-Harbor-7"
SECRET = "phc_TopSecretProjectKey123"


@pytest.fixture(autouse=True)
def vendor_allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", "us.i.posthog.com", "eu.i.posthog.com"]


def _client(django_user_model, role="Administrator"):
    user = django_user_model.objects.create_user(role.lower(), f"{role.lower()}@example.com", PASSWORD)
    user.groups.add(Group.objects.get(name=role))
    user.is_staff = role == "Administrator"
    user.save()
    c = Client()
    c.force_login(user)
    return c


def _form(**over):
    data = {
        "name": "PostHog",
        "provider": "posthog",
        "kind": "generic",
        "url": "",
        "event_types": ["registration.created"],
        "allowed_groups": ["identity", "profile", "organization", "registration", "event"],
        "required_consents": ["analytics"],
        "max_attempts": "8",
        "native-region": "us",
        "native-group_type": "company",
        "native-org_namespace": "crm_account_id",
        "native-processing_ack": "confirmed",
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def _creds(dest):
    dest.refresh_from_db()
    return json.loads(decrypt_secret(dest.credentials_encrypted)) if dest.credentials_encrypted else {}


def _create(c, **over):
    resp = c.post("/studio/integrations/new/", _form(**over))
    assert resp.status_code == 302, resp.content[:2000]
    return Destination.objects.get(name=over.get("name", "PostHog"))


@pytest.fixture
def admin(django_user_model):
    return _client(django_user_model)


def test_create_native_stores_provider_without_secrets_and_derives_url(admin):
    dest = _create(admin)
    creds = _creds(dest)
    assert creds["native_provider"] == "posthog" and creds["native_region"] == "us"
    assert "native_api_key" not in creds
    assert dest.url == "https://us.i.posthog.com" and dest.enabled is False


def test_create_enabled_without_secret_is_refused(admin):
    resp = admin.post("/studio/integrations/new/", _form(enabled="on"))
    assert resp.status_code == 400
    assert not Destination.objects.exists()
    assert b"Project API key is required" in resp.content
    # The chosen provider survives the failed submit.
    assert b'<option value="posthog" selected' in resp.content


def test_secret_set_explicitly_never_rendered_and_preview_redacted(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    resp = admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    assert resp.status_code == 302
    assert _creds(dest)["native_api_key"] == SECRET
    page = admin.get(url)
    assert page.status_code == 200
    assert SECRET.encode() not in page.content
    assert b"[redacted]" in page.content and b"/batch/" in page.content


def test_main_form_save_keeps_secret_and_enables_after_validation(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    resp = admin.post(url, _form(enabled="on", url=dest.url))
    assert resp.status_code == 302, resp.content[:2000]
    dest.refresh_from_db()
    assert dest.enabled is True
    assert _creds(dest)["native_api_key"] == SECRET  # not silently erased


def test_blank_secret_submit_keeps_value(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    admin.post(url + "credentials", {"action": "native", "secret-api_key": ""})
    assert _creds(dest)["native_api_key"] == SECRET


def test_enable_without_analytics_consent_refused(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    resp = admin.post(url, _form(enabled="on", required_consents=[]))
    assert resp.status_code == 400
    dest.refresh_from_db()
    assert dest.enabled is False


def test_vendor_host_must_be_operator_allowlisted(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    resp = admin.post("/studio/integrations/new/", _form())
    assert resp.status_code == 400
    assert not Destination.objects.exists()


def test_generic_credentials_form_keeps_provider_settings(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    admin.post(url + "credentials", {"cred-0-name": "extra", "cred-0-value": "v1"})
    creds = _creds(dest)
    assert creds["native_api_key"] == SECRET and creds["native_provider"] == "posthog" and creds["extra"] == "v1"
    resp = admin.post(url + "credentials", {"cred-0-name": "native_api_key", "cred-0-value": "x" * 10})
    assert resp.status_code == 302
    assert _creds(dest)["native_api_key"] == SECRET


def test_switch_to_generic_removes_native_settings_with_notice(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    resp = admin.post(url, _form(provider="", url="https://hooks.example.net/x"), follow=True)
    assert resp.status_code == 200
    assert "native_provider" not in _creds(dest)
    assert b"settings and secrets were removed" in resp.content


def test_generic_destination_unchanged(admin):
    resp = admin.post(
        "/studio/integrations/new/",
        _form(name="Hook", provider="", url="https://hooks.example.net/x"),
    )
    assert resp.status_code == 302
    dest = Destination.objects.get(name="Hook")
    assert dest.url == "https://hooks.example.net/x" and dest.credentials_encrypted == ""


def test_analyst_cannot_set_secrets(admin, django_user_model):
    dest = _create(admin)
    analyst = _client(django_user_model, "Analyst")
    resp = analyst.post(
        f"/studio/integrations/{dest.pk}/credentials", {"action": "native", "secret-api_key": SECRET}
    )
    assert resp.status_code in (403, 404)
    assert "native_api_key" not in _creds(dest)
