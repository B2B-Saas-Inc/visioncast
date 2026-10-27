"""Mailchimp native adapter: mocked member/tags/event contracts, data-center host policy, consent and redaction.

No network: transports are fakes and socket connects raise. Request shapes follow the official
references fetched 2026-10-27 (docs/native-integrations.md); nothing here has been checked
against a real Mailchimp account.
"""
import base64
import hashlib
import json
import socket

import pytest

import webinars.delivery
import webinars.ssrf
from tests.test_native_integrations import Fake, _claimed, body
from webinars import native_integrations as ni
from webinars.models import Destination, OutboxMessage
from webinars.native_providers import mailchimp as mc

pytestmark = pytest.mark.django_db

DC = "us19"
KEY = f"0123456789abcdef0123456789abcdef-{DC}"
HOST = f"{DC}.api.mailchimp.com"
AUDIENCE = "a1b2c3d4e5"
HASH = hashlib.md5(b"person@example.com").hexdigest()
MEMBER_URL = f"https://{HOST}/3.0/lists/{AUDIENCE}/members/{HASH}"
MC = {
    "native_provider": "mailchimp",
    "native_region": "datacenter",
    "native_datacenter": DC,
    "native_api_key": KEY,
    "native_audience_id": AUDIENCE,
    "native_email_merge_ack": "confirmed",
    "native_merge_field_map": "firstName=FNAME,lastName=LNAME,company=COMPANY,jobTitle=JOBTITLE",
    "native_tag_registered": "webinar registered",
    "native_tag_attended": "webinar attended",
    "native_tag_no_show": "webinar no-show",
    "native_processing_ack": "confirmed",
}
NO_TAGS = {k: v for k, v in MC.items() if not k.startswith("native_tag_")}
PENDING = {**MC, "native_status_if_new": "pending", "native_optin_ack": "confirmed"}
KW = dict(event_types=["registration.created"], allowed_groups=list(Destination.GROUPS), required_consents=[])
S = OutboxMessage.Status
MEMBER_OK = (200, {"id": HASH, "email_address": "person@example.com", "status": "transactional"})
BASIC = "Basic " + base64.b64encode(f"anystring:{KEY}".encode()).decode()
FORBIDDEN_MEMBER_KEYS = {
    "status", "interests", "tags", "marketing_permissions", "ip_signup", "ip_opt", "timestamp_signup", "timestamp_opt",
    "vip", "language", "email_type", "location", "sms_phone_number", "sms_subscription_status", "contact_id", "id",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", HOST, "api.segment.io"]


def run(creds=MC, b=None, event_type="registration.created", fake=None):
    fake = fake or Fake(responses=[MEMBER_OK])
    return ni.execute("mailchimp", creds, b if b is not None else body(), event_type, fake.ctx()), fake


def dest(**kw):
    return Destination(**{**KW, **kw})


# --- schema / credentials -----------------------------------------------------


def test_offered_and_schema_driven():
    assert dict(ni.PROVIDER_CHOICES)["mailchimp"] == "Mailchimp"
    assert ni.FIELDS["mailchimp"][0].name == "region" and ni.FIELDS["mailchimp"][0].choices
    assert ni.PROCESSING_ACK in ni.FIELDS["mailchimp"]
    assert "mailchimp" not in ni.HOSTS and HOST not in ni.VENDOR_HOSTS  # per-destination data-center host
    assert "mailchimp" in ni.NEEDS_DESTINATION_HOSTS and "mailchimp" in ni.NEEDS_REQUEST_TRANSPORT
    assert "mailchimp" not in ni.ANALYTICS_PROVIDERS
    assert ni.REGISTRY["mailchimp"] is mc
    status = next(f for f in ni.FIELDS["mailchimp"] if f.name == "status_if_new")
    assert {c for c, _ in status.choices} == {"transactional", "pending"}  # never subscribed


def test_credential_is_set_only():
    rendered = repr(ni.config_of(MC, "mailchimp")) + repr(ni.secrets_set(MC, "mailchimp"))
    assert KEY not in rendered and ni.secrets_set(MC, "mailchimp") == {"api_key": True}
    posted = {f.name: ni.config_of(MC, "mailchimp").get(f.name, "") for f in ni.FIELDS["mailchimp"]}
    posted["api_key"] = ""
    out, _ = ni.merge_credentials(MC, "mailchimp", posted)
    assert out["native_api_key"] == KEY  # blank keeps the stored value
    out, notes = ni.merge_credentials(MC, "segment", {"region": "us", "write_key": ""})
    assert KEY not in out.values() and notes


def test_valid_config():
    assert ni.validate("mailchimp", MC, **KW) == {}
    assert ni.validate("mailchimp", NO_TAGS, **KW) == {}
    assert ni.validate("mailchimp", PENDING, **KW) == {}
    assert ni.check_send_policy("mailchimp", MC, dest()) is None


@pytest.mark.parametrize("key", ["native_processing_ack", "native_email_merge_ack", "native_api_key", "native_datacenter", "native_audience_id"])
def test_required_fields(key):
    creds = {k: v for k, v in MC.items() if k != key}
    assert f"native-{key[len('native_'):]}" in ni.validate("mailchimp", creds, **KW)
    assert ni.check_send_policy("mailchimp", creds, dest()) == "native_config_invalid"


@pytest.mark.parametrize("value", ["not-confirmed", "yes", ""])
def test_email_merge_ack_must_be_confirmed(value):
    creds = {**MC, "native_email_merge_ack": value}
    assert "native-email_merge_ack" in ni.validate("mailchimp", creds, **KW)
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_match_strategy_unconfirmed", [])


def test_pending_needs_optin_ack_and_subscribed_is_refused():
    creds = {**MC, "native_status_if_new": "pending"}
    assert "native-optin_ack" in ni.validate("mailchimp", creds, **KW)
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_match_strategy_unconfirmed", [])
    for value in ("subscribed", "unsubscribed", "cleaned", "archived"):
        creds = {**MC, "native_status_if_new": value, "native_optin_ack": "confirmed"}
        assert "native-status_if_new" in ni.validate("mailchimp", creds, **KW)
        outcome, fake = run(creds)
        assert (outcome.kind, fake.calls) == ("dead", [])


@pytest.mark.parametrize(
    "mapping",
    [
        "email=EMAIL",
        "firstName=EMAIL",  # reserved
        "phone=PHONE",
        "customField=X",
        "firstName=fname",  # targets are uppercase merge tags
        "firstName=A,firstName=B",
        "firstName=NAME,lastName=NAME",
        "firstName=A;lastName=B",
    ],
)
def test_merge_field_map_allowlist(mapping):
    creds = {**MC, "native_merge_field_map": mapping}
    assert "native-merge_field_map" in ni.validate("mailchimp", creds, **KW)
    outcome, fake = run(creds)
    assert outcome.kind == "dead" and fake.calls == []


# --- host / data center policy (fail closed) ------------------------------------


@pytest.mark.parametrize("dc", ["US19", "us", "1us", "us-19", "us19.api.mailchimp.com", "us19/x", "", "u1", "abcd1"])
def test_bad_datacenter_rejected(dc, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = [HOST, f"{dc}.api.mailchimp.com"]
    creds = {**MC, "native_datacenter": dc, "native_api_key": f"abc-{dc}"}
    assert ni.validate("mailchimp", creds, **KW)
    assert ni.base_url("mailchimp", creds) is None and ni.allowed_hosts("mailchimp", creds) == frozenset()
    outcome, fake = run(creds)
    assert (outcome.kind, fake.calls) == ("dead", [])


@pytest.mark.parametrize("entries", [["api.mailchimp.com"], ["*.api.mailchimp.com"], [".api.mailchimp.com"], ["hooks.example.net"], ["us6.api.mailchimp.com"]])
def test_host_needs_exact_operator_entry(entries, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = entries
    errors = ni.validate("mailchimp", MC, **KW)
    assert any("exactly" in m and HOST in m for m in errors.get("provider", []))
    assert ni.check_send_policy("mailchimp", MC, dest()) == "native_config_invalid"
    assert ni.allowed_hosts("mailchimp", MC) == frozenset()
    outcome, fake = run()
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])


def test_datacenter_must_match_key_suffix_without_rendering_it(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = [HOST, "us6.api.mailchimp.com"]  # host would be fine; the key suffix is not
    creds = {**MC, "native_datacenter": "us6"}  # key ends in -us19
    errors = ni.validate("mailchimp", creds, **KW)
    assert "native-datacenter" in errors and KEY not in repr(errors) and "us19" not in repr(errors)
    assert ni.check_send_policy("mailchimp", creds, dest()) == "native_config_invalid"
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_invalid", [])
    for key in ("nodashsuffix", "abc-us19x", "abc-us6-us19-", ""):
        assert not mc.key_matches_datacenter({"native_api_key": key, "native_datacenter": "us19"})
    assert mc.key_matches_datacenter({"native_api_key": "a-b-us19", "native_datacenter": "us19"})


def test_host_removed_from_allowlist_after_enable_stops_send(settings):
    assert ni.check_send_policy("mailchimp", MC, dest()) is None
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    assert ni.check_send_policy("mailchimp", MC, dest()) == "native_config_invalid"
    assert run()[1].calls == []


def test_allowed_hosts_pins_exact_host():
    assert ni.allowed_hosts("mailchimp", MC) == frozenset({HOST})
    assert ni.base_url("mailchimp", MC) == f"https://{HOST}"
    assert ni.allowed_hosts("mailchimp", {**MC, "native_region": "global"}) == frozenset()


def test_unsendable_without_delivery_host_pinning(monkeypatch):
    monkeypatch.delattr(webinars.delivery, "NATIVE_DESTINATION_HOSTS", raising=False)
    assert any("per-destination host pinning" in m for m in ni.validate("mailchimp", MC, **KW)["provider"])
    assert ni.check_send_policy("mailchimp", MC, dest()) == "native_transport_unavailable"
    outcome, fake = run()
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_transport_unavailable", [])


# --- request contract ---------------------------------------------------------


def test_exact_requests():
    outcome, fake = run()
    assert outcome.kind == "delivered"
    member, tags, event = fake.calls
    for call in fake.calls:
        assert call["headers"]["Authorization"] == BASIC and call["headers"]["Content-Type"].startswith("application/json")
        assert call["url"].startswith(MEMBER_URL)
    assert member["method"] == "PUT" and member["url"] == MEMBER_URL
    assert member["json"] == {
        "email_address": "person@example.com",
        "status_if_new": "transactional",
        "merge_fields": {"FNAME": "Sample", "LNAME": "Person", "COMPANY": "Example Co", "JOBTITLE": "Role"},
    }
    assert tags["method"] == "POST" and tags["url"] == MEMBER_URL + "/tags"
    assert tags["json"] == {"tags": [{"name": "webinar registered", "status": "active"}]}
    assert event["method"] == "POST" and event["url"] == MEMBER_URL + "/events"
    assert event["json"]["name"] == "webinar_registered" and event["json"]["occurred_at"] == ni.SAMPLE_BODY["occurredAt"]
    props = event["json"]["properties"]
    assert all(isinstance(v, str) for v in props.values())
    assert props["webinar_delivery_id"] == ni.SAMPLE_BODY["delivery_metadata"]["deliveryId"]
    assert props["webinar_registration_id"] == ni.SAMPLE_BODY["registration"]["id"]
    assert props["webinar_event_type"] == "registration.created"
    assert set(event["json"]) == {"name", "properties", "occurred_at"}


def test_subscriber_hash_is_md5_of_lowercased_email():
    b = body()
    b["profile"]["email"] = "  Person@Example.COM "
    member = run(MC, b)[1].calls[0]
    assert member["url"] == MEMBER_URL and member["json"]["email_address"] == "  Person@Example.COM "
    assert mc.subscriber_hash("Person@Example.com") == HASH


def test_pending_status_if_new_with_ack():
    member = run(PENDING)[1].calls[0]["json"]
    assert member["status_if_new"] == "pending" and "status" not in member


@pytest.mark.parametrize(
    "event_type,attendance,name,tag",
    [
        ("registration.created", "unknown", "webinar_registered", "webinar registered"),
        ("registration.updated", "unknown", "webinar_registration_updated", None),
        ("attendance.checked_in", "unknown", "webinar_checked_in", "webinar attended"),
        ("attendance.recorded", "attended", "webinar_attended", "webinar attended"),
        ("attendance.recorded", "no_show", "webinar_no_show", "webinar no-show"),
    ],
)
def test_event_names_and_lifecycle_tags(event_type, attendance, name, tag):
    b = body()
    b["registration"]["attendance"] = attendance
    outcome, fake = run(MC, b, event_type)
    assert outcome.kind == "delivered"
    assert 2 <= len(name) <= 30 and fake.calls[-1]["json"]["name"] == name
    assert fake.calls[-1]["json"]["properties"]["webinar_attendance"] == attendance
    urls = [c["url"] for c in fake.calls]
    if tag is None:
        assert urls == [MEMBER_URL, MEMBER_URL + "/events"]
    else:
        assert urls == [MEMBER_URL, MEMBER_URL + "/tags", MEMBER_URL + "/events"]
        assert fake.calls[1]["json"] == {"tags": [{"name": tag, "status": "active"}]}
    outcome, fake = run(NO_TAGS, b, event_type)
    assert outcome.kind == "delivered" and [c["url"] for c in fake.calls] == [MEMBER_URL, MEMBER_URL + "/events"]


def test_unknown_attendance_skipped():
    b = body()
    b["registration"]["attendance"] = "weird"
    outcome, fake = run(MC, b, "attendance.recorded")
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_attendance_unknown", [])


def test_never_status_interests_permissions_or_campaign_endpoints():
    b = body()
    b["profile"].update({"phone": "+15550100", "status": "subscribed", "interests": {"x": True}, "vip": True, "customField": "x"})
    outcome, fake = run(MC, b)
    assert outcome.kind == "delivered"
    member = fake.calls[0]["json"]
    assert not FORBIDDEN_MEMBER_KEYS & set(member) and set(member) <= {"email_address", "status_if_new", "merge_fields"}
    assert "subscribed" not in repr([c["json"] for c in fake.calls])
    for call in fake.calls:
        text = repr(call["json"])
        assert "+15550100" not in text and "customField" not in text and "interests" not in text
        assert not any(p in call["url"] for p in ("/campaigns", "/automations", "/segments", "/interest-categories", "/signup-forms", "/batches"))
    assert set(fake.calls[1]["json"]) == {"tags"} and set(fake.calls[1]["json"]["tags"][0]) == {"name", "status"}


def test_empty_mapping_sends_email_and_status_only():
    member = run({**MC, "native_merge_field_map": ""})[1].calls[0]["json"]
    assert member == {"email_address": "person@example.com", "status_if_new": "transactional"}


def test_partial_mapping_only_maps_listed_fields():
    member = run({**MC, "native_merge_field_map": "firstName=FNAME"})[1].calls[0]["json"]
    assert member["merge_fields"] == {"FNAME": "Sample"}


# --- groups / private links / identity / consent -------------------------------


def test_no_profile_group_uses_identity_email_without_merge_fields():
    b = body()
    b.pop("profile")
    outcome, fake = run(MC, b)
    assert outcome.kind == "delivered"
    assert fake.calls[0]["json"] == {"email_address": "person@example.com", "status_if_new": "transactional"}
    assert "Sample" not in repr(fake.calls[-1]["json"]["properties"]).replace("Sample webinar", "")


def test_no_email_skipped_before_any_call():
    b = body()
    b["profile"].pop("email")
    b["identity"].pop("email")
    outcome, fake = run(MC, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_no_email", [])


def test_unverified_identity_never_sent():
    for mutate in (lambda b: b["identity"].update(verified=False), lambda b: b["registration"].update(identityVerified=False)):
        b = body()
        mutate(b)
        outcome, fake = run(MC, b)
        assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_identity_unverified", [])


def test_missing_delivery_id_is_dead():
    b = body()
    b["delivery_metadata"] = {}
    outcome, fake = run(MC, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_delivery_id_missing", [])


def test_private_links_urls_and_profile_never_in_event():
    b = body()
    b["event"]["occurrence"].update({"joinUrl": "https://zoom.example/j/SECRET1", "waitingRoomUrl": "https://zoom.example/w/SECRET2", "managedRoomUrl": "https://w.example/w/SECRET3/"})
    b["event"]["heroImageUrl"] = "https://img.example/SECRET4"
    calls = run(MC, b)[1].calls
    for call in calls:
        assert "SECRET" not in repr(call["json"]) and "https://" not in repr(call["json"])
    props = repr(calls[-1]["json"]["properties"])
    assert "@" not in props and "Example Co" not in props and "Role" not in props


def test_nested_merge_value_refused_before_any_call():
    b = body()
    b["profile"]["firstName"] = {"given": "Sample"}
    outcome, fake = run(MC, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "member: payload_unsupported", [])


def test_unparseable_timestamp_omitted_not_invented():
    event = run(MC, body(occurredAt="not-a-date"))[1].calls[-1]["json"]
    assert "occurred_at" not in event


def test_consent_rechecked_before_every_later_step():
    fake = Fake(responses=[MEMBER_OK])
    stop = ni.Outcome("cancelled", "consent_not_granted_mid_sequence")
    ctx = ni.NativeContext(post=fake.post, request=fake.request, renew=fake.renew, check=lambda: stop)
    outcome = ni.execute("mailchimp", MC, body(), "registration.created", ctx)
    assert outcome == stop and [c["url"] for c in fake.calls] == [MEMBER_URL]


# --- responses / failures -------------------------------------------------------


@pytest.mark.parametrize("response", [(200, {}), (200, {"id": ""}), (200, [])])
def test_missing_member_id_is_retried_before_tags_or_event(response):
    outcome, fake = run(fake=Fake(responses=[response]))
    assert (outcome.kind, outcome.error) == ("retry", "member: response_missing_id")
    assert [c["url"] for c in fake.calls] == [MEMBER_URL]


class StatusFake:
    """``request`` (member PUT) and ``post`` (tags, event) seams sharing one ordered result list."""

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def _next(self, url):
        self.calls.append(url)
        r = self.results.pop(0) if self.results else (204,)
        if isinstance(r, Exception):
            raise r
        return r

    def ctx(self):
        def request(method, url, data, headers):
            r = self._next(url)
            return (r[0], json.dumps({"id": HASH}).encode(), *r[1:])

        def post(url, data, headers):
            r = self._next(url)
            return (r[0], "", *r[1:])

        return ni.NativeContext(post=post, request=request, renew=lambda: True)


def test_all_steps_2xx_delivered():
    fake = StatusFake((200,), (204,), (204,))
    assert ni.execute("mailchimp", MC, body(), "registration.created", fake.ctx()).kind == "delivered"
    assert fake.calls == [MEMBER_URL, MEMBER_URL + "/tags", MEMBER_URL + "/events"]


@pytest.mark.parametrize(
    "result,kind,error",
    [
        ((429, 30), "retry", "http_status: 429"),  # 10 concurrent connections per user, documented
        ((500,), "retry", "http_status: 500"),
        ((503, 7), "retry", "http_status: 503"),
        ((425,), "retry", "http_status: 425"),
        ((301,), "retry", "redirect_not_followed: http 301"),
        ((400,), "dead", "http_status: 400"),
        ((401,), "dead", "http_status: 401"),
        ((403,), "dead", "http_status: 403"),
        ((404,), "dead", "http_status: 404"),
        (TimeoutError(), "retry", "timeout"),
        (ConnectionResetError(KEY), "retry", "transport_error: ConnectionResetError"),
        (ValueError(f"echo {KEY} person@example.com"), "retry", "transport_error: ValueError"),
    ],
)
@pytest.mark.parametrize("step", ["member", "tags", "event"])
def test_failures_stop_and_never_echo_secrets(step, result, kind, error):
    results = {"member": [result], "tags": [(200,), result], "event": [(200,), (204,), result]}[step]
    fake = StatusFake(*results)
    outcome = ni.execute("mailchimp", MC, body(), "registration.created", fake.ctx())
    assert (outcome.kind, outcome.error) == (kind, f"{step}: {error}")
    assert KEY not in outcome.error and "@" not in outcome.error and DC not in outcome.error
    assert len(fake.calls) == len(results)
    if isinstance(result, tuple) and len(result) > 1:
        assert outcome.retry_after == result[1]


def test_retry_resends_identical_requests_with_same_delivery_id():
    first = [c["json"] for c in run()[1].calls]
    again = [c["json"] for c in run()[1].calls]
    assert first == again
    assert first[-1]["properties"]["webinar_delivery_id"] == ni.SAMPLE_BODY["delivery_metadata"]["deliveryId"]


# --- preview ------------------------------------------------------------------


def test_preview_redacts_api_key():
    calls = ni.preview("mailchimp", MC)
    assert [c["url"] for c in calls] == [MEMBER_URL, MEMBER_URL + "/tags", MEMBER_URL + "/events"]
    assert KEY not in repr(calls) and BASIC not in repr(calls)
    assert all(c["headers"]["Authorization"] == ni.REDACTED for c in calls)
    assert calls[0]["body"]["status_if_new"] == "transactional"


def test_preview_empty_without_host(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.mailchimp.com"]
    assert ni.preview("mailchimp", MC) == []
    assert ni.preview_refusal("mailchimp", MC) is None  # validate reports it


def test_valid_preview_has_no_refusal():
    assert ni.preview("mailchimp", MC) and ni.preview_refusal("mailchimp", MC) is None
    assert len(ni.preview("mailchimp", NO_TAGS)) == 2 and ni.preview_refusal("mailchimp", NO_TAGS) is None
    assert ni.preview("mailchimp", PENDING)[0]["body"]["status_if_new"] == "pending"


@pytest.mark.parametrize(
    "creds, code",
    [
        ({**MC, "native_email_merge_ack": ""}, "native_match_strategy_unconfirmed"),
        ({**MC, "native_status_if_new": "pending"}, "native_match_strategy_unconfirmed"),
        ({**MC, "native_merge_field_map": "firstName=FNAME,lastName=FNAME"}, "native_config_invalid"),
        ({**MC, "native_audience_id": "bad id!"}, "native_config_invalid"),
    ],
)
def test_refused_preview_keeps_empty_list_and_reports_bounded_code(creds, code):
    assert ni.preview("mailchimp", creds) == []
    refusal = ni.preview_refusal("mailchimp", creds)
    assert refusal == (code, ni.PREVIEW_REFUSALS[code])
    assert KEY not in repr(refusal) and "person@example.com" not in repr(refusal)


# --- delivery (host pinning, consent gate, config change) ------------------------


class RecordingRequest:
    """Stands in for ``ssrf.request_json_bounded``: applies its host/credential policy, records calls."""

    def __init__(self, on_first=None, status=None):
        self.calls, self.on_first, self.status = [], on_first, status

    def __call__(self, method, url, data, headers, timeout, total, max_bytes, allowed_hosts=None):
        host = webinars.ssrf._validated_target(url)[0]
        webinars.ssrf._check_native_request(method, host, headers, allowed_hosts)
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "data": data, "allowed": allowed_hosts})
        if len(self.calls) == 1 and self.on_first:
            self.on_first()
        if method == "PUT":
            return self.status or 200, json.dumps({"id": url.rsplit("/", 1)[1]}).encode(), None, False
        return self.status or 204, b"", None, False


@pytest.fixture
def transport(monkeypatch):
    fake = RecordingRequest()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    return fake


def _deliver(msg):
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    return msg


def test_e2e_delivered_to_pinned_host_without_private_links(occurrence, transport, caplog):
    occurrence.join_url = "https://zoom.example/j/PRIVATE123"
    occurrence.save()
    msg, reg = _claimed(occurrence, MC, include_private_links=True)
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED and msg.attempts == 1
    h = hashlib.md5(reg.person.email.lower().encode()).hexdigest()
    url = f"https://{HOST}/3.0/lists/{AUDIENCE}/members/{h}"
    assert [(c["method"], c["url"]) for c in transport.calls] == [("PUT", url), ("POST", url + "/tags"), ("POST", url + "/events")]
    assert all(c["allowed"] == frozenset({HOST}) and c["headers"]["Authorization"] == BASIC for c in transport.calls)
    assert all(b"PRIVATE123" not in c["data"] for c in transport.calls)
    assert str(msg.id).encode() in transport.calls[-1]["data"]
    assert KEY not in caplog.text and KEY not in msg.last_error


@pytest.mark.parametrize("status,expected", [(503, S.PENDING), (400, S.DEAD)])
def test_e2e_failures(occurrence, transport, status, expected):
    transport.status = status
    msg, _ = _claimed(occurrence, MC)
    msg = _deliver(msg)
    assert msg.status == expected and msg.attempts == 1 and msg.last_status_code == status
    assert msg.last_error == f"member: http_status: {status}" and KEY not in msg.last_error


def test_e2e_operator_required_consent_blocks_before_first_step(occurrence, transport):
    msg, _ = _claimed(occurrence, MC)
    Destination.objects.filter(pk=msg.destination_id).update(required_consents=["marketing"])
    msg = _deliver(msg)
    assert transport.calls == [] and msg.status == S.CANCELLED


@pytest.mark.parametrize("target", ["https://api.segment.io", "https://hooks.example.net", "https://us6.api.mailchimp.com"])
def test_e2e_never_sent_to_another_host(occurrence, monkeypatch, settings, transport, caplog, target):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", HOST, "api.segment.io", "us6.api.mailchimp.com"]
    msg, _ = _claimed(occurrence, MC)
    monkeypatch.setattr(ni, "base_url", lambda provider, credentials: target)
    msg = _deliver(msg)
    assert transport.calls == []
    assert msg.status == S.DEAD and msg.last_error.endswith(": unsafe_destination")
    assert KEY not in msg.last_error and KEY not in caplog.text


def test_e2e_config_change_after_member_stops_before_next(occurrence, monkeypatch):
    from webinars.crypto import encrypt_secret

    msg, _ = _claimed(occurrence, MC)
    changed = {**MC, "native_tag_registered": "other"}
    fake = RecordingRequest(lambda: Destination.objects.filter(pk=msg.destination_id).update(credentials_encrypted=encrypt_secret(json.dumps(changed))))
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg = _deliver(msg)
    assert len(fake.calls) == 1 and fake.calls[0]["method"] == "PUT"  # the member write is not recalled; no stale tag/event
    assert msg.status == S.DEAD and msg.last_error == "native_config_changed_mid_sequence"


def test_e2e_without_request_transport_dead(occurrence, monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    msg, _ = _claimed(occurrence, MC)
    msg = _deliver(msg)
    assert msg.status == S.DEAD and msg.last_error == "native_transport_unavailable"
