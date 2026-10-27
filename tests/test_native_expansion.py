"""Segment, Amplitude and GA4 native adapters: mocked request contracts, consent and redaction.

No network: every transport is a fake. These pin our request shapes to the contracts read from
the vendors' docs (see docs/native-integrations.md); they are not live or sandbox verification.
"""
import base64
import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db import transaction
from django.utils import timezone

import webinars.delivery
import webinars.ssrf
from tests.conftest import make_destination, make_registration
from tests.test_native_integrations import Fake, body
from webinars import native_integrations as ni
from webinars.crypto import encrypt_secret
from webinars.models import AttributionTouch, ConsentRecord, Destination, OutboxMessage

pytestmark = pytest.mark.django_db
S = OutboxMessage.Status

NEW_HOSTS = ["api.segment.io", "events.eu1.segmentapis.com", "api2.amplitude.com", "api.eu.amplitude.com", "www.google-analytics.com"]
ACK = {"native_processing_ack": "confirmed"}
SEG_KEY = "seg_write_key_123"
AMP_KEY = "amp_api_key_456"
GA_SECRET = "gaSecretValue_789"
SEGMENT = {"native_provider": "segment", "native_region": "us", "native_write_key": SEG_KEY, **ACK}
AMPLITUDE = {"native_provider": "amplitude", "native_region": "eu", "native_api_key": AMP_KEY, **ACK}
GA4 = {"native_provider": "ga4", "native_region": "global", "native_measurement_id": "G-ABC123XYZ", "native_api_secret": GA_SECRET, **ACK}
SECRETS = (SEG_KEY, AMP_KEY, GA_SECRET)

OCCURRED_S = 1767268800  # SAMPLE_BODY occurredAt 2026-01-01T12:00:00Z
NO_PROFILE_GROUPS = [g for g in Destination.GROUPS if g != "profile"]
KW = dict(event_types=["registration.created"], allowed_groups=NO_PROFILE_GROUPS, required_consents=["analytics"])


@pytest.fixture(autouse=True)
def vendor_allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", *NEW_HOSTS]


def granted(value=True):
    return {"granted": value, "disclosureVersion": "v1", "recordedAt": "2026-01-01T11:00:00Z", "source": "trusted_api", "serverVerified": True}


def ga_body(client="GA1.1.123456789.1700000000", analytics=True, advertising=False):
    b = body()
    b["consents"] = {"analytics": granted(analytics), "advertising": granted(advertising), "sms": granted(False)}
    b["attribution"] = {"firstTouch": None, "registrationTouch": {"parameters": {}, "identifiers": ({"gaClientId": client} if client else {}), "pageUrl": None, "referrer": None}}
    return b


def run(creds, b, event_type="registration.created", fake=None):
    fake = fake or Fake()
    return ni.execute(creds["native_provider"], creds, b, event_type, fake.ctx()), fake


# --- schema / GUI -------------------------------------------------------------


def test_new_providers_offered_and_schema_driven():
    labels = dict(ni.PROVIDER_CHOICES)
    assert labels["segment"] == "Segment" and labels["amplitude"] == "Amplitude" and labels["ga4"] == "Google Analytics 4"
    for p in ("segment", "amplitude", "ga4"):
        # The studio defaults the region from the first field's first choice.
        assert ni.FIELDS[p][0].name == "region" and ni.FIELDS[p][0].choices
        assert ni.PROCESSING_ACK in ni.FIELDS[p]
        assert set(ni.HOSTS[p].values()) <= ni.VENDOR_HOSTS
    assert {f.name for f in ni.FIELDS["ga4"] if f.secret} == {"api_secret"}
    assert ni.config_of(GA4, "ga4") == {"region": "global", "measurement_id": "G-ABC123XYZ", "processing_ack": "confirmed"}


@pytest.mark.parametrize("name", ["mixpanel", "rudderstack", "brevo", "activecampaign", "mailchimp", "salesforce", "pipedrive", "beehiiv"])
def test_formerly_deferred_providers_now_built(name):
    # Covered in depth by tests/test_native_remaining_analytics.py, test_native_brevo.py, test_native_activecampaign.py,
    # test_native_mailchimp.py, test_native_salesforce.py, test_native_pipedrive.py and test_native_beehiiv.py.
    creds = {"native_provider": name, **ACK}
    assert ni.provider_of(creds) == name and name in ni.FIELDS
    assert ni.validate(name, creds, **KW).get("provider") != ["Unknown provider."]
    out, _notes = ni.merge_credentials({}, name, {})
    assert out["native_provider"] == name


def test_every_registered_provider_is_schema_driven_and_pinned():
    """Shared studio/preview/delivery assumptions hold for every provider, including module-registered ones."""
    for provider, label in ni.PROVIDER_CHOICES:
        if not provider:
            continue
        assert label and ni.PROVIDER_LABELS[provider] == label
        fields = ni.FIELDS[provider]
        # The studio defaults the region from the first field's first choice.
        assert fields[0].name == "region" and fields[0].choices
        assert ni.PROCESSING_ACK in fields
        assert any(f.secret for f in fields)
        assert len({f.name for f in fields}) == len(fields)
        if provider in ni.REGISTRY:
            module = ni.REGISTRY[provider]
            assert module.PROVIDER == provider and callable(module.run)
            assert provider in ni.NEEDS_DESTINATION_HOSTS or provider in ni.HOSTS
        # No host without a configuration: nothing can be sent until the destination resolves.
        assert ni.allowed_hosts(provider, {"native_provider": provider}) == frozenset()
    assert {"activecampaign", "mailchimp", "salesforce", "pipedrive", "beehiiv"} <= set(ni.REGISTRY)


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize("creds", [SEGMENT, AMPLITUDE, GA4])
def test_valid_configs(creds):
    assert ni.validate(creds["native_provider"], creds, **KW) == {}


@pytest.mark.parametrize("creds", [SEGMENT, AMPLITUDE, GA4])
def test_processing_ack_required(creds):
    c = {k: v for k, v in creds.items() if k != "native_processing_ack"}
    assert "native-processing_ack" in ni.validate(c["native_provider"], c, **KW)


@pytest.mark.parametrize("creds", [AMPLITUDE, GA4])
def test_analytics_consent_required(creds):
    errors = ni.validate(creds["native_provider"], creds, **{**KW, "required_consents": []})
    assert "required_consents" in errors
    assert ni.check_send_policy(creds["native_provider"], creds, Destination(event_types=KW["event_types"], allowed_groups=KW["allowed_groups"], required_consents=[])) == "native_consent_policy_invalid"


def test_segment_consent_is_operator_choice():
    assert ni.validate("segment", SEGMENT, **{**KW, "required_consents": []}) == {}


def test_ga4_rejects_profile_group_and_needs_consents_attribution():
    errors = ni.validate("ga4", GA4, **{**KW, "allowed_groups": list(Destination.GROUPS)})
    assert any("must not receive personal data" in m for m in errors["allowed_groups"])
    groups = [g for g in NO_PROFILE_GROUPS if g not in ("consents", "attribution")]
    errors = ni.validate("ga4", GA4, **{**KW, "allowed_groups": groups})
    assert any("consents, attribution" in m for m in errors["allowed_groups"])


@pytest.mark.parametrize(
    "field,value",
    [("measurement_id", "UA-1234-1"), ("measurement_id", "g-abc123"), ("api_secret", "has space here"), ("api_secret", "a&b=c&d"), ("region", "eu")],
)
def test_ga4_field_validation(field, value):
    assert f"native-{field}" in ni.validate("ga4", {**GA4, f"native_{field}": value}, **KW)


def test_host_must_be_operator_allowlisted(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    errors = ni.validate("amplitude", AMPLITUDE, **KW)
    assert errors["provider"] == ["Ask your operator to add api.eu.amplitude.com to WEBHOOK_HOST_ALLOWLIST before enabling."]


# --- Segment ------------------------------------------------------------------


def test_segment_exact_requests():
    outcome, fake = run(SEGMENT, body())
    assert outcome.kind == "delivered"
    identify, track = fake.calls
    token = "Basic " + base64.b64encode(f"{SEG_KEY}:".encode()).decode()
    assert identify["url"] == "https://api.segment.io/v1/identify" and track["url"] == "https://api.segment.io/v1/track"
    assert identify["method"] == track["method"] == "POST"
    assert identify["headers"]["Authorization"] == track["headers"]["Authorization"] == token
    delivery = ni.SAMPLE_BODY["delivery_metadata"]["deliveryId"]
    assert identify["json"] == {
        "userId": ni.SAMPLE_BODY["identity"]["personId"],
        "messageId": ni._stable_uuid(delivery, "segment-identify"),
        "timestamp": "2026-01-01T12:00:00Z",
        "traits": {"email": "person@example.com", "firstName": "Sample", "lastName": "Person", "title": "Role"},
    }
    assert track["json"]["event"] == "webinar registered"
    assert track["json"]["messageId"] == ni._stable_uuid(delivery, "segment-track")
    assert track["json"]["properties"]["webinar_registration_id"] == ni.SAMPLE_BODY["registration"]["id"]
    assert SEG_KEY not in json.dumps([identify["json"], track["json"]])


def test_segment_without_profile_sends_only_track_and_eu_host():
    b = body()
    b.pop("profile")
    outcome, fake = run({**SEGMENT, "native_region": "eu"}, b)
    assert outcome.kind == "delivered"
    [track] = fake.calls
    assert track["url"] == "https://events.eu1.segmentapis.com/v1/track"
    assert "traits" not in json.dumps(track["json"]) and "person@example.com" not in json.dumps(track["json"])


def test_segment_retry_resends_same_message_ids():
    _, first = run(SEGMENT, body())
    _, second = run(SEGMENT, body())
    assert [c["json"]["messageId"] for c in first.calls] == [c["json"]["messageId"] for c in second.calls]


def test_segment_consent_withdrawn_mid_sequence_stops_before_track():
    fake = Fake()
    ctx = ni.NativeContext(post=fake.post, request=fake.request, renew=fake.renew, check=lambda: ni.Outcome("cancelled", "consent_not_granted_mid_sequence"))
    outcome = ni.execute("segment", SEGMENT, body(), "registration.created", ctx)
    assert outcome.kind == "cancelled" and outcome.error == "consent_not_granted_mid_sequence"
    assert [c["url"] for c in fake.calls] == ["https://api.segment.io/v1/identify"]


def test_segment_unverified_identity_not_sent():
    b = body()
    b["identity"]["verified"] = False
    outcome, fake = run(SEGMENT, b)
    assert outcome.kind == "cancelled" and outcome.error == "native_identity_unverified" and fake.calls == []


# --- Amplitude ----------------------------------------------------------------


def test_amplitude_exact_request():
    outcome, fake = run(AMPLITUDE, body())
    assert outcome.kind == "delivered"
    [call] = fake.calls
    assert call["url"] == "https://api.eu.amplitude.com/2/httpapi" and call["method"] == "POST"
    assert "Authorization" not in call["headers"]
    delivery = ni.SAMPLE_BODY["delivery_metadata"]["deliveryId"]
    assert call["json"]["api_key"] == AMP_KEY
    [event] = call["json"]["events"]
    assert event["user_id"] == ni.SAMPLE_BODY["identity"]["personId"]
    assert event["event_type"] == "webinar registered"
    assert event["time"] == OCCURRED_S * 1000
    assert event["insert_id"] == ni._stable_uuid(delivery, "amplitude-event")
    assert "device_id" not in event  # never fabricated
    sent = json.dumps(call["json"])
    assert "person@example.com" not in sent and "Sample" not in sent.replace("Sample webinar", "")


def test_amplitude_attendance_and_unknown_attendance_skipped():
    b = body()
    b["registration"]["attendance"] = "attended"
    outcome, fake = run({**AMPLITUDE, "native_region": "us"}, b, "attendance.recorded")
    assert outcome.kind == "delivered" and fake.calls[0]["url"] == "https://api2.amplitude.com/2/httpapi"
    assert fake.calls[0]["json"]["events"][0]["event_type"] == "webinar attended"
    outcome, fake = run(AMPLITUDE, body(), "attendance.recorded")
    assert outcome.kind == "cancelled" and fake.calls == []


@pytest.mark.parametrize("status,kind", [(429, "retry"), (503, "retry"), (400, "dead"), (413, "dead")])
def test_amplitude_status_mapping_without_secret(status, kind):
    outcome, _ = run(AMPLITUDE, body(), fake=Fake([status]))
    assert outcome.kind == kind and outcome.error == f"event: http_status: {status}" and AMP_KEY not in outcome.error


# --- GA4 ----------------------------------------------------------------------


def test_ga4_exact_request_no_pii():
    outcome, fake = run(GA4, ga_body())
    assert outcome.kind == "delivered"
    [call] = fake.calls
    parts = urlsplit(call["url"])
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "www.google-analytics.com", "/mp/collect")
    assert parse_qs(parts.query) == {"measurement_id": ["G-ABC123XYZ"], "api_secret": [GA_SECRET]}
    assert "Authorization" not in call["headers"]
    assert call["json"] == {
        "client_id": "123456789.1700000000",
        "consent": {"ad_user_data": "DENIED", "ad_personalization": "DENIED"},
        "timestamp_micros": OCCURRED_S * 1_000_000,
        "events": [
            {
                "name": "webinar_registered",
                "params": {
                    "webinar_event_type": "registration.created",
                    "webinar_event_id": ni.SAMPLE_BODY["event"]["id"],
                    "webinar_occurrence_id": ni.SAMPLE_BODY["event"]["occurrence"]["id"],
                    "webinar_registration_status": "registered",
                    "webinar_attendance": "unknown",
                },
            }
        ],
    }
    sent = json.dumps(call["json"])
    for pii in ("person@example.com", "Sample Person", "Person", "Example Co", ni.SAMPLE_BODY["identity"]["personId"], ni.SAMPLE_BODY["registration"]["id"]):
        assert pii not in sent
    assert GA_SECRET not in sent


def test_ga4_advertising_granted_only_on_explicit_grant():
    _, fake = run(GA4, ga_body(advertising=True))
    assert fake.calls[0]["json"]["consent"] == {"ad_user_data": "GRANTED", "ad_personalization": "GRANTED"}
    b = ga_body()
    b["consents"].pop("advertising")
    _, fake = run(GA4, b)
    assert fake.calls[0]["json"]["consent"] == {"ad_user_data": "DENIED", "ad_personalization": "DENIED"}


def test_ga4_plain_client_id_accepted_and_first_touch_fallback():
    b = ga_body(client=None)
    b["attribution"]["firstTouch"] = {"identifiers": {"gaClientId": "42.1700000001"}}
    _, fake = run(GA4, b)
    assert fake.calls[0]["json"]["client_id"] == "42.1700000001"


@pytest.mark.parametrize(
    "b,reason",
    [
        (ga_body(analytics=False), "native_analytics_consent_missing"),
        ({**ga_body(), "consents": {}}, "native_analytics_consent_missing"),
        ({k: v for k, v in ga_body().items() if k != "consents"}, "native_analytics_consent_missing"),
        (ga_body(client=None), "native_ga_client_id_missing"),
        (ga_body(client="person@example.com"), "native_ga_client_id_missing"),
        ({k: v for k, v in ga_body().items() if k != "attribution"}, "native_ga_client_id_missing"),
    ],
)
def test_ga4_consent_or_identifier_missing_sends_nothing(b, reason):
    outcome, fake = run(GA4, copy.deepcopy(b))
    assert outcome.kind == "cancelled" and outcome.error == reason and fake.calls == []


def test_ga4_transport_errors_never_carry_secret():
    for exc in (OSError(f"boom https://www.google-analytics.com/mp/collect?api_secret={GA_SECRET}"), RuntimeError(GA_SECRET)):
        outcome, _ = run(GA4, ga_body(), fake=Fake([exc]))
        assert outcome.kind == "retry" and GA_SECRET not in outcome.error


def test_redact_url_scrubs_query_secrets():
    url = f"https://www.google-analytics.com/mp/collect?measurement_id=G-ABC123XYZ&api_secret={GA_SECRET}"
    out = ni.redact_url(url)
    assert GA_SECRET not in out and "measurement_id=G-ABC123XYZ" in out and "api_secret=%5Bredacted%5D" in out
    assert ni.redact_url("https://api.segment.io/v1/track") == "https://api.segment.io/v1/track"


# --- preview ------------------------------------------------------------------


@pytest.mark.parametrize("creds", [SEGMENT, AMPLITUDE, GA4])
def test_preview_redacts_all_secrets(creds):
    calls = ni.preview(creds["native_provider"], creds)
    assert calls
    dumped = json.dumps(calls)
    for secret in SECRETS:
        assert secret not in dumped
    for c in calls:
        assert c["headers"].get("Authorization") in (None, ni.REDACTED)


def test_ga4_preview_url_and_body():
    [call] = ni.preview("ga4", GA4)
    assert call["url"] == "https://www.google-analytics.com/mp/collect?measurement_id=G-ABC123XYZ&api_secret=%5Bredacted%5D"
    assert "person@example.com" not in call["body_json"]
    assert ni.preview("ga4", {k: v for k, v in GA4.items() if k != "native_measurement_id"}) == []


# --- end to end through delivery.deliver --------------------------------------


class Transport:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def __call__(self, url, data, headers, timeout):
        self.calls.append({"url": url, "body": data, "headers": dict(headers)})
        return self.status, ""


@pytest.fixture
def transport(monkeypatch):
    fake = Transport()
    monkeypatch.setattr(webinars.delivery, "post_json_pinned", fake, raising=False)
    monkeypatch.setattr(webinars.ssrf, "post_json_pinned", fake)
    return fake


def _claimed_ga4(occurrence, client="GA1.1.555.1700000002"):
    from webinars.outbox import enqueue_event

    make_destination(credentials_encrypted=encrypt_secret(json.dumps(GA4)), allowed_groups=NO_PROFILE_GROUPS, required_consents=["analytics"])
    reg = make_registration(occurrence)
    ConsentRecord.objects.create(person=reg.person, registration=reg, purpose="analytics", granted=True, source="trusted_api", server_verified=True)
    AttributionTouch.objects.create(person=reg.person, registration=reg, touch_type=AttributionTouch.TouchType.REGISTRATION_TOUCH, identifiers={"gaClientId": client})
    with transaction.atomic():
        assert len(enqueue_event("registration.created", registration=reg, occurrence=occurrence)) == 1
    [msg] = webinars.delivery.claim_batch("w1")
    return msg, reg


def _deliver(msg):
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    return msg


def test_e2e_ga4_delivered_without_pii_or_logged_secret(occurrence, transport, caplog):
    msg, reg = _claimed_ga4(occurrence)
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED
    [call] = transport.calls
    assert call["url"].startswith("https://www.google-analytics.com/mp/collect?")
    raw = call["body"].decode() if isinstance(call["body"], bytes) else call["body"]
    sent = json.loads(raw)
    assert sent["client_id"] == "555.1700000002"
    assert reg.person.email not in raw and str(reg.person.id) not in raw
    assert GA_SECRET not in caplog.text


def test_e2e_ga4_failure_last_error_has_no_secret(occurrence, transport, caplog):
    transport.status = 500
    msg = _deliver(_claimed_ga4(occurrence)[0])
    assert msg.status == S.PENDING and msg.last_error == "collect: http_status: 500"
    assert GA_SECRET not in msg.last_error and GA_SECRET not in caplog.text


def _claimed_segment(occurrence):
    from webinars.outbox import enqueue_event

    make_destination(credentials_encrypted=encrypt_secret(json.dumps(SEGMENT)))
    reg = make_registration(occurrence)
    with transaction.atomic():
        assert len(enqueue_event("registration.created", registration=reg, occurrence=occurrence)) == 1
    [msg] = webinars.delivery.claim_batch("w1")
    return msg


def test_e2e_segment_pinned_to_configured_region_host(occurrence, transport):
    msg = _deliver(_claimed_segment(occurrence))
    assert msg.status == S.DELIVERED and transport.calls
    assert all(c["url"].startswith("https://api.segment.io/") for c in transport.calls)


@pytest.mark.parametrize("target", ["https://api2.amplitude.com", "https://events.eu1.segmentapis.com", "https://hooks.example.net"])
def test_e2e_segment_key_never_sent_to_other_host(target, occurrence, transport, monkeypatch, caplog):
    # Each target is operator-allowlisted, and the first two are static vendor hosts that the
    # old VENDOR_HOSTS pinning admitted; delivery now pins the destination's own host only.
    msg = _claimed_segment(occurrence)
    monkeypatch.setattr(ni, "base_url", lambda provider, credentials: target)
    msg = _deliver(msg)
    assert transport.calls == []
    assert msg.status == S.DEAD and msg.last_error.endswith(": unsafe_destination")
    assert SEG_KEY not in msg.last_error and SEG_KEY not in caplog.text


def test_e2e_ga4_withdrawn_analytics_consent_sends_nothing(occurrence, transport):
    msg, reg = _claimed_ga4(occurrence)
    ConsentRecord.objects.filter(person=reg.person).update(withdrawn_at=timezone.now())
    msg = _deliver(msg)
    assert msg.status == S.CANCELLED and msg.last_error == "consent_not_granted" and transport.calls == []
