"""Create-integration form: a chosen native provider shows its settings and credential boxes
before anything is saved, hides the webhook-only fields, and stores the secret at creation."""
import pytest

from tests.test_native_studio import _client, _creds, _form
from webinars.models import Destination

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def vendor_allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", "us.i.posthog.com", "eu.i.posthog.com"]


@pytest.fixture
def admin(django_user_model):
    return _client(django_user_model)


def test_get_with_provider_renders_settings_and_secret_boxes_without_saving(admin):
    resp = admin.get("/studio/integrations/new/?provider=posthog&name=PostHog+leads")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'name="native-region"' in html and 'name="native-group_type"' in html
    assert 'name="secret-api_key"' in html and 'type="password"' in html
    assert "PostHog settings" in html and 'value="PostHog leads"' in html
    assert "https://us.i.posthog.com" in html
    assert "Save to load this provider" not in html
    # Webhook-only fields are present (still submitted) but hidden.
    assert 'data-webhook-only hidden' in html and 'data-native-only>' in html
    assert "Platform preset" in html and "sent server-side to PostHog" in html


def test_get_without_provider_is_the_webhook_form(admin):
    html = admin.get("/studio/integrations/new/").content.decode()
    assert 'data-webhook-only>' in html and 'data-native-only hidden' in html
    assert "native-region" not in html and "secret-api_key" not in html
    assert "We POST a signed JSON message" in html and 'data-provider-reload' in html


def test_get_with_unknown_provider_or_preset_is_ignored(admin):
    html = admin.get("/studio/integrations/new/?provider=nope").content.decode()
    assert "native-region" not in html
    html = admin.get("/studio/integrations/new/?preset=zapier").content.decode()
    assert 'value="zapier" selected' in html


def test_create_stores_secret_typed_into_the_form(admin):
    resp = admin.post("/studio/integrations/new/", _form(**{"secret-api_key": "phc_live_key_0123456789"}))
    assert resp.status_code == 302, resp.context.get("errors") if resp.context else resp.content[:500]
    dest = Destination.objects.get(name="PostHog")
    creds = _creds(dest)
    assert creds["native_api_key"] == "phc_live_key_0123456789" and creds["native_region"] == "us"
    assert dest.url == "https://us.i.posthog.com"
    page = admin.get(f"/studio/integrations/{dest.pk}/").content.decode()
    assert "phc_live_key" not in page and 'name="secret-api_key"' in page  # aside form, never echoed


def test_create_refuses_malformed_secret_and_never_echoes_it(admin):
    resp = admin.post("/studio/integrations/new/", _form(**{"secret-api_key": "has space"}))
    assert resp.status_code == 400
    assert "Project API key looks invalid" in resp.content.decode()
    assert "has space" not in resp.content.decode()
    assert not Destination.objects.filter(name="PostHog").exists()


def test_create_without_secret_still_works_and_edit_page_has_no_secret_boxes_in_main_form(admin):
    resp = admin.post("/studio/integrations/new/", _form())
    assert resp.status_code == 302
    dest = Destination.objects.get(name="PostHog")
    assert "native_api_key" not in _creds(dest)
    html = admin.get(f"/studio/integrations/{dest.pk}/").content.decode()
    assert html.count('name="secret-api_key"') == 1 and "new-secret-help" not in html
    assert 'data-webhook-only hidden' in html
