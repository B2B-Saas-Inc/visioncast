"""Review findings N1-N4 (.runs/completion-review-result.md), native test sends and the MCP scope hint.

Mocked only: no network, no vendor accounts. Request shapes follow the vendor docs cited in
docs/native-integrations.md; they are not sandbox-verified.
"""
import copy
import datetime
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db import transaction
from django.utils import timezone

import webinars.delivery as delivery
import webinars.ssrf
from tests.test_native_integrations import ATTIO, CUSTOMERIO, HUBSPOT, VENDOR_HOSTS, Fake, _claimed, body
from webinars import native_integrations as ni
from webinars.models import ConsentRecord, Destination, OutboxMessage

pytestmark = pytest.mark.django_db
S = OutboxMessage.Status
OK = dict(event_types=["registration.created"], allowed_groups=["identity", "registration", "event"], required_consents=[])


@pytest.fixture(autouse=True)
def vendor_allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = list(VENDOR_HOSTS)


# ---------------------------------------------------------------- N1 Customer.io timestamp


def test_customerio_event_timestamp_is_integer_unix_seconds():
    fake = Fake()
    out = ni.execute("customerio", CUSTOMERIO, body(), "registration.created", fake.ctx())
    assert out.kind == "delivered"
    event = [c["json"] for c in fake.calls if c["json"].get("action") == "event"][0]
    expected = int(datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc).timestamp())
    assert event["timestamp"] == expected and type(event["timestamp"]) is int


def test_customerio_timestamp_omitted_when_unparsable():
    fake = Fake()
    ni.execute("customerio", CUSTOMERIO, body(occurredAt="not a date"), "registration.created", fake.ctx())
    event = [c["json"] for c in fake.calls if c["json"].get("action") == "event"][0]
    assert "timestamp" not in event


@pytest.mark.parametrize("value,expected", [("1970-01-01T00:01:00Z", 60), ("1970-01-01T00:01:00+00:00", 60), (None, None), ("", None), ("x", None)])
def test_unix_seconds(value, expected):
    assert ni.unix_seconds(value) == expected


# ---------------------------------------------------------------- N2 matching strategy


EMAIL_HS = {**HUBSPOT, "native_person_match": "email", "native_email_merge_ack": "confirmed"}
EMAIL_ATTIO = {**ATTIO, "native_person_match": "email", "native_email_merge_ack": "confirmed"}


def _json_responses(n):
    return [(200, {"results": [{"id": str(i)}], "data": {"id": {"record_id": f"r{i}"}}}) for i in range(n)]


def test_hubspot_email_mode_upserts_by_email_documented_route():
    fake = Fake(_json_responses(6))
    out = ni.execute("hubspot", EMAIL_HS, body(), "registration.created", fake.ctx())
    assert out.kind == "delivered"
    first = fake.calls[0]
    assert first["method"] == "POST" and first["url"] == "https://api.hubapi.com/crm/v3/objects/contacts/batch/upsert"
    [inp] = first["json"]["inputs"]
    assert inp["idProperty"] == "email" and inp["id"] == "person@example.com"
    assert inp["properties"]["email"] == "person@example.com"
    # Our person id is still recorded when a property is configured, but it is not the match key.
    assert inp["properties"]["webinar_person_id"] == ni.SAMPLE_BODY["identity"]["personId"]


def test_hubspot_custom_mode_matches_on_custom_property():
    fake = Fake(_json_responses(6))
    ni.execute("hubspot", HUBSPOT, body(), "registration.created", fake.ctx())
    [inp] = fake.calls[0]["json"]["inputs"]
    assert inp["idProperty"] == "webinar_person_id" and inp["id"] == ni.SAMPLE_BODY["identity"]["personId"]


def test_custom_mode_email_collision_fails_closed():
    fake = Fake([(409, {"message": "Contact already exists"})])
    out = ni.execute("hubspot", HUBSPOT, body(), "registration.created", fake.ctx())
    assert (out.kind, out.error) == ("dead", "contact: http_status: 409") and len(fake.calls) == 1


def test_attio_email_mode_asserts_on_email_addresses():
    fake = Fake(_json_responses(6))
    out = ni.execute("attio", EMAIL_ATTIO, body(org_ids=()), "registration.created", fake.ctx())
    assert out.kind == "delivered"
    person = fake.calls[0]
    assert person["method"] == "PUT"
    parts = urlsplit(person["url"])
    assert parts.path == "/v2/objects/people/records"
    assert parse_qs(parts.query) == {"matching_attribute": ["email_addresses"]}
    assert person["json"]["data"]["values"]["email_addresses"] == ["person@example.com"]


def test_attio_custom_mode_asserts_on_custom_attribute():
    fake = Fake(_json_responses(6))
    ni.execute("attio", ATTIO, body(org_ids=()), "registration.created", fake.ctx())
    assert parse_qs(urlsplit(fake.calls[0]["url"]).query) == {"matching_attribute": ["webinar_person_id"]}


@pytest.mark.parametrize("creds", [HUBSPOT, ATTIO])
def test_email_mode_without_operator_confirmation_never_writes(creds):
    unconfirmed = {**creds, "native_person_match": "email"}
    unconfirmed.pop("native_email_merge_ack", None)
    assert "native-email_merge_ack" in ni.validate(creds["native_provider"], unconfirmed, **OK)
    fake = Fake()
    out = ni.execute(creds["native_provider"], unconfirmed, body(), "registration.created", fake.ctx())
    assert (out.kind, out.error) == ("dead", "native_match_strategy_unconfirmed") and fake.calls == []


@pytest.mark.parametrize("creds", [HUBSPOT, ATTIO])
def test_missing_strategy_never_writes(creds):
    loose = {k: v for k, v in creds.items() if k != "native_person_match"}
    assert "native-person_match" in ni.validate(creds["native_provider"], loose, **OK)
    fake = Fake()
    assert ni.execute(creds["native_provider"], loose, body(), "registration.created", fake.ctx()).kind == "dead"
    assert fake.calls == []


def test_custom_mode_requires_custom_id_field():
    no_prop = {k: v for k, v in HUBSPOT.items() if k != "native_contact_id_property"}
    assert "native-contact_id_property" in ni.validate("hubspot", no_prop, **OK)


def test_email_mode_without_email_is_skipped():
    b = body()
    b["profile"] = dict(b["profile"], email=None)
    fake = Fake()
    out = ni.execute("hubspot", EMAIL_HS, b, "registration.created", fake.ctx())
    assert (out.kind, out.error) == ("cancelled", "native_no_email") and fake.calls == []


def test_unverified_identity_never_matched_by_email():
    b = body()
    b["identity"] = dict(b["identity"], verified=False)
    fake = Fake()
    out = ni.execute("hubspot", EMAIL_HS, b, "registration.created", fake.ctx())
    assert out.kind == "cancelled" and fake.calls == []


# ---------------------------------------------------------------- N3 operator acknowledgement, no implicit legal policy


@pytest.mark.parametrize("creds", [HUBSPOT, ATTIO, CUSTOMERIO])
def test_processing_acknowledgement_required(creds):
    unacked = {k: v for k, v in creds.items() if k != "native_processing_ack"}
    assert "native-processing_ack" in ni.validate(creds["native_provider"], unacked, **OK)


@pytest.mark.parametrize("creds", [HUBSPOT, ATTIO, CUSTOMERIO])
def test_crm_consent_requirement_is_operator_choice(creds, monkeypatch):
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", lambda *a, **k: None, raising=False)
    # No consent purpose is forced for CRM/email tools; the operator's ack + settings decide.
    assert "required_consents" not in ni.validate(creds["native_provider"], creds, **OK)


# ---------------------------------------------------------------- N4 per-step send-time recheck


class FakeRequest:
    def __init__(self, on_first=None):
        self.calls, self.on_first = [], on_first

    def __call__(self, method, url, data, headers, timeout, total, max_bytes, allowed_hosts=None):
        self.calls.append(url)
        if len(self.calls) == 1 and self.on_first:
            self.on_first()
        return 200, json.dumps({"results": [{"id": "1"}]}).encode(), None, False


def _hubspot_message(occurrence, monkeypatch, on_first_factory):
    msg, reg = _claimed(occurrence, HUBSPOT, required_consents=["analytics"])
    fake = FakeRequest(on_first_factory(msg, reg))
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    return msg, fake


def test_consent_withdrawn_mid_sequence_stops_before_next_call(occurrence, monkeypatch):
    msg, fake = _hubspot_message(
        occurrence,
        monkeypatch,
        lambda m, reg: lambda: ConsentRecord.objects.filter(person=reg.person).update(withdrawn_at=timezone.now()),
    )
    delivery.deliver(msg)
    msg.refresh_from_db()
    assert len(fake.calls) == 1  # the contact write already made is not recalled
    assert msg.status == S.CANCELLED and msg.last_error == "consent_not_granted_mid_sequence"
    assert msg.payload == {"purged": True}


def test_required_consents_tightened_mid_sequence_stops(occurrence, monkeypatch):
    msg, fake = _hubspot_message(
        occurrence,
        monkeypatch,
        lambda m, reg: lambda: Destination.objects.filter(pk=m.destination_id).update(required_consents=["analytics", "sms"]),
    )
    delivery.deliver(msg)
    msg.refresh_from_db()
    assert len(fake.calls) == 1 and msg.status == S.CANCELLED and msg.last_error == "consent_not_granted_mid_sequence"


def test_destination_disabled_mid_sequence_parks(occurrence, monkeypatch):
    msg, fake = _hubspot_message(
        occurrence, monkeypatch, lambda m, reg: lambda: Destination.objects.filter(pk=m.destination_id).update(enabled=False)
    )
    delivery.deliver(msg)
    msg.refresh_from_db()
    assert len(fake.calls) == 1 and msg.status == S.PENDING and msg.last_error == "destination_disabled_mid_sequence"


def test_full_sequence_when_nothing_changes(occurrence, monkeypatch):
    msg, fake = _hubspot_message(occurrence, monkeypatch, lambda m, reg: None)
    delivery.deliver(msg)
    msg.refresh_from_db()
    # contact, participation, contact->participation association (no verified org id here)
    assert msg.status == S.DELIVERED and len(fake.calls) == 3


# ---------------------------------------------------------------- native test send is explicitly unsupported


def test_native_test_send_is_cancelled_explicitly_not_faked(monkeypatch):
    from tests.conftest import make_destination
    from webinars.crypto import encrypt_secret
    from webinars.mcp_config import TEST_EVENT_TYPE, test_fixture_payload

    assert TEST_EVENT_TYPE == delivery.NATIVE_TEST_EVENT_TYPE
    fake = FakeRequest()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    dest = make_destination(credentials_encrypted=encrypt_secret(json.dumps(HUBSPOT)))
    with transaction.atomic():
        OutboxMessage.objects.create(destination=dest, event_type=TEST_EVENT_TYPE, payload=test_fixture_payload(dest, "x"))
    [msg] = delivery.claim_batch("w1")
    delivery.deliver(msg)
    msg.refresh_from_db()
    assert msg.status == S.CANCELLED and msg.last_error == "native_test_send_unsupported" and fake.calls == []
    assert msg.delivered_at is None


# ---------------------------------------------------------------- MCP scope hint


def test_mcp_scope_hint_follows_tool_registry():
    from webinars.studio.views_settings import _mcp_scopes

    scopes = _mcp_scopes()
    for s in ("public_read", "content_write", "attendance_write", "reporting", "config_read", "publication", "delivery_admin", "keys_admin", "team_admin"):
        assert s in scopes
    for s in ("register", "proxy", "trusted"):
        assert s not in scopes


def test_every_scope_has_help_text():
    from webinars.models import ApiKey
    from webinars.studio.views_settings import SCOPE_HELP

    assert set(ApiKey.Scope.values) <= set(SCOPE_HELP)


def test_sample_body_unchanged():
    # Guard: the fixtures above rely on SAMPLE_BODY's email and occurredAt.
    b = copy.deepcopy(ni.SAMPLE_BODY)
    assert b["profile"]["email"] == "person@example.com" and b["occurredAt"] == "2026-01-01T12:00:00Z"
