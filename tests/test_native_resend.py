"""Resend native adapter: mocked send contract, idempotency, consent, identity, links and host policy.

No network: transports are fakes and socket connects raise. The request shape follows the Resend
send-email, idempotency-key, rate-limit and errors references fetched 2026-10-27
(docs/native-integrations.md); nothing here has been checked against a real Resend account.
"""
import json
import socket

import pytest
from django.db import transaction

import webinars.delivery
import webinars.ssrf
from tests.conftest import JOIN_URL, make_destination, make_registration
from tests.test_native_integrations import Fake, _claimed, body
from tests.test_transactional_email import mail_body
from webinars import native_integrations as ni
from webinars.crypto import encrypt_secret
from webinars.models import Destination, OutboxMessage
from webinars.native_providers import _transactional_email as tx
from webinars.native_providers import resend

pytestmark = pytest.mark.django_db

KEY = "re_SecretApiKeyValue_1234567890"
HOST = "api.resend.com"
SEND_URL = f"https://{HOST}/emails"
RESEND = {
    "native_provider": "resend",
    "native_region": "global",
    "native_api_key": KEY,
    "native_from_address": "events@example.com",
    "native_from_name": "Example Events",
    "native_reply_to": "hello@example.com",
    "native_subject_prefix": "[Example]",
    "native_send_ack": "confirmed",
    "native_processing_ack": "confirmed",
}
KW = dict(event_types=list(tx.EVENT_TYPES), allowed_groups=list(Destination.GROUPS), required_consents=[])
S = OutboxMessage.Status
DELIVERY_ID = ni.SAMPLE_BODY["delivery_metadata"]["deliveryId"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", HOST, "api.segment.io"]
    settings.PUBLIC_BASE_URL = "https://webinars.example.com"
    settings.PUBLIC_EVENT_URL_TEMPLATE = ""


def run(creds=RESEND, b=None, event_type="registration.created", fake=None):
    fake = fake or Fake()
    return ni.execute("resend", creds, b if b is not None else mail_body(), event_type, fake.ctx()), fake


def dest(**kw):
    return Destination(**{**KW, **kw})


# --- schema / credentials -----------------------------------------------------


def test_offered_and_schema_driven():
    assert dict(ni.PROVIDER_CHOICES)["resend"] == "Resend"
    assert ni.FIELDS["resend"][0].name == "region" and ni.FIELDS["resend"][0].choices == (("global", HOST),)
    assert ni.PROCESSING_ACK in ni.FIELDS["resend"] and tx.SEND_ACK in ni.FIELDS["resend"]
    assert ni.HOSTS["resend"] == {"global": HOST} and HOST in ni.VENDOR_HOSTS
    assert "resend" not in ni.ANALYTICS_PROVIDERS and "resend" in ni.NEEDS_REQUEST_TRANSPORT
    assert ni.supported_event_types("resend") == tx.EVENT_TYPES
    assert {f.name for f in ni.FIELDS["resend"] if f.secret} == {"api_key"}


def test_credential_is_set_only():
    rendered = repr(ni.config_of(RESEND, "resend")) + repr(ni.secrets_set(RESEND, "resend"))
    assert KEY not in rendered and ni.secrets_set(RESEND, "resend") == {"api_key": True}
    posted = {f.name: ni.config_of(RESEND, "resend").get(f.name, "") for f in ni.FIELDS["resend"]}
    posted["api_key"] = ""
    out, _ = ni.merge_credentials(RESEND, "resend", posted)
    assert out["native_api_key"] == KEY
    out, notes = ni.merge_credentials(RESEND, "ses", {"region": "eu-west-2"})
    assert KEY not in out.values() and notes


def test_valid_config():
    assert ni.validate("resend", RESEND, **KW) == {}
    assert ni.check_send_policy("resend", RESEND, dest()) is None
    minimal = {k: v for k, v in RESEND.items() if k not in ("native_from_name", "native_reply_to", "native_subject_prefix")}
    assert ni.validate("resend", minimal, **KW) == {}


@pytest.mark.parametrize("key", ["native_processing_ack", "native_send_ack", "native_from_address", "native_api_key"])
def test_required_fields(key):
    creds = {k: v for k, v in RESEND.items() if k != key}
    assert f"native-{key[len('native_'):]}" in ni.validate("resend", creds, **KW)
    assert ni.check_send_policy("resend", creds, dest()) == "native_config_invalid"


@pytest.mark.parametrize("field,value", [("from_address", "Example <events@example.com>"), ("reply_to", "nope"), ("from_name", "Bad <name>"), ("subject_prefix", "{event_title}")])
def test_address_and_prefix_formats(field, value):
    assert f"native-{field}" in ni.validate("resend", {**RESEND, f"native_{field}": value}, **KW)


def test_event_types_and_groups_policy():
    assert "event_types" in ni.validate("resend", RESEND, **{**KW, "event_types": ["attendance.checked_in"]})
    assert "allowed_groups" not in ni.validate("resend", RESEND, **{**KW, "allowed_groups": ["identity", "registration", "event"]})
    assert "allowed_groups" in ni.validate("resend", RESEND, **{**KW, "allowed_groups": ["identity", "event"]})
    assert "required_consents" not in ni.validate("resend", RESEND, **KW)  # consent purposes are the operator's choice


@pytest.mark.parametrize("region", ["eu", "us", ""])
def test_unknown_region_has_no_host(region):
    creds = {**RESEND, "native_region": region}
    assert ni.validate("resend", creds, **KW)
    assert ni.base_url("resend", creds) is None and ni.allowed_hosts("resend", creds) == frozenset()
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])


def test_host_must_be_operator_allowlisted(settings):
    settings.NATIVE_VENDOR_HOSTS_IMPLICIT = False  # operator opted out of implicit vendor hosts
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    assert any(HOST in m for m in ni.validate("resend", RESEND, **KW)["provider"])
    assert ni.check_send_policy("resend", RESEND, dest()) == "native_config_invalid"


def test_allowed_hosts_pins_exact_host():
    assert ni.allowed_hosts("resend", RESEND) == frozenset({HOST})


def test_without_request_transport_cannot_be_enabled(monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    assert any("request_json_bounded" in m for m in ni.validate("resend", RESEND, **KW)["provider"])
    assert ni.check_send_policy("resend", RESEND, dest()) == "native_transport_unavailable"


# --- request contract ---------------------------------------------------------


def test_exact_request():
    outcome, fake = run()
    assert outcome.kind == "delivered"
    [call] = fake.calls
    assert call["method"] == "POST" and call["url"] == SEND_URL
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["headers"]["Idempotency-Key"] == DELIVERY_ID
    assert call["headers"]["Content-Type"].startswith("application/json")
    assert set(call["json"]) == {"from", "to", "reply_to", "subject", "text", "html", "headers"}
    assert call["json"]["from"] == "Example Events <events@example.com>"
    assert call["json"]["to"] == ["person@example.com"] and call["json"]["reply_to"] == "hello@example.com"
    assert call["json"]["subject"] == "[Example] You're registered: Sample webinar"
    assert call["json"]["text"].startswith("Hi Sample,\n\nYou're registered for Sample webinar.")
    assert "Tue 3 Nov 2026, 14:00 GMT (Europe/London)" in call["json"]["text"]
    assert call["json"]["html"].startswith("<!DOCTYPE html>")
    assert call["json"]["headers"] == {"X-Webinar-Delivery-Id": DELIVERY_ID, "X-Webinar-Event-Type": "registration.created"}
    assert KEY not in json.dumps(call["json"])


def test_optional_settings_omitted_when_blank():
    creds = {k: v for k, v in RESEND.items() if k not in ("native_from_name", "native_reply_to", "native_subject_prefix")}
    [call] = run(creds)[1].calls
    assert call["json"]["from"] == "events@example.com" and "reply_to" not in call["json"]
    assert call["json"]["subject"] == "You're registered: Sample webinar"


@pytest.mark.parametrize(
    "event_type,attendance,subject",
    [
        ("reminder.due", None, "[Example] Reminder: Sample webinar starts Tue 3 Nov 2026, 14:00 GMT"),
        ("occurrence.rescheduled", None, "[Example] New time for Sample webinar"),
        ("occurrence.cancelled", None, "[Example] Cancelled: Sample webinar"),
        ("attendance.recorded", "attended", "[Example] Thanks for joining Sample webinar"),
        ("attendance.recorded", "no_show", "[Example] Sorry we missed you at Sample webinar"),
        ("follow_up.requested", None, "[Example] Following up on Sample webinar"),
    ],
)
def test_message_per_event_type(event_type, attendance, subject):
    b = mail_body()
    if attendance:
        b["registration"]["attendance"] = attendance
    outcome, fake = run(RESEND, b, event_type)
    assert outcome.kind == "delivered"
    assert fake.calls[0]["json"]["subject"] == subject
    assert fake.calls[0]["json"]["headers"]["X-Webinar-Event-Type"] == event_type


def test_recording_published_needs_a_recording_link():
    outcome, fake = run(RESEND, mail_body(), "recording.published")
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_recording_missing", [])
    b = mail_body()
    b["event"]["occurrence"]["recordingUrl"] = "https://videos.example.com/rec/1"
    outcome, fake = run(RESEND, b, "recording.published")
    assert outcome.kind == "delivered" and "https://videos.example.com/rec/1" in fake.calls[0]["json"]["text"]


@pytest.mark.parametrize("event_type", ["registration.updated", "attendance.checked_in"])
def test_unsupported_events_are_cancelled_before_any_call(event_type):
    outcome, fake = run(RESEND, mail_body(), event_type)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_event_unsupported", [])


def test_unknown_attendance_skipped():
    b = mail_body()
    b["registration"]["attendance"] = "weird"
    outcome, fake = run(RESEND, b, "attendance.recorded")
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_attendance_unknown", [])


# --- links / identity / consent -------------------------------------------------


def test_managed_room_link_wins_and_private_link_never_leaks():
    b = mail_body()
    b["event"]["occurrence"].update({"managedRoomUrl": "https://webinars.example.com/w/ROOM1/", "joinUrl": "https://zoom.example/j/PRIVATE1", "waitingRoomUrl": "https://zoom.example/w/PRIVATE2"})
    [call] = run(RESEND, b)[1].calls
    text = json.dumps(call["json"])
    assert "https://webinars.example.com/w/ROOM1/" in text and "PRIVATE" not in text


def test_private_join_link_used_only_when_the_body_carries_it():
    b = mail_body()
    b["event"]["occurrence"]["joinUrl"] = "https://zoom.example/j/PRIVATE1"
    assert "https://zoom.example/j/PRIVATE1" in run(RESEND, b)[1].calls[0]["json"]["text"]
    [call] = run(RESEND, mail_body())[1].calls
    assert "zoom.example" not in call["json"]["text"] and "https://webinars.example.com/api/v1/events/sample-webinar" in call["json"]["text"]


def test_no_email_skipped_before_any_call():
    b = mail_body()
    b["profile"].pop("email")
    b["identity"].pop("email")
    outcome, fake = run(RESEND, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_no_email", [])


def test_no_profile_group_uses_identity_email_and_there():
    b = mail_body()
    b.pop("profile")
    [call] = run(RESEND, b)[1].calls
    assert call["json"]["to"] == ["person@example.com"] and call["json"]["text"].startswith("Hi there,")


def test_cancelled_registration_only_gets_the_cancellation_notice():
    b = mail_body()
    b["registration"]["status"] = "cancelled"
    outcome, fake = run(RESEND, b, "reminder.due")
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_registration_cancelled", [])
    assert run(RESEND, b, "occurrence.cancelled")[0].kind == "delivered"


def test_unverified_identity_never_sent():
    for mutate in (lambda b: b["identity"].update(verified=False), lambda b: b["registration"].update(identityVerified=False)):
        b = mail_body()
        mutate(b)
        outcome, fake = run(RESEND, b)
        assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_identity_unverified", [])


def test_missing_delivery_id_is_dead():
    b = mail_body()
    b["delivery_metadata"] = {}
    outcome, fake = run(RESEND, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_delivery_id_missing", [])


def test_send_ack_unconfirmed_is_dead_without_a_call():
    outcome, fake = run({**RESEND, "native_send_ack": ""})
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_match_strategy_unconfirmed", [])


def test_invalid_stored_override_is_dead_without_a_call():
    outcome, fake = run({**RESEND, "native_body_registration_created": "{api_key}"})
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_invalid", [])


def test_request_size_bound_checked_before_the_call(monkeypatch):
    monkeypatch.setattr(tx, "MAX_REQUEST_BYTES", 100)
    outcome, fake = run()
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "send: payload_too_large", [])


# --- responses / failures / idempotency ----------------------------------------------


class StatusFake:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def ctx(self):
        def request(method, url, data, headers):
            self.calls.append({"url": url, "headers": dict(headers), "data": data})
            r = self.results.pop(0) if self.results else (200, b'{"id": "em_1"}')
            if isinstance(r, Exception):
                raise r
            return r

        return ni.NativeContext(post=None, request=request, renew=lambda: True)


def test_accepted_response_is_delivered_even_without_id():
    for raw in (b'{"id": "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"}', b"{}"):
        fake = StatusFake((200, raw))
        assert ni.execute("resend", RESEND, mail_body(), "registration.created", fake.ctx()).kind == "delivered"
        assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "result,kind,error",
    [
        ((429, b"", 42), "retry", "http_status: 429"),
        ((500, b""), "retry", "http_status: 500"),
        ((503, b"", 7), "retry", "http_status: 503"),
        ((302, b""), "retry", "redirect_not_followed: http 302"),
        ((400, b""), "dead", "http_status: 400"),
        ((401, b""), "dead", "http_status: 401"),
        ((403, b""), "dead", "http_status: 403"),
        ((409, b""), "dead", "http_status: 409"),
        ((422, b""), "dead", "http_status: 422"),
        (TimeoutError(), "retry", "timeout"),
        (ConnectionResetError(KEY), "retry", "transport_error: ConnectionResetError"),
        (ValueError(f"echo {KEY} person@example.com"), "retry", "transport_error: ValueError"),
    ],
)
def test_failures_never_echo_secrets(result, kind, error):
    fake = StatusFake(result)
    outcome = ni.execute("resend", RESEND, mail_body(), "registration.created", fake.ctx())
    assert (outcome.kind, outcome.error) == (kind, f"send: {error}")
    assert KEY not in outcome.error and "@" not in outcome.error
    if isinstance(result, tuple) and len(result) > 2:
        assert outcome.retry_after == result[2]


def test_retry_resends_the_same_idempotency_key_and_payload():
    first = run()[1].calls[0]
    again = run()[1].calls[0]
    assert first["headers"]["Idempotency-Key"] == again["headers"]["Idempotency-Key"] == DELIVERY_ID
    assert first["json"] == again["json"]
    assert len(DELIVERY_ID) <= resend.MAX_IDEMPOTENCY_KEY_CHARS


# --- preview ------------------------------------------------------------------


def test_preview_redacts_key_and_shows_rendered_message():
    [call] = ni.preview("resend", RESEND)
    assert call["url"] == SEND_URL and call["headers"]["Authorization"] == ni.REDACTED
    assert KEY not in repr(call)
    assert call["body"]["subject"] == "[Example] You're registered: Sample webinar"
    assert "Hi Sample," in call["body"]["text"] and "<p>Hi Sample,</p>" in call["body"]["html"]
    assert ni.preview_refusal("resend", RESEND) is None


def test_preview_empty_without_host():
    assert ni.preview("resend", {**RESEND, "native_region": "eu"}) == []
    assert ni.preview_refusal("resend", {**RESEND, "native_region": "eu"}) is None


@pytest.mark.parametrize(
    "creds, code",
    [
        ({**RESEND, "native_send_ack": ""}, "native_match_strategy_unconfirmed"),
        ({**RESEND, "native_body_registration_created": "{nope}"}, "native_config_invalid"),
        ({k: v for k, v in RESEND.items() if k != "native_from_address"}, "native_config_incomplete"),
    ],
)
def test_refused_preview_reports_bounded_code(creds, code):
    assert ni.preview("resend", creds) == []
    refusal = ni.preview_refusal("resend", creds)
    assert refusal == (code, ni.PREVIEW_REFUSALS[code])
    assert KEY not in repr(refusal) and "person@example.com" not in repr(refusal)


# --- delivery (host pinning, consent gate, links, policy at send time) ---------------


class RecordingRequest:
    """Stands in for ``ssrf.request_json_bounded``: applies its host/credential policy, records calls."""

    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, method, url, data, headers, timeout, total, max_bytes, allowed_hosts=None):
        host = webinars.ssrf._validated_target(url)[0]
        webinars.ssrf._check_native_request(method, host, headers, allowed_hosts)
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "data": data, "allowed": allowed_hosts})
        return self.status, b'{"id": "em_1"}', None, False


@pytest.fixture
def transport(monkeypatch):
    fake = RecordingRequest()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    return fake


def _deliver(msg):
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    return msg


def _enqueue(occurrence, creds, event_type, **dest_kw):
    from webinars.outbox import enqueue_event

    make_destination(event_types=[event_type], credentials_encrypted=encrypt_secret(json.dumps(creds)), **dest_kw)
    reg = make_registration(occurrence)
    with transaction.atomic():
        assert len(enqueue_event(event_type, registration=reg, occurrence=occurrence)) == 1
    [msg] = webinars.delivery.claim_batch("w1")
    return msg, reg


def test_e2e_confirmation_delivered_without_private_links(occurrence, transport, caplog):
    msg, reg = _claimed(occurrence, RESEND)
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED and msg.attempts == 1
    [call] = transport.calls
    assert call["url"] == SEND_URL and call["allowed"] == frozenset({HOST})
    assert call["headers"]["Authorization"] == f"Bearer {KEY}" and call["headers"]["Idempotency-Key"] == str(msg.id)
    sent = json.loads(call["data"])
    assert sent["to"] == [reg.person.email] and sent["headers"]["X-Webinar-Delivery-Id"] == str(msg.id)
    assert "Hi Pat," in sent["text"]
    assert JOIN_URL not in call["data"].decode() and "secret-waiting-token" not in call["data"].decode()
    assert f"https://webinars.example.com/api/v1/events/{occurrence.event.slug}" in sent["text"]
    assert f"/api/v1/occurrences/{occurrence.pk}/calendar.ics" in sent["text"]
    assert KEY not in caplog.text and KEY not in msg.last_error


def test_e2e_private_link_only_with_include_private_links(occurrence, transport):
    msg, _ = _claimed(occurrence, RESEND, include_private_links=True)
    _deliver(msg)
    sent = json.loads(transport.calls[0]["data"])
    assert f"Join link: {JOIN_URL}" in sent["text"]


def test_e2e_managed_room_link_replaces_provider_link(occurrence, transport):
    occurrence.waiting_room_enabled = True
    occurrence.waiting_room_code = "ROOMCODE1234"
    occurrence.save()
    msg, _ = _claimed(occurrence, RESEND, include_private_links=True)
    _deliver(msg)
    sent = json.loads(transport.calls[0]["data"])
    assert "Join link: https://webinars.example.com/w/ROOMCODE1234/" in sent["text"] and JOIN_URL not in sent["text"]


def test_e2e_reminder_uses_the_occurrence_timezone(occurrence, transport):
    msg, _ = _enqueue(occurrence, RESEND, "reminder.due")
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED
    sent = json.loads(transport.calls[0]["data"])
    expected = tx.format_local(occurrence.starts_at.isoformat(), occurrence.timezone)
    assert sent["subject"] == f"[Example] Reminder: {occurrence.event.public_title} starts {expected}"
    assert "(Europe/London)" in sent["text"]


@pytest.mark.parametrize("status,expected", [(503, S.PENDING), (429, S.PENDING), (400, S.DEAD), (422, S.DEAD)])
def test_e2e_failures(occurrence, transport, status, expected):
    transport.status = status
    msg, _ = _claimed(occurrence, RESEND)
    msg = _deliver(msg)
    assert msg.status == expected and msg.last_error == f"send: http_status: {status}" and msg.last_status_code == status


def test_e2e_operator_required_consent_blocks_before_the_call(occurrence, transport):
    msg, _ = _claimed(occurrence, RESEND)
    Destination.objects.filter(pk=msg.destination_id).update(required_consents=["sms"])
    msg = _deliver(msg)
    assert transport.calls == [] and msg.status == S.CANCELLED and msg.last_error == "consent_not_granted"


def test_e2e_event_type_change_after_enqueue_is_refused_at_send_time(occurrence, transport):
    msg, _ = _claimed(occurrence, RESEND)
    Destination.objects.filter(pk=msg.destination_id).update(event_types=["attendance.checked_in"])
    msg = _deliver(msg)
    assert transport.calls == [] and msg.status == S.DEAD and msg.last_error == "native_config_invalid"


def test_e2e_never_sent_to_another_host(occurrence, monkeypatch, transport, caplog):
    msg, _ = _claimed(occurrence, RESEND)
    monkeypatch.setattr(ni, "base_url", lambda provider, credentials: "https://api.segment.io")
    msg = _deliver(msg)
    assert transport.calls == [] and msg.status == S.DEAD and msg.last_error.endswith(": unsafe_destination")
    assert KEY not in caplog.text


def test_e2e_without_request_transport_dead(occurrence, monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    msg, _ = _claimed(occurrence, RESEND)
    msg = _deliver(msg)
    assert msg.status == S.DEAD and msg.last_error == "native_transport_unavailable"


def test_e2e_test_send_is_unsupported(occurrence, transport):
    msg, _ = _claimed(occurrence, RESEND)
    OutboxMessage.objects.filter(pk=msg.pk).update(event_type=webinars.delivery.NATIVE_TEST_EVENT_TYPE)
    msg.refresh_from_db()
    msg = _deliver(msg)
    assert transport.calls == [] and msg.status == S.CANCELLED and msg.last_error == "native_test_send_unsupported"


def test_body_helper_is_the_shared_sample():
    assert body()["identity"]["personId"] == mail_body()["identity"]["personId"]


def test_vendor_host_needs_no_operator_entry_by_default(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = []
    assert "provider" not in ni.validate("resend", RESEND, **KW)
    assert ni.check_send_policy("resend", RESEND, dest()) is None
