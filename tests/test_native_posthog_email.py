"""PostHog transactional email adapter: mocked send contract, idempotency, identity and host policy.

No network: transports are fakes and socket connects raise. The request shape follows PostHog's
transactional email and API references fetched 2026-11-02 (docs/native-integrations.md); nothing
here has been checked against a real PostHog project.
"""
import json
import socket

import pytest

from tests.test_native_integrations import Fake
from tests.test_transactional_email import mail_body
from webinars import native_integrations as ni
from webinars.models import Destination
from webinars.native_providers import _transactional_email as tx
from webinars.native_providers import posthog_email

pytestmark = pytest.mark.django_db

KEY = "phs_SecretProjectKeyValue_1234567890"
HOST = "us.posthog.com"
SEND_URL = f"https://{HOST}/api/projects/@current/messaging/send/email/"
TEMPLATE = "01963d8a-6f2c-7d4e-9b1a-3c5e8f2a7b10"
POSTHOG = {
    "native_provider": "posthog_email",
    "native_region": "us",
    "native_template_id": TEMPLATE,
    "native_api_key": KEY,
    "native_subject_prefix": "[Example]",
    "native_send_ack": "confirmed",
    "native_processing_ack": "confirmed",
}
KW = dict(event_types=list(tx.EVENT_TYPES), allowed_groups=list(Destination.GROUPS), required_consents=[])
DELIVERY_ID = ni.SAMPLE_BODY["delivery_metadata"]["deliveryId"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    settings.PUBLIC_BASE_URL = "https://webinars.example.com"
    settings.PUBLIC_EVENT_URL_TEMPLATE = ""


def run(creds=POSTHOG, b=None, event_type="registration.created", fake=None):
    fake = fake or Fake()
    return ni.execute("posthog_email", creds, b if b is not None else mail_body(), event_type, fake.ctx()), fake


def test_offered_and_schema_driven():
    assert dict(ni.PROVIDER_CHOICES)["posthog_email"] == "PostHog transactional email"
    assert ni.HOSTS["posthog_email"] == {"us": "us.posthog.com", "eu": "eu.posthog.com"}
    names = [f.name for f in ni.FIELDS["posthog_email"]]
    # The sender is the template's: no from or reply-to settings.
    assert not {"from_address", "from_name", "reply_to"} & set(names)
    assert {f.name for f in ni.FIELDS["posthog_email"] if f.secret} == {"api_key"}
    assert ni.supported_event_types("posthog_email") == tx.EVENT_TYPES and "posthog_email" in ni.NEEDS_REQUEST_TRANSPORT


def test_valid_config_and_required_fields():
    assert ni.validate("posthog_email", POSTHOG, **KW) == {}
    for key in ("native_template_id", "native_api_key", "native_send_ack", "native_processing_ack"):
        creds = {k: v for k, v in POSTHOG.items() if k != key}
        assert f"native-{key[len('native_'):]}" in ni.validate("posthog_email", creds, **KW)


@pytest.mark.parametrize("field,value", [("template_id", "not-a-uuid"), ("api_key", "phc_PublicProjectToken123456"), ("api_key", "phx_PersonalKeyValue1234567890")])
def test_formats(field, value):
    assert f"native-{field}" in ni.validate("posthog_email", {**POSTHOG, f"native_{field}": value}, **KW)


def test_credential_is_set_only():
    rendered = repr(ni.config_of(POSTHOG, "posthog_email")) + repr(ni.secrets_set(POSTHOG, "posthog_email"))
    assert KEY not in rendered and ni.secrets_set(POSTHOG, "posthog_email") == {"api_key": True}


def test_exact_request():
    outcome, fake = run()
    assert outcome.kind == "delivered"
    [call] = fake.calls
    assert call["method"] == "POST" and call["url"] == SEND_URL
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["headers"]["Idempotency-Key"] == DELIVERY_ID
    body = call["json"]
    assert set(body) == {"template_id", "to", "distinct_id", "message_data"}
    assert body["template_id"] == TEMPLATE and body["to"] == "person@example.com"
    assert body["distinct_id"] == "person@example.com"  # the verified email, as PostHog analytics uses
    data = body["message_data"]
    assert data["subject"] == "[Example] You're registered: Sample webinar"
    assert data["text"].startswith("Hi Sample,\n\nYou're registered for Sample webinar.")
    # Paragraphs only: the template supplies the page.
    assert data["html"].startswith("<p>") and "<html" not in data["html"] and "<!DOCTYPE" not in data["html"]
    assert (data["event_type"], data["message"], data["delivery_id"]) == ("registration.created", "registration_created", DELIVERY_ID)
    assert KEY not in json.dumps(body)


def test_eu_host():
    [call] = run({**POSTHOG, "native_region": "eu"})[1].calls
    assert call["url"] == "https://eu.posthog.com/api/projects/@current/messaging/send/email/"


@pytest.mark.parametrize("region", ["", "global", "us.i"])
def test_unknown_region_has_no_host(region):
    outcome, fake = run({**POSTHOG, "native_region": region})
    assert (outcome.kind, fake.calls) == ("dead", [])


def test_html_fragment_escapes_and_links():
    fragment = tx.to_html_fragment("Hi <b>you</b>\n\nJoin https://example.com/x.")
    assert fragment == '<p>Hi &lt;b&gt;you&lt;/b&gt;</p>\n<p>Join <a href="https://example.com/x">https://example.com/x</a>.</p>'
    assert tx.to_html("Hi") == "<!DOCTYPE html>\n<html><body>\n<p>Hi</p>\n</body></html>\n"


def test_preview_never_shows_the_key():
    assert posthog_email.preview_response("send", "POST", SEND_URL, {}) == {"message_id": "<returned id>", "replayed": False}
