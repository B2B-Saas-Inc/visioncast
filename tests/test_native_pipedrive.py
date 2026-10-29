"""Pipedrive native adapter: mocked v2 search/write/activity contracts, dedupe on retry, host policy, preview and delivery.

No network: transports are fakes and socket connects raise. Request shapes follow the Pipedrive
API v2 OpenAPI specification fetched 2026-10-27 (docs/native-integrations.md); nothing here has
been checked against a real Pipedrive account.
"""
import json
import socket

import pytest

import webinars.delivery
import webinars.ssrf
from tests.test_native_integrations import _claimed, body
from tests.test_native_salesforce import ReadFake
from webinars import native_integrations as ni
from webinars.crypto import encrypt_secret
from webinars.models import Destination, OutboxMessage
from webinars.native_providers import pipedrive as pd
from webinars.ssrf import UnsafeDestination

pytestmark = pytest.mark.django_db

HOST = "api.pipedrive.com"
TOKEN = "pd-api-token-secret-0123456789abcdef"
PID_FIELD = "0123456789abcdef0123456789abcdef01234567"
ORG_FIELD = "fedcba9876543210fedcba9876543210fedcba98"
PID = ni.SAMPLE_BODY["identity"]["personId"]
RID = ni.SAMPLE_BODY["registration"]["id"]
PD = {
    "native_provider": "pipedrive",
    "native_region": "global",
    "native_api_token": TOKEN,
    "native_person_match": "custom_id",
    "native_person_id_field": PID_FIELD,
    "native_org_id_field": ORG_FIELD,
    "native_org_namespace": "crm",
    "native_activity_type": "meeting",
    "native_processing_ack": "confirmed",
}
EMAIL_MODE = {**PD, "native_person_match": "email", "native_email_merge_ack": "confirmed"}
NO_ORG = {**PD, "native_org_id_field": "", "native_org_namespace": ""}
KW = dict(event_types=["registration.created"], allowed_groups=list(Destination.GROUPS), required_consents=[])
S = OutboxMessage.Status
BASE = f"https://{HOST}/api/v2"
MARKER = f"webinar-registration:{RID}:registration.created"

NOT_FOUND = (200, {"success": True, "data": {"items": []}, "additional_data": {"next_cursor": None}})


def found(record_id):
    return (200, {"success": True, "data": {"items": [{"result_score": 1.0, "item": {"id": record_id, "type": "person"}}]}})


def created(record_id):
    return (200, {"success": True, "data": {"id": record_id}})


def activities(*notes):
    return (200, {"success": True, "data": [{"id": 900 + i, "subject": "x", "note": n, "done": False} for i, n in enumerate(notes)]})


CREATE_ALL = (NOT_FOUND, created(77), NOT_FOUND, created(42), activities(), created(9))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", HOST, "api.hubapi.com"]


def run(creds=PD, b=None, event_type="registration.created", responses=CREATE_ALL, fake=None):
    fake = fake or ReadFake(list(responses))
    return ni.execute("pipedrive", creds, b if b is not None else body(), event_type, fake.ctx()), fake


def dest(**kw):
    return Destination(**{**KW, **kw})


# --- schema / credentials -----------------------------------------------------


def test_offered_and_schema_driven():
    assert dict(ni.PROVIDER_CHOICES)["pipedrive"] == "Pipedrive"
    assert ni.FIELDS["pipedrive"][0].name == "region" and ni.FIELDS["pipedrive"][0].choices == (("global", HOST),)
    assert ni.PROCESSING_ACK in ni.FIELDS["pipedrive"] and ni.PERSON_MATCH in ni.FIELDS["pipedrive"] and ni.EMAIL_MERGE_ACK in ni.FIELDS["pipedrive"]
    assert ni.HOSTS["pipedrive"] == {"global": HOST} and HOST in ni.VENDOR_HOSTS
    assert "pipedrive" not in ni.ANALYTICS_PROVIDERS and "pipedrive" in ni.NEEDS_REQUEST_TRANSPORT and "pipedrive" not in ni.NEEDS_DESTINATION_HOSTS
    assert ni.CUSTOM_ID_FIELD["pipedrive"] == "person_id_field" and ni.ORG_TARGETS["pipedrive"] == "org_id_field"
    assert "x-api-token" in ni.SECRET_HEADERS and "x-api-token" in webinars.ssrf.CREDENTIAL_HEADERS


def test_credential_is_set_only():
    assert TOKEN not in repr(ni.config_of(PD, "pipedrive")) and ni.secrets_set(PD, "pipedrive") == {"api_token": True}
    posted = {f.name: ni.config_of(PD, "pipedrive").get(f.name, "") for f in ni.FIELDS["pipedrive"]}
    posted["api_token"] = ""
    out, _ = ni.merge_credentials(PD, "pipedrive", posted)
    assert out["native_api_token"] == TOKEN
    out, notes = ni.merge_credentials(PD, "hubspot", {"region": "global", "access_token": ""})
    assert TOKEN not in out.values() and notes


@pytest.mark.parametrize("creds", [PD, EMAIL_MODE, NO_ORG, {**PD, "native_activity_type": ""}])
def test_valid_configs(creds):
    assert ni.validate("pipedrive", creds, **KW) == {}
    assert ni.check_send_policy("pipedrive", creds, dest()) is None


@pytest.mark.parametrize("key", ["native_processing_ack", "native_api_token", "native_person_match", "native_person_id_field"])
def test_required_fields(key):
    creds = {k: v for k, v in PD.items() if k != key}
    assert f"native-{key[len('native_'):]}" in ni.validate("pipedrive", creds, **KW)
    assert ni.check_send_policy("pipedrive", creds, dest()) == "native_config_invalid"  # delivery gates on this before execute


@pytest.mark.parametrize("key,error", [("native_api_token", "native_config_incomplete"), ("native_person_match", "native_match_strategy_unconfirmed"), ("native_person_id_field", "native_config_incomplete")])
def test_execute_refuses_incomplete_config_without_calls(key, error):
    outcome, fake = run({k: v for k, v in PD.items() if k != key})
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", error, [])


def test_email_mode_needs_confirmation():
    creds = {**EMAIL_MODE, "native_email_merge_ack": ""}
    assert "native-email_merge_ack" in ni.validate("pipedrive", creds, **KW)
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_match_strategy_unconfirmed", [])


@pytest.mark.parametrize("field,value", [("person_id_field", "abc"), ("person_id_field", "G" * 40), ("org_id_field", "0123456789abcdef0123456789abcdef0123456"), ("activity_type", "Meeting Type"), ("activity_type", "1call")])
def test_field_formats(field, value):
    assert f"native-{field}" in ni.validate("pipedrive", {**PD, f"native_{field}": value}, **KW)


def test_org_namespace_and_field_together():
    assert "native-org_namespace" in ni.validate("pipedrive", {**PD, "native_org_namespace": ""}, **KW)
    assert "native-org_namespace" in ni.validate("pipedrive", {**PD, "native_org_id_field": ""}, **KW)


@pytest.mark.parametrize("region", ["eu", "us", ""])
def test_unknown_region_has_no_host(region):
    creds = {**PD, "native_region": region}
    assert ni.validate("pipedrive", creds, **KW)
    assert ni.base_url("pipedrive", creds) is None and ni.allowed_hosts("pipedrive", creds) == frozenset()
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])


def test_host_must_be_operator_allowlisted(settings):
    settings.NATIVE_VENDOR_HOSTS_IMPLICIT = False  # operator opted out of implicit vendor hosts
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    assert any(HOST in m for m in ni.validate("pipedrive", PD, **KW)["provider"])
    assert ni.check_send_policy("pipedrive", PD, dest()) == "native_config_invalid"


def test_allowed_hosts_pins_exact_host():
    assert ni.allowed_hosts("pipedrive", PD) == frozenset({HOST})


def test_api_token_header_is_a_credential_for_the_transport():
    for header in ("x-api-token", "X-Api-Token"):
        with pytest.raises(UnsafeDestination):
            webinars.ssrf._check_native_request("POST", "hooks.example.net", {header: TOKEN}, None)
        with pytest.raises(UnsafeDestination):
            webinars.ssrf._check_native_request("GET", "hooks.example.net", {header: TOKEN}, frozenset({HOST}))
    webinars.ssrf._check_native_request("PATCH", HOST, {"x-api-token": TOKEN}, frozenset({HOST}))


# --- request contract ---------------------------------------------------------


def test_exact_requests_create_everything():
    outcome, fake = run()
    assert outcome.kind == "delivered" and len(fake.calls) == 6
    for call in fake.calls:
        assert call["url"].startswith(BASE + "/") and call["headers"]["x-api-token"] == TOKEN and "Authorization" not in call["headers"]
        assert call["headers"]["Accept"] == "application/json"
    org_search, org_create, person_search, person_create, activity_list, activity_create = fake.calls
    assert (org_search["method"], org_search["url"]) == ("GET", f"{BASE}/organizations/search?term=acme-1&fields=custom_fields&exact_match=true&limit=1")
    assert org_search["raw"] == b"" and org_search["json"] is None
    assert (org_create["method"], org_create["url"]) == ("POST", f"{BASE}/organizations")
    assert org_create["json"] == {"name": "Example Co", "custom_fields": {ORG_FIELD: "acme-1"}}
    assert (person_search["method"], person_search["url"]) == ("GET", f"{BASE}/persons/search?term={PID}&fields=custom_fields&exact_match=true&limit=1")
    assert (person_create["method"], person_create["url"]) == ("POST", f"{BASE}/persons")
    assert person_create["json"] == {
        "name": "Sample Person",
        "custom_fields": {PID_FIELD: PID},
        "org_id": 77,
        "emails": [{"value": "person@example.com", "primary": True, "label": "work"}],
    }
    assert (activity_list["method"], activity_list["url"]) == ("GET", f"{BASE}/activities?person_id=42&limit=500&sort_by=add_time&sort_direction=desc")
    assert (activity_create["method"], activity_create["url"]) == ("POST", f"{BASE}/activities")
    assert activity_create["json"] == {
        "subject": "Webinar registered: Sample webinar",
        "type": "meeting",
        "due_date": "2026-01-02",
        "due_time": "12:00:00",
        "note": MARKER,
        "org_id": 77,
        "done": False,
        "participants": [{"person_id": 42, "primary": True}],
    }


def test_found_records_are_updated_not_recreated():
    outcome, fake = run(responses=(found(77), found(42), (200, {"success": True, "data": {}}), activities("other", MARKER)))
    assert outcome.kind == "delivered" and len(fake.calls) == 4
    org_search, person_search, person_update, activity_list = fake.calls
    assert org_search["method"] == person_search["method"] == "GET"
    assert (person_update["method"], person_update["url"]) == ("PATCH", f"{BASE}/persons/42")  # 2xx is enough for an update
    assert person_update["json"] == {"name": "Sample Person", "custom_fields": {PID_FIELD: PID}, "org_id": 77}  # emails never rewritten
    assert activity_list["method"] == "GET"  # the marker exists: no second activity


def test_email_mode_searches_by_email():
    outcome, fake = run(EMAIL_MODE, responses=(NOT_FOUND, created(77), found(42), created(42), activities(), created(9)))
    assert outcome.kind == "delivered"
    person_search = fake.calls[2]
    assert person_search["url"] == f"{BASE}/persons/search?term=person%40example.com&fields=email&exact_match=true&limit=1"
    assert fake.calls[3]["method"] == "PATCH" and fake.calls[3]["json"]["custom_fields"] == {PID_FIELD: PID}  # configured field still written


def test_email_mode_without_person_id_field_sends_no_custom_fields():
    creds = {**EMAIL_MODE, "native_person_id_field": ""}
    _, fake = run(creds, responses=(NOT_FOUND, created(77), NOT_FOUND, created(42), activities(), created(9)))
    assert "custom_fields" not in fake.calls[3]["json"]


def test_without_organization_four_calls():
    outcome, fake = run(NO_ORG, responses=(NOT_FOUND, created(42), activities(), created(9)))
    assert outcome.kind == "delivered" and [c["method"] for c in fake.calls] == ["GET", "POST", "GET", "POST"]
    assert "org_id" not in fake.calls[1]["json"] and "org_id" not in fake.calls[3]["json"]


@pytest.mark.parametrize(
    "org_ids",
    [
        ({"namespace": "crm", "value": "acme-1", "verified": False, "source": "browser"},),
        ({"namespace": "crm", "value": "acme-1", "verified": True, "source": "trusted_api"}, {"namespace": "crm", "value": "acme-2", "verified": True, "source": "trusted_api"}),
        (),
        ({"namespace": "crm", "value": "acme-1", "verified": True, "source": "browser"},),
    ],
)
def test_no_organization_without_single_verified_org(org_ids):
    outcome, fake = run(PD, body(org_ids), responses=(NOT_FOUND, created(42), activities(), created(9)))
    assert outcome.kind == "delivered" and "organizations" not in " ".join(c["url"] for c in fake.calls)
    assert "Example Co" not in json.dumps([c["json"] for c in fake.calls])  # company names are never a match key


def test_organization_name_falls_back_to_org_id():
    b = body()
    b["profile"].pop("company")
    _, fake = run(PD, b)
    assert fake.calls[1]["json"] == {"name": "acme-1", "custom_fields": {ORG_FIELD: "acme-1"}}


@pytest.mark.parametrize(
    "event_type,attendance,subject,done",
    [
        ("registration.updated", "unknown", "Webinar registration updated: Sample webinar", False),
        ("attendance.checked_in", "unknown", "Webinar checked in: Sample webinar", True),
        ("attendance.recorded", "attended", "Webinar attended: Sample webinar", True),
        ("attendance.recorded", "no_show", "Webinar no-show: Sample webinar", True),
    ],
)
def test_activity_subjects_and_done(event_type, attendance, subject, done):
    b = body()
    b["registration"]["attendance"] = attendance
    _, fake = run(PD, b, event_type)
    activity = fake.calls[-1]["json"]
    assert activity["subject"] == subject and activity["done"] is done
    assert activity["note"] == f"webinar-registration:{RID}:{event_type}"


def test_unknown_attendance_skipped_before_any_call():
    b = body()
    b["registration"]["attendance"] = "weird"
    outcome, fake = run(PD, b, "attendance.recorded")
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_attendance_unknown", [])


def test_due_date_omitted_when_start_unparseable_and_subject_without_title():
    b = body()
    b["event"]["occurrence"]["startsAt"] = "soon"
    b["event"].pop("title")
    activity = run(PD, b)[1].calls[-1]["json"]
    assert "due_date" not in activity and "due_time" not in activity and activity["subject"] == "Webinar registered"


def test_due_time_is_utc():
    b = body()
    b["event"]["occurrence"]["startsAt"] = "2026-03-01T23:30:00+02:00"
    activity = run(PD, b)[1].calls[-1]["json"]
    assert (activity["due_date"], activity["due_time"]) == ("2026-03-01", "21:30:00")


def test_never_sends_marketing_status_job_title_phone_or_urls():
    b = body()
    b["profile"].update({"phone": "+15550100", "customFields": {"q": "SECRET1"}})
    b["event"]["occurrence"]["joinUrl"] = "https://zoom.example/j/SECRET2"
    for call in run(PD, b)[1].calls:
        text = json.dumps(call["json"])
        assert not {"marketing_status", "job_title", "phones", "notes", "label_ids", "deal_id", "lead_id"} & set(call["json"] or {})
        assert "+15550100" not in text and "SECRET" not in text and "https://" not in text


# --- identity / inputs / consent ------------------------------------------------


def test_unverified_identity_never_sent():
    for mutate in (lambda b: b["identity"].update(verified=False), lambda b: b["registration"].update(identityVerified=False)):
        b = body()
        mutate(b)
        outcome, fake = run(PD, b)
        assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_identity_unverified", [])


def test_email_mode_without_email_skipped():
    b = body()
    b["profile"].pop("email")
    outcome, fake = run(EMAIL_MODE, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_no_email", [])


def test_name_falls_back_to_identity_email_then_skips():
    b = body()
    b.pop("profile")
    _, fake = run(PD, b)
    assert fake.calls[3]["json"]["name"] == "person@example.com" and fake.calls[3]["json"]["emails"][0]["value"] == "person@example.com"
    b["identity"].pop("email")
    outcome, fake = run(PD, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_person_name_missing", [])


@pytest.mark.parametrize(
    "creds,mutate,step",
    [
        (PD, lambda b: b["identity"].update(personId="id with spaces"), "person_search"),
        (PD, lambda b: b["identity"].update(personId="a/b?c=d"), "person_search"),
        (EMAIL_MODE, lambda b: b["profile"].update(email="not an email"), "person_search"),
        (EMAIL_MODE, lambda b: b["profile"].update(email="a@b.c?x=1"), "person_search"),
        (PD, lambda b: b["organization"]["externalIds"][0].update(value="acme 1"), "organization_search"),
    ],
)
def test_search_terms_refused_before_any_call(creds, mutate, step):
    b = body()
    mutate(b)
    outcome, fake = run(creds, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", f"{step}: payload_unsupported", [])


def test_nested_profile_value_refused_before_any_call():
    b = body()
    b["profile"]["firstName"] = {"given": "Sample"}
    outcome, fake = run(PD, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "person: payload_unsupported", [])


def test_body_size_limit():
    b = body()
    b["profile"]["firstName"] = "x" * pd.MAX_BODY_BYTES
    outcome, fake = run(NO_ORG, b, responses=(NOT_FOUND,))
    assert (outcome.kind, outcome.error, [c["method"] for c in fake.calls]) == ("dead", "person: payload_too_large", ["GET"])


def test_consent_rechecked_before_every_step():
    fake = ReadFake(list(CREATE_ALL))
    stop = ni.Outcome("cancelled", "consent_not_granted_mid_sequence")
    ctx = ni.NativeContext(post=fake.post, request=fake.request, renew=fake.renew, check=lambda: stop)
    outcome = ni.execute("pipedrive", PD, body(), "registration.created", ctx)
    assert outcome == stop and len(fake.calls) == 1


def test_six_calls_is_the_budget():
    fake = ReadFake(list(CREATE_ALL))
    ctx = fake.ctx()
    assert ni.execute("pipedrive", PD, body(), "registration.created", ctx).kind == "delivered" and ctx.calls == 6 == ni.MAX_CALLS


# --- responses / failures -------------------------------------------------------


@pytest.mark.parametrize("step_index,step", [(0, "organization_search"), (1, "organization"), (2, "person_search"), (3, "person"), (4, "activities"), (5, "activity")])
@pytest.mark.parametrize(
    "result,kind,error",
    [
        ((429, {}, 42), "retry", "http_status: 429"),
        ((500, {}), "retry", "http_status: 500"),
        ((302, {}), "retry", "redirect_not_followed: http 302"),
        ((400, {"error": f"bad {TOKEN}"}), "dead", "http_status: 400"),
        ((401, {}), "dead", "http_status: 401"),
        ((403, {}), "dead", "http_status: 403"),
        (TimeoutError(), "retry", "timeout"),
        (ConnectionResetError(TOKEN), "retry", "transport_error: ConnectionResetError"),
    ],
)
def test_failures_stop_at_the_failing_step_and_never_echo_secrets(step_index, step, result, kind, error):
    responses = list(CREATE_ALL[:step_index]) + [result]
    fake = ReadFake([])

    def request(method, url, data, headers):
        r = responses.pop(0)
        fake.calls.append(url)
        if isinstance(r, Exception):
            raise r
        return (r[0], json.dumps(r[1]).encode()) + tuple(r[2:])

    ctx = ni.NativeContext(post=None, request=request, renew=lambda: True)
    outcome = ni.execute("pipedrive", PD, body(), "registration.created", ctx)
    assert (outcome.kind, outcome.error) == (kind, f"{step}: {error}")
    assert TOKEN not in outcome.error and "@" not in outcome.error and len(fake.calls) == step_index + 1
    if isinstance(result, tuple) and len(result) > 2:
        assert outcome.retry_after == result[2]


@pytest.mark.parametrize("step_index,step", [(1, "organization"), (3, "person"), (5, "activity")])
def test_create_without_id_is_retried(step_index, step):
    responses = list(CREATE_ALL)
    responses[step_index] = (200, {"success": True, "data": {}})
    outcome, fake = run(responses=responses)
    assert (outcome.kind, outcome.error, len(fake.calls)) == ("retry", f"{step}: response_missing_id", step_index + 1)


@pytest.mark.parametrize("payload", [{"success": True, "data": None}, {"success": True, "data": {"items": "x"}}, {"data": {"items": [{"item": {}}]}}, []])
def test_odd_search_responses_mean_not_found(payload):
    outcome, fake = run(NO_ORG, responses=((200, payload), created(42), activities(), created(9)))
    assert outcome.kind == "delivered" and fake.calls[1]["method"] == "POST"


@pytest.mark.parametrize("payload", [{"success": True, "data": None}, {"success": True, "data": {"id": 1}}, {"data": [{"note": None}, {"nope": 1}, "x"]}])
def test_odd_activity_lists_do_not_block_creation(payload):
    outcome, fake = run(NO_ORG, responses=(found(42), created(42), (200, payload), created(9)))
    assert outcome.kind == "delivered" and fake.calls[-1]["url"] == f"{BASE}/activities" and fake.calls[-1]["method"] == "POST"


def test_retry_after_partial_failure_converges_without_duplicates():
    # First attempt: organization and person are created, the activity list fails.
    first, f1 = run(responses=(NOT_FOUND, created(77), NOT_FOUND, created(42), (503, {})))
    assert (first.kind, first.error) == ("retry", "activities: http_status: 503")
    assert [c["method"] for c in f1.calls] == ["GET", "POST", "GET", "POST", "GET"]
    # Retry: both now exist and the activity was never created, so one activity is created and nothing is recreated.
    second, f2 = run(responses=(found(77), found(42), created(42), activities(), created(9)))
    assert second.kind == "delivered"
    assert [(c["method"], c["url"]) for c in f2.calls] == [
        ("GET", f1.calls[0]["url"]),
        ("GET", f1.calls[2]["url"]),
        ("PATCH", f"{BASE}/persons/42"),
        ("GET", f"{BASE}/activities?person_id=42&limit=500&sort_by=add_time&sort_direction=desc"),
        ("POST", f"{BASE}/activities"),
    ]
    assert f2.calls[-1]["json"]["note"] == MARKER and "POST" not in [c["method"] for c in f2.calls[:3]]  # no plain re-create
    # A third delivery (timeout after the activity was accepted) finds the marker and creates nothing.
    third, f3 = run(responses=(found(77), found(42), created(42), activities(MARKER)))
    assert third.kind == "delivered" and [c["method"] for c in f3.calls] == ["GET", "GET", "PATCH", "GET"]


def test_marker_match_is_exact_per_registration_and_event():
    outcome, fake = run(responses=(found(77), found(42), created(42), activities(f"webinar-registration:{RID}:registration.updated", "webinar-registration:other:registration.created"), created(9)))
    assert outcome.kind == "delivered" and fake.calls[-1]["method"] == "POST"


# --- preview ------------------------------------------------------------------


def test_preview_redacts_api_token():
    calls = ni.preview("pipedrive", PD)
    assert [c["method"] for c in calls] == ["GET", "POST", "GET", "POST", "GET", "POST"]
    assert TOKEN not in repr(calls) and all(c["headers"]["x-api-token"] == ni.REDACTED for c in calls)
    assert calls[1]["url"] == f"{BASE}/organizations" and calls[3]["url"] == f"{BASE}/persons" and calls[5]["url"] == f"{BASE}/activities"
    assert calls[5]["body"]["participants"] == [{"person_id": "<returned id>", "primary": True}]
    assert ni.preview_refusal("pipedrive", PD) is None


def test_preview_empty_without_host():
    assert ni.preview("pipedrive", {**PD, "native_region": "eu"}) == [] and ni.preview_refusal("pipedrive", {**PD, "native_region": "eu"}) is None


@pytest.mark.parametrize(
    "creds,code",
    [({**EMAIL_MODE, "native_email_merge_ack": ""}, "native_match_strategy_unconfirmed"), ({**PD, "native_person_id_field": ""}, "native_config_incomplete")],
)
def test_refused_preview_reports_bounded_code(creds, code):
    assert ni.preview("pipedrive", creds) == []
    refusal = ni.preview_refusal("pipedrive", creds)
    assert refusal == (code, ni.PREVIEW_REFUSALS[code]) and TOKEN not in repr(refusal)


# --- delivery -----------------------------------------------------------------


class RequestTransport:
    """Stands in for ``ssrf.request_json_bounded``: answers by method and path, records the pinned host set."""

    def __init__(self, on_first=None, fail=None):
        self.calls, self.on_first, self.fail = [], on_first, fail

    def __call__(self, method, url, data, headers, timeout, total, max_bytes, allowed_hosts=None):
        self.calls.append({"method": method, "url": url, "data": data, "headers": dict(headers), "allowed": allowed_hosts})
        if len(self.calls) == 1 and self.on_first:
            self.on_first()
        if self.fail and self.fail[0] in url:
            return self.fail[1], b"{}", None, False
        if "/search?" in url:
            return 200, json.dumps(NOT_FOUND[1]).encode(), None, False
        if method == "GET":
            return 200, json.dumps(activities()[1]).encode(), None, False
        return 200, json.dumps(created(len(self.calls))[1]).encode(), None, False


def _deliver(msg):
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    return msg


def test_e2e_delivered_to_pinned_host(occurrence, monkeypatch, caplog):
    fake = RequestTransport()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg, reg = _claimed(occurrence, NO_ORG, consent=False)
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED and msg.attempts == 1
    assert [(c["method"], c["url"].split("?")[0]) for c in fake.calls] == [("GET", f"{BASE}/persons/search"), ("POST", f"{BASE}/persons"), ("GET", f"{BASE}/activities"), ("POST", f"{BASE}/activities")]
    assert all(c["allowed"] == frozenset({HOST}) and c["headers"]["x-api-token"] == TOKEN for c in fake.calls)
    assert fake.calls[0]["data"] in (b"", None) and reg.person.email.encode() in fake.calls[1]["data"]
    assert f"webinar-registration:{reg.id}:registration.created".encode() in fake.calls[3]["data"]
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("status,expected", [(503, S.PENDING), (400, S.DEAD)])
def test_e2e_failures(occurrence, monkeypatch, status, expected, caplog):
    fake = RequestTransport(fail=("/persons/search", status))
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg, _ = _claimed(occurrence, NO_ORG, consent=False)
    msg = _deliver(msg)
    assert msg.status == expected and msg.last_error == f"person_search: http_status: {status}" and len(fake.calls) == 1
    assert TOKEN not in msg.last_error and TOKEN not in caplog.text


def test_e2e_operator_required_consent_blocks_before_first_call(occurrence, monkeypatch):
    fake = RequestTransport()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg, _ = _claimed(occurrence, NO_ORG, consent=False)
    Destination.objects.filter(pk=msg.destination_id).update(required_consents=["sms"])
    msg = _deliver(msg)
    assert fake.calls == [] and msg.status == S.CANCELLED


@pytest.mark.parametrize("target", ["https://api.hubapi.com", "https://hooks.example.net"])
def test_e2e_token_never_sent_to_another_host(target, occurrence, monkeypatch, caplog):
    msg, _ = _claimed(occurrence, NO_ORG, consent=False)
    monkeypatch.setattr(ni, "base_url", lambda provider, credentials: target)
    msg = _deliver(msg)  # the real transport refuses before any connection
    assert msg.status == S.DEAD and msg.last_error == "person_search: unsafe_destination"
    assert TOKEN not in msg.last_error and TOKEN not in caplog.text


@pytest.mark.parametrize(
    "mutate",
    [
        dict(credentials_encrypted=encrypt_secret(json.dumps({**NO_ORG, "native_activity_type": "call"}))),
        dict(allowed_groups=["identity", "registration", "event"]),
        dict(event_types=["registration.created", "registration.updated"]),
    ],
    ids=["credentials", "allowed_groups", "event_types"],
)
def test_e2e_config_change_after_first_call_stops_before_next(occurrence, monkeypatch, mutate):
    msg, _ = _claimed(occurrence, NO_ORG, consent=False)
    fake = RequestTransport(on_first=lambda: Destination.objects.filter(pk=msg.destination_id).update(**mutate))
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg = _deliver(msg)
    assert len(fake.calls) == 1 and msg.status == S.DEAD and msg.last_error == "native_config_changed_mid_sequence"


def test_e2e_without_request_transport_is_dead(occurrence, monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    msg, _ = _claimed(occurrence, NO_ORG, consent=False)
    msg = _deliver(msg)
    assert msg.status == S.DEAD and msg.last_error == "native_transport_unavailable"


def test_vendor_host_needs_no_operator_entry_by_default(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = []
    assert "provider" not in ni.validate("pipedrive", PD, **KW)
    assert ni.check_send_policy("pipedrive", PD, dest()) is None
