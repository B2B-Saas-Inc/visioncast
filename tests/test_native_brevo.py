"""Brevo native adapter: mocked contact/event contracts, consent, identity, data minimisation and host policy.

No network: transports are fakes and socket connects raise. Request shapes follow the Brevo
create-contact and create-event references as fetched 2026-10-25 (docs/native-integrations.md);
nothing here has been checked against a real Brevo account or sandbox.
"""
import socket

import pytest

import webinars.delivery
import webinars.ssrf
from tests.test_native_integrations import Fake, _claimed, body
from webinars import native_integrations as ni
from webinars.models import Destination, OutboxMessage

pytestmark = pytest.mark.django_db

KEY = "xkeysib-brevoSecretValue123"
BREVO = {
    "native_provider": "brevo",
    "native_region": "global",
    "native_api_key": KEY,
    "native_contact_sync": "upsert",
    "native_email_merge_ack": "confirmed",
    "native_contact_attributes": "firstName=FIRSTNAME,lastName=LASTNAME,company=COMPANY,jobTitle=JOB_TITLE",
    "native_processing_ack": "confirmed",
}
EVENTS_ONLY = {**BREVO, "native_contact_sync": "events"}
KW = dict(event_types=["registration.created"], allowed_groups=list(Destination.GROUPS), required_consents=[])
S = OutboxMessage.Status
FORBIDDEN_CONTACT_KEYS = {"listIds", "unlinkListIds", "emailBlacklisted", "smsBlacklisted", "smtpBlacklistSender", "forceMerge", "ext_id", "SMS", "WHATSAPP"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", "api.brevo.com", "api.segment.io"]


def run(creds=BREVO, b=None, event_type="registration.created", fake=None):
    fake = fake or Fake()
    return ni.execute("brevo", creds, b if b is not None else body(), event_type, fake.ctx()), fake


def dest(**kw):
    return Destination(**{**KW, **kw})


# --- schema / credentials -----------------------------------------------------


def test_offered_and_schema_driven():
    assert dict(ni.PROVIDER_CHOICES)["brevo"] == "Brevo"
    assert ni.FIELDS["brevo"][0].name == "region" and ni.FIELDS["brevo"][0].choices
    assert ni.PROCESSING_ACK in ni.FIELDS["brevo"]
    assert ni.HOSTS["brevo"] == {"global": "api.brevo.com"} and "api.brevo.com" in ni.VENDOR_HOSTS
    assert "brevo" not in ni.ANALYTICS_PROVIDERS  # consent purposes are the operator's choice


def test_credential_is_set_only():
    rendered = repr(ni.config_of(BREVO, "brevo")) + repr(ni.secrets_set(BREVO, "brevo"))
    assert KEY not in rendered and ni.secrets_set(BREVO, "brevo") == {"api_key": True}
    posted = {f.name: ni.config_of(BREVO, "brevo").get(f.name, "") for f in ni.FIELDS["brevo"]}
    posted["api_key"] = ""
    out, _ = ni.merge_credentials(BREVO, "brevo", posted)
    assert out["native_api_key"] == KEY  # blank keeps the stored value
    out, notes = ni.merge_credentials(BREVO, "segment", {"region": "us", "write_key": ""})
    assert KEY not in out.values() and notes


def test_valid_config():
    assert ni.validate("brevo", BREVO, **KW) == {}
    assert ni.validate("brevo", EVENTS_ONLY, **KW) == {}
    assert ni.check_send_policy("brevo", BREVO, dest()) is None


@pytest.mark.parametrize("key", ["native_processing_ack", "native_email_merge_ack", "native_contact_sync", "native_api_key"])
def test_required_fields(key):
    creds = {k: v for k, v in BREVO.items() if k != key}
    assert f"native-{key[len('native_'):]}" in ni.validate("brevo", creds, **KW)
    assert ni.check_send_policy("brevo", creds, dest()) == "native_config_invalid"


@pytest.mark.parametrize("value", ["not-confirmed", "yes"])
def test_email_merge_ack_must_be_confirmed(value):
    creds = {**BREVO, "native_email_merge_ack": value}
    assert "native-email_merge_ack" in ni.validate("brevo", creds, **KW)
    for c in (creds, {k: v for k, v in BREVO.items() if k != "native_email_merge_ack"}):
        outcome, fake = run(c)
        assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_match_strategy_unconfirmed", [])


@pytest.mark.parametrize(
    "mapping",
    [
        "phone=PHONE",  # not a canonical allowlisted source
        "email=EMAIL",
        "customField=X",
        "firstName=firstname",  # targets are uppercase existing attributes
        "firstName=FIRST NAME",
        "firstName=SMS",  # reserved identifier/channel attribute
        "firstName=EMAILBLACKLISTED",
        "firstName=EXT_ID",
        "firstName=A,firstName=B",  # duplicate source
        "firstName=NAME,lastName=NAME",  # duplicate target
        "firstName=A;lastName=B",
    ],
)
def test_attribute_mapping_allowlist(mapping):
    creds = {**BREVO, "native_contact_attributes": mapping}
    assert "native-contact_attributes" in ni.validate("brevo", creds, **KW)
    outcome, fake = run(creds)
    assert outcome.kind == "dead" and fake.calls == []


@pytest.mark.parametrize("region", ["eu", "us", ""])
def test_unknown_region_has_no_host(region):
    creds = {**BREVO, "native_region": region}
    assert ni.validate("brevo", creds, **KW)
    assert ni.base_url("brevo", creds) is None and ni.allowed_hosts("brevo", creds) == frozenset()
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])


def test_host_must_be_operator_allowlisted(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    assert any("api.brevo.com" in m for m in ni.validate("brevo", BREVO, **KW)["provider"])
    assert ni.check_send_policy("brevo", BREVO, dest()) == "native_config_invalid"


def test_allowed_hosts_pins_exact_host():
    assert ni.allowed_hosts("brevo", BREVO) == frozenset({"api.brevo.com"})


# --- request contract ---------------------------------------------------------


def test_exact_requests():
    outcome, fake = run()
    assert outcome.kind == "delivered"
    contact, event = fake.calls
    for call in (contact, event):
        assert call["method"] == "POST" and call["url"].startswith("https://api.brevo.com/v3/")
        assert call["headers"]["api-key"] == KEY and "Authorization" not in call["headers"]
        assert call["headers"]["Content-Type"].startswith("application/json")
    assert contact["url"] == "https://api.brevo.com/v3/contacts"
    assert contact["json"] == {
        "email": "person@example.com",
        "updateEnabled": True,
        "attributes": {"FIRSTNAME": "Sample", "LASTNAME": "Person", "COMPANY": "Example Co", "JOB_TITLE": "Role"},
    }
    assert event["url"] == "https://api.brevo.com/v3/events"
    assert event["json"]["event_name"] == "webinar_registration_created"
    assert event["json"]["identifiers"] == {"email_id": "person@example.com"}
    assert event["json"]["event_date"] == ni.SAMPLE_BODY["occurredAt"]
    props = event["json"]["event_properties"]
    assert props["registration_id"] == ni.SAMPLE_BODY["registration"]["id"]
    assert props["occurrence_id"] == ni.SAMPLE_BODY["event"]["occurrence"]["id"]
    assert props["occurrence_starts_at"] == ni.SAMPLE_BODY["event"]["occurrence"]["startsAt"]
    assert props["delivery_id"] == ni.SAMPLE_BODY["delivery_metadata"]["deliveryId"]
    assert props["person_id"] == ni.SAMPLE_BODY["identity"]["personId"]
    assert props["occurred_at"] == ni.SAMPLE_BODY["occurredAt"]


@pytest.mark.parametrize("event_type,attendance,name", [
    ("registration.updated", "unknown", "webinar_registration_updated"),
    ("attendance.checked_in", "unknown", "webinar_attendance_checked_in"),
    ("attendance.recorded", "attended", "webinar_attendance_recorded"),
    ("attendance.recorded", "no_show", "webinar_attendance_recorded"),
])
def test_event_names_are_deterministic_and_valid(event_type, attendance, name):
    b = body()
    b["registration"]["attendance"] = attendance
    event = run(EVENTS_ONLY, b, event_type)[1].calls[-1]["json"]
    assert event["event_name"] == name and len(name) <= 255
    assert event["event_properties"]["registration_attendance"] == attendance


def test_unknown_attendance_skipped():
    b = body()
    b["registration"]["attendance"] = "weird"
    outcome, fake = run(BREVO, b, "attendance.recorded")
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_attendance_unknown", [])


def test_never_lists_blocklists_force_merge_ext_id_or_sms():
    b = body()
    b["profile"].update({"phone": "+15550100", "sms": "+15550100", "listIds": [1], "emailBlacklisted": False, "customField": "x"})
    for call in run(BREVO, b)[1].calls:
        text = repr(call["json"])
        assert not FORBIDDEN_CONTACT_KEYS & set(call["json"])
        assert not FORBIDDEN_CONTACT_KEYS & set(call["json"].get("attributes", {}))
        assert "+15550100" not in text and "customField" not in text and "forceMerge" not in text
        assert "/sms" not in call["url"] and "/lists" not in call["url"]
    contact = run(BREVO, b)[1].calls[0]["json"]
    assert set(contact) <= {"email", "attributes", "updateEnabled"} and contact["updateEnabled"] is True


def test_events_only_mode_never_writes_contact():
    (event,) = run(EVENTS_ONLY)[1].calls
    assert event["url"].endswith("/v3/events")


def test_email_only_identifier_no_ext_id_cross_merge():
    contact, event = run()[1].calls
    assert set(event["json"]["identifiers"]) == {"email_id"}
    assert "ext_id" not in contact["json"] and "EXT_ID" not in contact["json"].get("attributes", {})


def test_empty_mapping_sends_email_only():
    contact, _ = run({**BREVO, "native_contact_attributes": ""})[1].calls
    assert contact["json"] == {"email": "person@example.com", "updateEnabled": True}


def test_partial_mapping_only_maps_listed_fields():
    contact, _ = run({**BREVO, "native_contact_attributes": "firstName=FIRSTNAME"})[1].calls
    assert contact["json"]["attributes"] == {"FIRSTNAME": "Sample"}


# --- groups / private links / identity / consent -------------------------------


def test_no_contact_without_profile_group_event_uses_identity_email():
    b = body()
    b.pop("profile")
    (event,) = run(BREVO, b)[1].calls
    assert event["url"].endswith("/v3/events") and event["json"]["identifiers"] == {"email_id": "person@example.com"}


def test_no_email_skipped_before_any_call():
    b = body()
    b["profile"].pop("email")
    b["identity"].pop("email")
    outcome, fake = run(BREVO, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_no_email", [])


def test_missing_groups_not_sent_and_no_profile_data_in_event():
    b = body()
    b.pop("delivery_metadata")
    b.pop("event")
    b["delivery_metadata"] = {"deliveryId": ni.SAMPLE_BODY["delivery_metadata"]["deliveryId"]}
    props = run(EVENTS_ONLY, b)[1].calls[0]["json"]["event_properties"]
    assert not any(k.startswith(("event_", "occurrence_")) for k in props)
    full = run(EVENTS_ONLY)[1].calls[0]["json"]["event_properties"]
    assert "Sample" not in repr(full).replace("Sample webinar", "") and "Example Co" not in repr(full) and "@" not in repr(full)


def test_private_links_and_urls_never_sent():
    b = body()
    b["event"]["occurrence"].update(
        {"joinUrl": "https://zoom.example/j/SECRET1", "waitingRoomUrl": "https://zoom.example/w/SECRET2", "managedRoomUrl": "https://w.example/w/SECRET3/", "recordingUrl": "https://rec.example/SECRET4"}
    )
    b["event"]["heroImageUrl"] = "https://img.example/SECRET5"
    for call in run(BREVO, b)[1].calls:
        assert "SECRET" not in repr(call["json"]) and "https://" not in repr(call["json"])


def test_event_properties_flat_and_keys_valid():
    b = body()
    b["event"]["title"] = {"nested": "x"}
    props = run(EVENTS_ONLY, b)[1].calls[0]["json"]["event_properties"]
    assert "event_title" not in props
    assert all(isinstance(v, (str, int, float, bool)) for v in props.values())
    assert all(ni._BREVO_NAME.match(k) for k in props)


def test_nested_contact_attribute_refused_before_any_call():
    b = body()
    b["profile"]["firstName"] = {"given": "Sample"}
    outcome, fake = run(BREVO, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "contact: payload_unsupported", [])


def test_unverified_identity_never_sent():
    for mutate in (lambda b: b["identity"].update(verified=False), lambda b: b["registration"].update(identityVerified=False)):
        b = body()
        mutate(b)
        outcome, fake = run(BREVO, b)
        assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_identity_unverified", [])


def test_missing_delivery_id_is_dead():
    b = body()
    b["delivery_metadata"] = {}
    outcome, fake = run(BREVO, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_delivery_id_missing", [])


def test_unparseable_timestamp_omitted_not_invented():
    event = run(EVENTS_ONLY, body(occurredAt="not-a-date"))[1].calls[0]["json"]
    assert "event_date" not in event


def test_consent_rechecked_before_every_step():
    """The existing per-step check (delivery's consent/erasure re-check) stops the event after the contact."""
    fake = Fake()
    stop = ni.Outcome("cancelled", "consent_not_granted_mid_sequence")
    ctx = ni.NativeContext(post=fake.post, request=fake.request, renew=fake.renew, check=lambda: stop)
    outcome = ni.execute("brevo", BREVO, body(), "registration.created", ctx)
    assert outcome == stop and [c["url"] for c in fake.calls] == ["https://api.brevo.com/v3/contacts"]


# --- byte limit ---------------------------------------------------------------


def _sized(target):
    b = body()
    b["event"]["title"] = "x"
    base = len(ni._encode(run(BREVO, b)[1].calls[1]["json"]["event_properties"]))
    b["event"]["title"] = "x" * (1 + target - base)
    return b


def test_event_properties_byte_limit_fails_before_first_send():
    limit = ni.BREVO_MAX_PROPERTIES_BYTES
    outcome, fake = run(BREVO, _sized(limit))
    assert outcome.kind == "delivered" and len(ni._encode(fake.calls[1]["json"]["event_properties"])) == limit
    outcome, fake = run(BREVO, _sized(limit + 1))
    # Checked before the contact call: no contact write ahead of a refused event.
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "event: payload_too_large", [])
    b = body()
    b["event"]["title"] = "é" * (limit // 2)  # UTF-8 bytes, not characters
    outcome, fake = run(BREVO, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "event: payload_too_large", [])


# --- responses / failures -------------------------------------------------------


class StatusFake:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def ctx(self):
        def post(url, data, headers):
            self.calls.append(url)
            r = self.results.pop(0) if self.results else (204, "")
            if isinstance(r, Exception):
                raise r
            return r

        return ni.NativeContext(post=post, request=None, renew=lambda: True)


@pytest.mark.parametrize("contact_status", [201, 204])
def test_contact_created_or_updated_then_event_posted(contact_status):
    fake = StatusFake((contact_status, ""), (204, ""))
    assert ni.execute("brevo", BREVO, body(), "registration.created", fake.ctx()).kind == "delivered"
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    "result,kind,error",
    [
        ((429, "", 42), "retry", "http_status: 429"),
        ((500, ""), "retry", "http_status: 500"),
        ((503, "", 7), "retry", "http_status: 503"),
        ((302, ""), "retry", "redirect_not_followed: http 302"),
        ((400, ""), "dead", "http_status: 400"),
        ((401, ""), "dead", "http_status: 401"),
        ((403, ""), "dead", "http_status: 403"),
        (TimeoutError(), "retry", "timeout"),
        (ConnectionResetError(KEY), "retry", "transport_error: ConnectionResetError"),
        (ValueError(f"echo {KEY} person@example.com"), "retry", "transport_error: ValueError"),
    ],
)
@pytest.mark.parametrize("step", ["contact", "event"])
def test_failures_stop_and_never_echo_secrets(step, result, kind, error):
    results = [result] if step == "contact" else [(201, ""), result]
    fake = StatusFake(*results)
    outcome = ni.execute("brevo", BREVO, body(), "registration.created", fake.ctx())
    assert (outcome.kind, outcome.error) == (kind, f"{step}: {error}")
    assert KEY not in outcome.error and "@" not in outcome.error
    assert len(fake.calls) == len(results)
    if isinstance(result, tuple) and len(result) > 2:
        assert outcome.retry_after == result[2]


def test_retry_resends_same_delivery_id_for_consumer_dedupe():
    first = run()[1].calls[1]["json"]["event_properties"]["delivery_id"]
    again = run()[1].calls[1]["json"]["event_properties"]["delivery_id"]
    assert first == again == ni.SAMPLE_BODY["delivery_metadata"]["deliveryId"]


# --- preview ------------------------------------------------------------------


def test_preview_redacts_api_key():
    calls = ni.preview("brevo", BREVO)
    assert [c["url"] for c in calls] == ["https://api.brevo.com/v3/contacts", "https://api.brevo.com/v3/events"]
    assert KEY not in repr(calls) and all(c["headers"]["api-key"] == ni.REDACTED for c in calls)


def test_preview_empty_without_host():
    assert ni.preview("brevo", {**BREVO, "native_region": "eu"}) == []
    assert ni.preview_refusal("brevo", {**BREVO, "native_region": "eu"}) is None  # validate reports it


def test_valid_preview_has_no_refusal():
    assert ni.preview("brevo", BREVO) and ni.preview_refusal("brevo", BREVO) is None
    assert ni.preview("brevo", EVENTS_ONLY) and ni.preview_refusal("brevo", EVENTS_ONLY) is None


@pytest.mark.parametrize(
    "creds, code",
    [
        ({**BREVO, "native_email_merge_ack": ""}, "native_match_strategy_unconfirmed"),
        ({k: v for k, v in BREVO.items() if k != "native_email_merge_ack"}, "native_match_strategy_unconfirmed"),
        ({**BREVO, "native_contact_sync": ""}, "native_match_strategy_unconfirmed"),
        ({**BREVO, "native_contact_attributes": "firstName=FIRSTNAME,lastName=FIRSTNAME"}, "native_config_invalid"),
    ],
)
def test_refused_preview_keeps_empty_list_and_reports_bounded_code(creds, code):
    assert ni.preview("brevo", creds) == []  # public shape unchanged
    refusal = ni.preview_refusal("brevo", creds)
    assert refusal == (code, ni.PREVIEW_REFUSALS[code])
    assert KEY not in repr(refusal) and "person@example.com" not in repr(refusal)


def test_preview_refusal_never_echoes_provider_text_or_secrets(monkeypatch):
    def stop(*a, **k):
        raise ni._Stop(ni.Outcome("dead", f"event: http_status: 500 {KEY} person@example.com"))

    monkeypatch.setattr(ni, "plan_brevo", stop)
    assert ni.preview_refusal("brevo", BREVO) == ("preview_refused", ni.PREVIEW_REFUSALS["preview_refused"])

    def skip(*a, **k):
        raise ni.Skip(f"native_no_email {KEY}")

    monkeypatch.setattr(ni, "plan_brevo", skip)
    code, guidance = ni.preview_refusal("brevo", BREVO)
    assert code == "preview_event_skipped" and KEY not in guidance


def test_delivery_id_survives_when_delivery_metadata_not_listed():
    # Review finding F2 (conditional) disproven: outbox.ALWAYS_GROUPS keeps delivery_metadata,
    # so no enable-time rule is needed for adapters that call _require_delivery_id.
    from webinars.outbox import refilter_for_send

    narrowed = dest(allowed_groups=["identity", "profile", "registration", "event"])
    assert ni.validate("brevo", BREVO, **{**KW, "allowed_groups": narrowed.allowed_groups}) == {}
    sent = refilter_for_send(body(), narrowed)
    assert sent["delivery_metadata"]["deliveryId"] == body()["delivery_metadata"]["deliveryId"]
    outcome, fake = run(b=sent)
    assert outcome.kind == "delivered" and len(fake.calls) == 2


# --- delivery (host pinning, consent gate, private links) -----------------------


class Transport:
    def __init__(self):
        self.calls = []

    def __call__(self, url, data, headers, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "data": data})
        return 204, ""


@pytest.fixture
def transport(monkeypatch):
    fake = Transport()
    monkeypatch.setattr(webinars.delivery, "post_json_pinned", fake, raising=False)
    monkeypatch.setattr(webinars.ssrf, "post_json_pinned", fake)
    return fake


def _deliver(msg):
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    return msg


def test_e2e_delivered_to_pinned_host_without_private_links(occurrence, transport, caplog):
    occurrence.join_url = "https://zoom.example/j/PRIVATE123"
    occurrence.save()
    msg, reg = _claimed(occurrence, BREVO, include_private_links=True)
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED
    assert [c["url"] for c in transport.calls] == ["https://api.brevo.com/v3/contacts", "https://api.brevo.com/v3/events"]
    assert all(c["headers"]["api-key"] == KEY for c in transport.calls)
    assert all(b"PRIVATE123" not in c["data"] for c in transport.calls)
    assert reg.person.email.encode() in transport.calls[1]["data"]
    assert KEY not in caplog.text


def test_e2e_operator_required_consent_blocks_before_first_step(occurrence, transport):
    msg, _ = _claimed(occurrence, BREVO)
    # Operator now requires a purpose the registrant has not granted (only analytics is): send-time check.
    Destination.objects.filter(pk=msg.destination_id).update(required_consents=["marketing"])
    msg = _deliver(msg)
    assert transport.calls == [] and msg.status == S.CANCELLED


def test_e2e_never_sent_to_another_host(occurrence, monkeypatch, transport, caplog):
    msg, _ = _claimed(occurrence, BREVO)
    monkeypatch.setattr(ni, "base_url", lambda provider, credentials: "https://api.segment.io")
    msg = _deliver(msg)
    assert transport.calls == []
    assert msg.status == S.DEAD and msg.last_error.endswith(": unsafe_destination")
    assert KEY not in msg.last_error and KEY not in caplog.text
