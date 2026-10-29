"""ActiveCampaign native adapter: mocked contact-sync and tag contracts, host policy, consent and redaction.

No network: transports are fakes and socket connects raise. Request shapes follow the official
references fetched 2026-10-27 (docs/native-integrations.md); nothing here has been checked
against a real ActiveCampaign account.
"""
import json
import multiprocessing
import socket

import pytest

import webinars.delivery
import webinars.ssrf
from tests.test_native_integrations import Fake, _claimed, body
from webinars import native_integrations as ni
from webinars.models import Destination, OutboxMessage
from webinars.native_providers import activecampaign as ac
from webinars.ssrf import UnsafeDestination

pytestmark = pytest.mark.django_db

KEY = "ac-apiTokenSecretValue-123456"
HOST = "acme.api-us1.com"
AC = {
    "native_provider": "activecampaign",
    "native_region": "account",
    "native_api_host": HOST,
    "native_api_key": KEY,
    "native_email_merge_ack": "confirmed",
    "native_field_map": "company=12,jobTitle=13,webinar_event_title=15,webinar_registration_status=16",
    "native_tag_registered": "20",
    "native_tag_attended": "21",
    "native_tag_no_show": "22",
    "native_processing_ack": "confirmed",
}
NO_TAGS = {k: v for k, v in AC.items() if not k.startswith("native_tag_")}
KW = dict(event_types=["registration.created"], allowed_groups=list(Destination.GROUPS), required_consents=[])
S = OutboxMessage.Status
SYNC_URL = f"https://{HOST}/api/3/contact/sync"
TAG_URL = f"https://{HOST}/api/3/contactTags"
SYNC_OK = (201, {"contact": {"id": "7"}})
FORBIDDEN_CONTACT_KEYS = {
    "phone", "lists", "tags", "status", "form", "account", "account_name", "orgid", "organization", "ip4",
    "whatsapp_id", "source", "useDefaults", "allowNullEmail", "open_tracking_optout", "id", "fields", "deleted",
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


def run(creds=AC, b=None, event_type="registration.created", fake=None):
    fake = fake or Fake(responses=[SYNC_OK])
    return ni.execute("activecampaign", creds, b if b is not None else body(), event_type, fake.ctx()), fake


def dest(**kw):
    return Destination(**{**KW, **kw})


# --- schema / credentials -----------------------------------------------------


def test_offered_and_schema_driven():
    assert dict(ni.PROVIDER_CHOICES)["activecampaign"] == "ActiveCampaign"
    assert ni.FIELDS["activecampaign"][0].name == "region" and ni.FIELDS["activecampaign"][0].choices
    assert ni.PROCESSING_ACK in ni.FIELDS["activecampaign"]
    assert "activecampaign" not in ni.HOSTS and HOST not in ni.VENDOR_HOSTS  # per-destination host
    assert "activecampaign" in ni.NEEDS_DESTINATION_HOSTS and "activecampaign" in ni.NEEDS_REQUEST_TRANSPORT
    assert "api-token" in ni.SECRET_HEADERS
    assert "activecampaign" not in ni.ANALYTICS_PROVIDERS  # consent purposes are the operator's choice
    assert ni.REGISTRY["activecampaign"] is ac


def test_credential_is_set_only():
    rendered = repr(ni.config_of(AC, "activecampaign")) + repr(ni.secrets_set(AC, "activecampaign"))
    assert KEY not in rendered and ni.secrets_set(AC, "activecampaign") == {"api_key": True}
    posted = {f.name: ni.config_of(AC, "activecampaign").get(f.name, "") for f in ni.FIELDS["activecampaign"]}
    posted["api_key"] = ""
    out, _ = ni.merge_credentials(AC, "activecampaign", posted)
    assert out["native_api_key"] == KEY  # blank keeps the stored value
    out, notes = ni.merge_credentials(AC, "segment", {"region": "us", "write_key": ""})
    assert KEY not in out.values() and notes


def test_valid_config():
    assert ni.validate("activecampaign", AC, **KW) == {}
    assert ni.validate("activecampaign", NO_TAGS, **KW) == {}
    assert ni.check_send_policy("activecampaign", AC, dest()) is None


@pytest.mark.parametrize("key", ["native_processing_ack", "native_email_merge_ack", "native_api_key", "native_api_host"])
def test_required_fields(key):
    creds = {k: v for k, v in AC.items() if k != key}
    assert f"native-{key[len('native_'):]}" in ni.validate("activecampaign", creds, **KW)
    assert ni.check_send_policy("activecampaign", creds, dest()) == "native_config_invalid"


@pytest.mark.parametrize("value", ["not-confirmed", "yes", ""])
def test_email_merge_ack_must_be_confirmed(value):
    creds = {**AC, "native_email_merge_ack": value}
    assert "native-email_merge_ack" in ni.validate("activecampaign", creds, **KW)
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_match_strategy_unconfirmed", [])


@pytest.mark.parametrize(
    "mapping",
    [
        "phone=12",  # never phone
        "email=12",
        "customField=12",
        "company=abc",  # targets are numeric field ids
        "company=0",
        "company=12,company=13",  # duplicate source
        "company=12,jobTitle=12",  # duplicate target
        "company=12;jobTitle=13",
        "company=12,",
    ],
)
def test_field_map_allowlist(mapping):
    creds = {**AC, "native_field_map": mapping}
    assert "native-field_map" in ni.validate("activecampaign", creds, **KW)
    outcome, fake = run(creds)
    assert outcome.kind == "dead" and fake.calls == []


@pytest.mark.parametrize("tag", ["0", "abc", "-1", "20,21", "1" * 16])
def test_tag_ids_numeric(tag):
    creds = {**AC, "native_tag_registered": tag}
    assert "native-tag_registered" in ni.validate("activecampaign", creds, **KW)
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_invalid", [])


# --- host policy (fail closed) -------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["https://acme.api-us1.com", "acme.api-us1.com:443", "acme.api-us1.com/api/3", "*.api-us1.com", "Acme.API-US1.com", "acme.api-us1.com.", "10.0.0.1", "localhost", "acme", ""],
)
def test_bad_host_formats_rejected(host, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = [host or "x.example.com", HOST]
    creds = {**AC, "native_api_host": host}
    assert ni.validate("activecampaign", creds, **KW)
    assert ni.base_url("activecampaign", creds) is None and ni.allowed_hosts("activecampaign", creds) == frozenset()
    outcome, fake = run(creds)
    assert (outcome.kind, fake.calls) == ("dead", [])


@pytest.mark.parametrize(
    "entries,host",
    [
        (["api-us1.com"], HOST),  # parent domain does not admit sub-domains
        (["*.api-us1.com"], HOST),
        ([".api-us1.com"], HOST),
        ([HOST], HOST + ".evil.net"),  # suffix trick
        ([HOST], "evil" + HOST),
        (["hooks.example.net"], HOST),
    ],
)
def test_host_needs_exact_operator_entry(entries, host, settings):
    settings.NATIVE_VENDOR_HOSTS_IMPLICIT = False  # operator opted out of implicit vendor hosts
    settings.WEBHOOK_HOST_ALLOWLIST = entries
    creds = {**AC, "native_api_host": host}
    errors = ni.validate("activecampaign", creds, **KW)
    assert any("exactly" in m for m in errors.get("provider", []))
    assert ni.check_send_policy("activecampaign", creds, dest()) == "native_config_invalid"
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])


def test_other_vendor_host_refused(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.segment.io", "api.brevo.com"]
    for host in ("api.segment.io", "api.brevo.com"):
        creds = {**AC, "native_api_host": host}
        assert "native-api_host" in ni.validate("activecampaign", creds, **KW)
        assert ni.allowed_hosts("activecampaign", creds) == frozenset()


def test_host_removed_from_allowlist_after_enable_stops_send(settings):
    settings.NATIVE_VENDOR_HOSTS_IMPLICIT = False
    assert ni.check_send_policy("activecampaign", AC, dest()) is None
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    assert ni.check_send_policy("activecampaign", AC, dest()) == "native_config_invalid"
    assert run()[1].calls == []


def test_unknown_region_has_no_host():
    creds = {**AC, "native_region": "global"}
    assert ni.validate("activecampaign", creds, **KW)
    assert ni.allowed_hosts("activecampaign", creds) == frozenset()


def test_allowed_hosts_pins_exact_host():
    assert ni.allowed_hosts("activecampaign", AC) == frozenset({HOST})
    assert ni.base_url("activecampaign", AC) == f"https://{HOST}"


def test_unsendable_without_delivery_host_pinning(monkeypatch):
    monkeypatch.delattr(webinars.delivery, "NATIVE_DESTINATION_HOSTS", raising=False)
    assert any("per-destination host pinning" in m for m in ni.validate("activecampaign", AC, **KW)["provider"])
    assert ni.check_send_policy("activecampaign", AC, dest()) == "native_transport_unavailable"
    outcome, fake = run()
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_transport_unavailable", [])


def test_unsendable_without_request_transport(monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    assert any("request_json_bounded" in m for m in ni.validate("activecampaign", AC, **KW)["provider"])
    assert ni.check_send_policy("activecampaign", AC, dest()) == "native_transport_unavailable"


# --- transport policy: Api-Token is a credential header ------------------------


@pytest.fixture
def no_spawn(monkeypatch):
    spawned = []
    monkeypatch.setattr(multiprocessing, "get_context", lambda *a: spawned.append(a) or None)
    return spawned


@pytest.mark.parametrize("header", ["Api-Token", "api-token", "API-TOKEN", "X-Api-Token", "x-api-token"])
def test_api_token_header_needs_explicit_hosts(settings, no_spawn, header):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", HOST]
    assert header.lower() in webinars.ssrf.CREDENTIAL_HEADERS
    with pytest.raises(UnsafeDestination):
        webinars.ssrf.request_json_bounded("POST", "https://hooks.example.net/x", b"{}", {header: "x"}, 10, 5)
    with pytest.raises(UnsafeDestination):
        webinars.ssrf._check_native_request("POST", "hooks.example.net", {header: "x"}, None)
    with pytest.raises(UnsafeDestination):  # named hosts, but not this one
        webinars.ssrf.request_json_bounded("POST", "https://hooks.example.net/x", b"{}", {header: "x"}, 10, 5, allowed_hosts=frozenset({HOST}))
    assert no_spawn == []


# --- request contract ---------------------------------------------------------


def test_exact_requests():
    outcome, fake = run()
    assert outcome.kind == "delivered"
    contact, tag = fake.calls
    for call in (contact, tag):
        assert call["method"] == "POST" and call["url"].startswith(f"https://{HOST}/api/3/")
        assert call["headers"]["Api-Token"] == KEY and "Authorization" not in call["headers"]
        assert call["headers"]["Content-Type"].startswith("application/json")
    assert contact["url"] == SYNC_URL
    assert contact["json"] == {
        "contact": {
            "email": "person@example.com",
            "firstName": "Sample",
            "lastName": "Person",
            "fieldValues": [
                {"field": "12", "value": "Example Co"},
                {"field": "13", "value": "Role"},
                {"field": "15", "value": "Sample webinar"},
                {"field": "16", "value": "registered"},
            ],
        }
    }
    assert tag["url"] == TAG_URL and tag["json"] == {"contactTag": {"contact": 7, "tag": 20}}


@pytest.mark.parametrize(
    "event_type,attendance,tag",
    [
        ("registration.created", "unknown", 20),
        ("registration.updated", "unknown", None),
        ("attendance.checked_in", "unknown", 21),
        ("attendance.recorded", "attended", 21),
        ("attendance.recorded", "no_show", 22),
    ],
)
def test_lifecycle_tags(event_type, attendance, tag):
    b = body()
    b["registration"]["attendance"] = attendance
    outcome, fake = run(AC, b, event_type)
    assert outcome.kind == "delivered"
    urls = [c["url"] for c in fake.calls]
    if tag is None:
        assert urls == [SYNC_URL]
    else:
        assert urls == [SYNC_URL, TAG_URL] and fake.calls[1]["json"] == {"contactTag": {"contact": 7, "tag": tag}}
    outcome, fake = run(NO_TAGS, b, event_type)
    assert outcome.kind == "delivered" and [c["url"] for c in fake.calls] == [SYNC_URL]


def test_unknown_attendance_skipped():
    b = body()
    b["registration"]["attendance"] = "weird"
    outcome, fake = run(AC, b, "attendance.recorded")
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_attendance_unknown", [])


def test_never_phone_lists_status_automations_or_tracking():
    b = body()
    b["profile"].update({"phone": "+15550100", "lists": [1], "status": 1, "customField": "x", "tags": ["vip"]})
    b["identity"]["phone"] = "+15550100"
    outcome, fake = run(AC, b)
    assert outcome.kind == "delivered"
    for call in fake.calls:
        text = repr(call["json"])
        assert "+15550100" not in text and "customField" not in text and "vip" not in text
        assert not any(p in call["url"] for p in ("/contactLists", "/contactAutomations", "/automations", "trackcmp", "/lists", "/contacts/"))
    contact = fake.calls[0]["json"]["contact"]
    assert not FORBIDDEN_CONTACT_KEYS & set(contact)
    assert set(contact) <= {"email", "firstName", "lastName", "fieldValues"}
    assert set(fake.calls[0]["json"]) == {"contact"}
    assert set(fake.calls[1]["json"]["contactTag"]) == {"contact", "tag"}


def test_empty_mapping_sends_email_and_names_only():
    contact = run({**AC, "native_field_map": ""})[1].calls[0]["json"]["contact"]
    assert contact == {"email": "person@example.com", "firstName": "Sample", "lastName": "Person"}


def test_missing_values_are_not_sent_as_empty_fields():
    b = body()
    b["profile"].pop("company")
    b["event"].pop("title")
    values = run(AC, b)[1].calls[0]["json"]["contact"]["fieldValues"]
    assert values == [{"field": "13", "value": "Role"}, {"field": "16", "value": "registered"}]


def test_mapped_webinar_fields_are_lifecycle_facts():
    b = body()
    b["registration"]["attendance"] = "attended"
    creds = {**AC, "native_field_map": "webinar_attendance=30,webinar_event_type=31,webinar_starts_at=32"}
    values = run(creds, b, "attendance.recorded")[1].calls[0]["json"]["contact"]["fieldValues"]
    assert values == [
        {"field": "30", "value": "attended"},
        {"field": "31", "value": "attendance.recorded"},
        {"field": "32", "value": ni.SAMPLE_BODY["event"]["occurrence"]["startsAt"]},
    ]


# --- groups / private links / identity / consent -------------------------------


def test_no_profile_group_syncs_identity_email_without_names():
    b = body()
    b.pop("profile")
    outcome, fake = run(AC, b)
    assert outcome.kind == "delivered"
    contact = fake.calls[0]["json"]["contact"]
    assert contact == {"email": "person@example.com", "fieldValues": [{"field": "15", "value": "Sample webinar"}, {"field": "16", "value": "registered"}]}


def test_no_email_skipped_before_any_call():
    b = body()
    b["profile"].pop("email")
    b["identity"].pop("email")
    outcome, fake = run(AC, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_no_email", [])


def test_unverified_identity_never_sent():
    for mutate in (lambda b: b["identity"].update(verified=False), lambda b: b["registration"].update(identityVerified=False)):
        b = body()
        mutate(b)
        outcome, fake = run(AC, b)
        assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_identity_unverified", [])


def test_private_links_and_urls_never_sent():
    b = body()
    b["event"]["occurrence"].update({"joinUrl": "https://zoom.example/j/SECRET1", "waitingRoomUrl": "https://zoom.example/w/SECRET2", "managedRoomUrl": "https://w.example/w/SECRET3/"})
    b["event"]["heroImageUrl"] = "https://img.example/SECRET4"
    for call in run(AC, b)[1].calls:
        assert "SECRET" not in repr(call["json"]) and "https://" not in repr(call["json"])


def test_nested_profile_value_refused_before_any_call():
    b = body()
    b["profile"]["company"] = {"name": "Example"}
    outcome, fake = run(AC, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "contact: payload_unsupported", [])
    b = body()
    b["profile"]["firstName"] = ["Sample"]
    outcome, fake = run(AC, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "contact: payload_unsupported", [])


def test_consent_rechecked_before_tag_step():
    fake = Fake(responses=[SYNC_OK])
    stop = ni.Outcome("cancelled", "consent_not_granted_mid_sequence")
    ctx = ni.NativeContext(post=fake.post, request=fake.request, renew=fake.renew, check=lambda: stop)
    outcome = ni.execute("activecampaign", AC, body(), "registration.created", ctx)
    assert outcome == stop and [c["url"] for c in fake.calls] == [SYNC_URL]


def test_lost_lease_stops_before_tag_step():
    fake = Fake(responses=[SYNC_OK], renew=False)
    outcome, fake = run(fake=fake)
    assert outcome.kind == "lost_lease" and [c["url"] for c in fake.calls] == [SYNC_URL]


# --- responses / failures -------------------------------------------------------


@pytest.mark.parametrize("response", [(200, {}), (201, {"contact": {}}), (201, {"contact": {"id": "abc"}}), (201, {"contact": {"id": ""}}), (201, [])])
def test_missing_contact_id_is_retried_without_tagging(response):
    outcome, fake = run(fake=Fake(responses=[response]))
    assert (outcome.kind, outcome.error) == ("retry", "contact: response_missing_id")
    assert [c["url"] for c in fake.calls] == [SYNC_URL]


@pytest.mark.parametrize("response", [(200, {"contact": {"id": "7"}}), (201, {"contact": {"id": 7}})])
def test_created_or_updated_contact_id_parsed(response):
    outcome, fake = run(fake=Fake(responses=[response]))
    assert outcome.kind == "delivered" and fake.calls[1]["json"]["contactTag"]["contact"] == 7


class StatusFake:
    """``request`` (sync) and ``post`` (tag) seams sharing one ordered result list."""

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def _next(self, url):
        self.calls.append(url)
        r = self.results.pop(0) if self.results else (200,)
        if isinstance(r, Exception):
            raise r
        return r

    def ctx(self):
        def request(method, url, data, headers):
            r = self._next(url)
            return (r[0], json.dumps({"contact": {"id": "7"}}).encode(), *r[1:])

        def post(url, data, headers):
            r = self._next(url)
            return (r[0], "", *r[1:])

        return ni.NativeContext(post=post, request=request, renew=lambda: True)


@pytest.mark.parametrize("tag_status", [201, 200])
def test_tag_applied_or_already_applied_is_delivered(tag_status):
    # 200 "already applied" is documented: re-applying the same tag on a retry is safe.
    fake = StatusFake((201,), (tag_status,))
    assert ni.execute("activecampaign", AC, body(), "registration.created", fake.ctx()).kind == "delivered"
    assert fake.calls == [SYNC_URL, TAG_URL]


@pytest.mark.parametrize(
    "result,kind,error",
    [
        ((429, 30), "retry", "http_status: 429"),
        ((500,), "retry", "http_status: 500"),
        ((503, 7), "retry", "http_status: 503"),
        ((408,), "retry", "http_status: 408"),
        ((302,), "retry", "redirect_not_followed: http 302"),
        ((400,), "dead", "http_status: 400"),
        ((401,), "dead", "http_status: 401"),
        ((403,), "dead", "http_status: 403"),
        ((404,), "dead", "http_status: 404"),
        ((422,), "dead", "http_status: 422"),
        (TimeoutError(), "retry", "timeout"),
        (ConnectionResetError(KEY), "retry", "transport_error: ConnectionResetError"),
        (ValueError(f"echo {KEY} person@example.com"), "retry", "transport_error: ValueError"),
    ],
)
@pytest.mark.parametrize("step", ["contact", "tag"])
def test_failures_stop_and_never_echo_secrets(step, result, kind, error):
    results = [result] if step == "contact" else [(201,), result]
    fake = StatusFake(*results)
    outcome = ni.execute("activecampaign", AC, body(), "registration.created", fake.ctx())
    assert (outcome.kind, outcome.error) == (kind, f"{step}: {error}")
    assert KEY not in outcome.error and "@" not in outcome.error
    assert len(fake.calls) == len(results)
    if isinstance(result, tuple) and len(result) > 1:
        assert outcome.retry_after == result[1]


def test_retry_resends_identical_email_keyed_requests():
    first = [c["json"] for c in run()[1].calls]
    again = [c["json"] for c in run()[1].calls]
    assert first == again and first[0]["contact"]["email"] == "person@example.com"


# --- preview ------------------------------------------------------------------


def test_preview_redacts_api_token():
    calls = ni.preview("activecampaign", AC)
    assert [c["url"] for c in calls] == [SYNC_URL, TAG_URL]
    assert KEY not in repr(calls) and all(c["headers"]["Api-Token"] == ni.REDACTED for c in calls)
    assert calls[1]["body"] == {"contactTag": {"contact": 1, "tag": 20}}


def test_preview_empty_without_host(settings):
    settings.NATIVE_VENDOR_HOSTS_IMPLICIT = False
    settings.WEBHOOK_HOST_ALLOWLIST = ["api-us1.com"]
    assert ni.preview("activecampaign", AC) == []
    assert ni.preview_refusal("activecampaign", AC) is None  # validate reports it


def test_valid_preview_has_no_refusal():
    assert ni.preview("activecampaign", AC) and ni.preview_refusal("activecampaign", AC) is None
    assert len(ni.preview("activecampaign", NO_TAGS)) == 1 and ni.preview_refusal("activecampaign", NO_TAGS) is None


@pytest.mark.parametrize(
    "creds, code",
    [
        ({**AC, "native_email_merge_ack": ""}, "native_match_strategy_unconfirmed"),
        ({k: v for k, v in AC.items() if k != "native_email_merge_ack"}, "native_match_strategy_unconfirmed"),
        ({**AC, "native_field_map": "company=12,jobTitle=12"}, "native_config_invalid"),
        ({**AC, "native_tag_registered": "x"}, "native_config_invalid"),
    ],
)
def test_refused_preview_keeps_empty_list_and_reports_bounded_code(creds, code):
    assert ni.preview("activecampaign", creds) == []
    refusal = ni.preview_refusal("activecampaign", creds)
    assert refusal == (code, ni.PREVIEW_REFUSALS[code])
    assert KEY not in repr(refusal) and "person@example.com" not in repr(refusal)


# --- delivery (host pinning, consent gate, config change) ------------------------


class RecordingRequest:
    """Stands in for ``ssrf.request_json_bounded``: applies its host/credential policy, records calls."""

    def __init__(self, on_first=None, status=200):
        self.calls, self.on_first, self.status = [], on_first, status

    def __call__(self, method, url, data, headers, timeout, total, max_bytes, allowed_hosts=None):
        host = webinars.ssrf._validated_target(url)[0]
        webinars.ssrf._check_native_request(method, host, headers, allowed_hosts)
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "data": data, "allowed": allowed_hosts})
        if len(self.calls) == 1 and self.on_first:
            self.on_first()
        payload = {"contact": {"id": "7"}} if url.endswith("/contact/sync") else {}
        return self.status, json.dumps(payload).encode(), None, False


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
    msg, reg = _claimed(occurrence, AC, include_private_links=True)
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED and msg.attempts == 1
    assert [c["url"] for c in transport.calls] == [SYNC_URL, TAG_URL]
    assert all(c["allowed"] == frozenset({HOST}) and c["headers"]["Api-Token"] == KEY for c in transport.calls)
    assert all(b"PRIVATE123" not in c["data"] for c in transport.calls)
    assert reg.person.email.encode() in transport.calls[0]["data"]
    assert KEY not in caplog.text and KEY not in msg.last_error


@pytest.mark.parametrize("status,expected", [(503, S.PENDING), (400, S.DEAD)])
def test_e2e_failures(occurrence, transport, status, expected):
    transport.status = status
    msg, _ = _claimed(occurrence, AC)
    msg = _deliver(msg)
    assert msg.status == expected and msg.attempts == 1 and msg.last_status_code == status
    assert msg.last_error == f"contact: http_status: {status}" and KEY not in msg.last_error


def test_e2e_operator_required_consent_blocks_before_first_step(occurrence, transport):
    msg, _ = _claimed(occurrence, AC)
    Destination.objects.filter(pk=msg.destination_id).update(required_consents=["marketing"])
    msg = _deliver(msg)
    assert transport.calls == [] and msg.status == S.CANCELLED


@pytest.mark.parametrize("target", ["https://api.segment.io", "https://hooks.example.net", "https://other.api-us1.com"])
def test_e2e_never_sent_to_another_host(occurrence, monkeypatch, settings, transport, caplog, target):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", HOST, "api.segment.io", "other.api-us1.com"]
    msg, _ = _claimed(occurrence, AC)
    monkeypatch.setattr(ni, "base_url", lambda provider, credentials: target)
    msg = _deliver(msg)
    assert transport.calls == []
    assert msg.status == S.DEAD and msg.last_error.endswith(": unsafe_destination")
    assert KEY not in msg.last_error and KEY not in caplog.text


def test_e2e_config_change_after_sync_stops_before_tag(occurrence, monkeypatch):
    msg, _ = _claimed(occurrence, AC)
    changed = {**AC, "native_tag_registered": "99"}
    from webinars.crypto import encrypt_secret

    fake = RecordingRequest(lambda: Destination.objects.filter(pk=msg.destination_id).update(credentials_encrypted=encrypt_secret(json.dumps(changed))))
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg = _deliver(msg)
    assert len(fake.calls) == 1 and fake.calls[0]["url"] == SYNC_URL  # the sync is not recalled; no stale tag call
    assert msg.status == S.DEAD and msg.last_error == "native_config_changed_mid_sequence"


def test_e2e_without_request_transport_dead(occurrence, monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    msg, _ = _claimed(occurrence, AC)
    msg = _deliver(msg)
    assert msg.status == S.DEAD and msg.last_error == "native_transport_unavailable"


@pytest.mark.parametrize("host,ok", [(HOST, True), ("evil" + HOST, True), (HOST + ".evil.net", False), ("acme.example.com", False)])
def test_vendor_domain_host_needs_no_operator_entry_by_default(settings, host, ok):
    settings.WEBHOOK_HOST_ALLOWLIST = []
    creds = {**AC, "native_api_host": host}
    errors = ni.validate("activecampaign", creds, **KW)
    if ok:
        assert errors == {} and ni.allowed_hosts("activecampaign", creds) == frozenset({host})
        assert ni.check_send_policy("activecampaign", creds, dest()) is None
    else:
        assert "native-api_host" in errors and "provider" not in errors
        assert ni.allowed_hosts("activecampaign", creds) == frozenset()
