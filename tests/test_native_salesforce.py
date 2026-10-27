"""Salesforce native adapter: mocked token + composite contracts, host policy, identity, retries, preview and delivery.

No network: transports are fakes and socket connects raise. Request shapes follow the REST API
Developer Guide pages read on 2026-10-27 (docs/native-integrations.md); nothing here has been
checked against a real org or sandbox.
"""
import json
import socket
import uuid
from urllib.parse import parse_qsl

import pytest
from django.db import transaction

import webinars.delivery
import webinars.ssrf
from tests.conftest import make_destination, make_person, make_registration
from tests.test_native_integrations import Fake, body
from webinars import native_integrations as ni
from webinars.crypto import encrypt_secret
from webinars.hosting import base as hosting_base
from webinars.models import Destination, OutboxMessage
from webinars.native_providers import salesforce as sf

pytestmark = pytest.mark.django_db

HOST = "acme.my.salesforce.com"
CLIENT_ID = "3MVG9ConsumerKeyValue0000000"
CLIENT_SECRET = "ConsumerSecretValue1234567890ABC"
TOKEN = "00Dxx0000001gPz.AQEAQ.accessTokenValue"
PID = ni.SAMPLE_BODY["identity"]["personId"]
RID = ni.SAMPLE_BODY["registration"]["id"]
SF = {
    "native_provider": "salesforce",
    "native_region": "mydomain",
    "native_instance_host": HOST,
    "native_client_id": CLIENT_ID,
    "native_client_secret": CLIENT_SECRET,
    "native_person_match": "custom_id",
    "native_person_object": "Contact",
    "native_person_external_id_field": "Webinar_Person_Id__c",
    "native_account_external_id_field": "Webinar_Org_Id__c",
    "native_org_namespace": "crm",
    "native_participation_object": "Webinar_Participation__c",
    "native_participation_external_id_field": "Webinar_Registration_Id__c",
    "native_person_lookup_relationship": "Contact__r",
    "native_participation_field_map": "webinar_event_title=Event_Title__c,webinar_attendance=Attendance__c,webinar_starts_at=Starts_At__c",
    "native_processing_ack": "confirmed",
}
LEAD = {**SF, "native_person_object": "Lead", "native_account_external_id_field": "", "native_org_namespace": "", "native_person_lookup_relationship": "Lead__r"}
KW = dict(event_types=["registration.created"], allowed_groups=list(Destination.GROUPS), required_consents=[])
S = OutboxMessage.Status
SECRETS = (CLIENT_ID, CLIENT_SECRET, TOKEN)
TOKEN_URL = f"https://{HOST}/services/oauth2/token"
COMPOSITE_URL = f"https://{HOST}/services/data/{sf.API_VERSION}/composite"
TOKEN_OK = (200, {"access_token": TOKEN, "instance_url": f"https://{HOST}", "token_type": "Bearer", "issued_at": "1"})


def composite_ok(*refs, status=201):
    return (200, {"compositeResponse": [{"referenceId": r, "httpStatusCode": status, "httpHeaders": {}, "body": {"id": f"id-{r}", "success": True, "errors": []}} for r in refs]})


COMPOSITE_OK = composite_ok("account", "person", "participation")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", HOST, "api.hubapi.com", "api.segment.io"]


@pytest.fixture(autouse=True)
def fresh_token_cache():
    hosting_base.clear_token_cache()
    yield
    hosting_base.clear_token_cache()


class ReadFake(Fake):
    """``Fake`` that also records form-encoded (token) and bodiless (GET) requests."""

    def request(self, method, url, data, headers):
        try:
            parsed = json.loads(data) if data else None
        except ValueError:
            parsed = dict(parse_qsl(data.decode()))
        self.calls.append({"method": method, "url": url, "json": parsed, "headers": dict(headers), "raw": data})
        r = self._next()
        status, payload = r if isinstance(r, tuple) else (r, {})
        return status, json.dumps(payload).encode()


def run(creds=SF, b=None, event_type="registration.created", responses=(TOKEN_OK, COMPOSITE_OK), fake=None):
    fake = fake or ReadFake(list(responses))
    return ni.execute("salesforce", creds, b if b is not None else body(), event_type, fake.ctx()), fake


def dest(**kw):
    return Destination(**{**KW, **kw})


def subrequests(fake):
    return {r["referenceId"]: r for r in fake.calls[-1]["json"]["compositeRequest"]}


# --- schema / credentials -----------------------------------------------------


def test_offered_and_schema_driven():
    assert dict(ni.PROVIDER_CHOICES)["salesforce"] == "Salesforce"
    assert ni.FIELDS["salesforce"][0].name == "region" and ni.FIELDS["salesforce"][0].choices
    assert ni.PROCESSING_ACK in ni.FIELDS["salesforce"] and ni.ORG_NAMESPACE in ni.FIELDS["salesforce"]
    assert "salesforce" not in ni.HOSTS and "salesforce" in ni.NEEDS_DESTINATION_HOSTS  # per-destination host
    assert "salesforce" in ni.NEEDS_REQUEST_TRANSPORT and "salesforce" not in ni.ANALYTICS_PROVIDERS
    assert ni.CUSTOM_ID_FIELD["salesforce"] == "person_external_id_field" and ni.ORG_TARGETS["salesforce"] == "account_external_id_field"
    assert dict(next(f for f in ni.FIELDS["salesforce"] if f.name == "person_match").choices).keys() == {"custom_id"}


def test_credentials_are_set_only():
    cfg = ni.config_of(SF, "salesforce")
    assert not any(s in repr(cfg) for s in SECRETS) and "client_id" not in cfg and "client_secret" not in cfg
    assert ni.secrets_set(SF, "salesforce") == {"client_id": True, "client_secret": True}
    posted = {f.name: cfg.get(f.name, "") for f in ni.FIELDS["salesforce"]}
    posted.update(client_id="", client_secret="")
    out, _ = ni.merge_credentials(SF, "salesforce", posted)
    assert out["native_client_id"] == CLIENT_ID and out["native_client_secret"] == CLIENT_SECRET  # blank keeps stored
    out, notes = ni.merge_credentials(SF, "pipedrive", {"region": "global", "api_token": ""})
    assert not any(s in out.values() for s in SECRETS) and notes


@pytest.mark.parametrize("creds", [SF, LEAD])
def test_valid_config(creds):
    assert ni.validate("salesforce", creds, **KW) == {}
    assert ni.check_send_policy("salesforce", creds, dest()) is None
    assert ni.base_url("salesforce", creds) == f"https://{HOST}"


@pytest.mark.parametrize(
    "key",
    ["native_processing_ack", "native_instance_host", "native_client_id", "native_client_secret", "native_person_match", "native_person_object",
     "native_participation_object", "native_participation_external_id_field", "native_person_lookup_relationship"],
)
def test_required_fields(key):
    creds = {k: v for k, v in SF.items() if k != key}
    assert f"native-{key[len('native_'):]}" in ni.validate("salesforce", creds, **KW)
    assert ni.check_send_policy("salesforce", creds, dest()) == "native_config_invalid"  # delivery gates on this before execute


@pytest.mark.parametrize("key", ["native_person_object", "native_participation_object", "native_participation_external_id_field", "native_person_lookup_relationship", "native_client_secret", "native_instance_host"])
def test_execute_refuses_incomplete_config_without_calls(key):
    outcome, fake = run({k: v for k, v in SF.items() if k != key})
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])


def test_custom_id_field_required_and_email_mode_not_offered():
    creds = {k: v for k, v in SF.items() if k != "native_person_external_id_field"}
    assert "native-person_external_id_field" in ni.validate("salesforce", creds, **KW)
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])
    email = {**SF, "native_person_match": "email", "native_email_merge_ack": "confirmed"}
    assert "native-person_match" in ni.validate("salesforce", email, **KW)
    outcome, fake = run(email)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_match_strategy_unconfirmed", [])


@pytest.mark.parametrize(
    "mapping",
    [
        "webinar_event_title=Event_Title",  # targets are custom fields
        "webinar_event_title=event_title__c,webinar_event_title=Other__c",  # duplicate source
        "webinar_event_title=Same__c,webinar_attendance=Same__c",  # duplicate target
        "webinar_email=Email__c",  # not a participation source
        "webinar_event_title=Webinar_Registration_Id__c",  # the external id field itself
        "webinar_event_title=Contact__r",  # the lookup relationship
        "profile.email=Email__c",
        "webinar_event_title=Event_Title__c;webinar_attendance=Attendance__c",
    ],
)
def test_field_map_validation(mapping):
    creds = {**SF, "native_participation_field_map": mapping}
    assert "native-participation_field_map" in ni.validate("salesforce", creds, **KW)
    outcome, fake = run(creds)
    assert outcome.kind == "dead" and fake.calls == []


def test_field_map_helper():
    assert sf.participation_field_map("") == {}
    assert sf.participation_field_map("webinar_event_title=Event_Title__c,webinar_delivery_id=Delivery_Id__c") == {"webinar_event_title": "Event_Title__c", "webinar_delivery_id": "Delivery_Id__c"}
    assert sf.participation_field_map("webinar_nope=X__c") is None


def test_lead_mode_cannot_link_accounts():
    creds = {**LEAD, "native_account_external_id_field": "Webinar_Org_Id__c", "native_org_namespace": "crm"}
    assert "native-account_external_id_field" in ni.validate("salesforce", creds, **KW)


def test_profile_group_required():
    errors = ni.validate("salesforce", SF, **{**KW, "allowed_groups": ["identity", "registration", "event"]})
    assert any("profile" in m for m in errors["allowed_groups"])


def test_org_namespace_and_account_field_together():
    assert "native-org_namespace" in ni.validate("salesforce", {**SF, "native_org_namespace": ""}, **KW)
    assert "native-org_namespace" in ni.validate("salesforce", {**SF, "native_account_external_id_field": ""}, **KW)


def test_request_transport_gate(monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    assert any("transport" in m for m in ni.validate("salesforce", SF, **KW)["provider"])
    assert ni.check_send_policy("salesforce", SF, dest()) == "native_transport_unavailable"


# --- host policy (fail closed) ---------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["https://acme.my.salesforce.com", "acme.my.salesforce.com:443", "acme.my.salesforce.com/services", "user@acme.my.salesforce.com",
     "*.my.salesforce.com", "Acme.My.Salesforce.com", "acme.my.salesforce.com.", "10.0.0.1", "localhost", "acme", ""],
)
def test_bad_host_formats_rejected(host, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = [host or "x.example.com", HOST]
    creds = {**SF, "native_instance_host": host}
    assert ni.validate("salesforce", creds, **KW)
    assert ni.base_url("salesforce", creds) is None and ni.allowed_hosts("salesforce", creds) == frozenset()
    outcome, fake = run(creds)
    assert (outcome.kind, fake.calls) == ("dead", [])


@pytest.mark.parametrize(
    "entries,host",
    [
        (["salesforce.com"], HOST),  # parent domain does not admit sub-domains
        (["my.salesforce.com"], HOST),
        (["*.my.salesforce.com"], HOST),  # wildcard does not either
        ([HOST], HOST + ".evil.net"),  # suffix trick
        ([HOST], "evil" + HOST),
        (["hooks.example.net"], HOST),
    ],
)
def test_host_needs_exact_operator_entry(entries, host, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = entries
    creds = {**SF, "native_instance_host": host}
    errors = ni.validate("salesforce", creds, **KW)
    assert any("exactly" in m for m in errors.get("provider", []))
    assert ni.check_send_policy("salesforce", creds, dest()) == "native_config_invalid"
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])


def test_other_vendor_host_refused(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.hubapi.com", "api.pipedrive.com"]
    for host in ("api.hubapi.com", "api.pipedrive.com"):
        errors = ni.validate("salesforce", {**SF, "native_instance_host": host}, **KW)
        assert "native-instance_host" in errors
        assert ni.allowed_hosts("salesforce", {**SF, "native_instance_host": host}) == frozenset()


def test_sandbox_host_is_just_another_host(settings):
    sandbox = "acme--dev.sandbox.my.salesforce.com"
    settings.WEBHOOK_HOST_ALLOWLIST = [sandbox]
    creds = {**SF, "native_instance_host": sandbox}
    assert ni.validate("salesforce", creds, **KW) == {}
    assert ni.allowed_hosts("salesforce", creds) == frozenset({sandbox})


def test_host_removed_from_allowlist_after_enable_stops_send(settings):
    assert ni.check_send_policy("salesforce", SF, dest()) is None
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    assert ni.check_send_policy("salesforce", SF, dest()) == "native_config_invalid"
    assert run()[1].calls == []


def test_allowed_hosts_pins_exact_host():
    assert ni.allowed_hosts("salesforce", SF) == frozenset({HOST})
    assert ni.allowed_hosts("salesforce", {**SF, "native_region": "global"}) == frozenset()


def test_unsendable_without_delivery_host_pinning(monkeypatch):
    monkeypatch.delattr(webinars.delivery, "NATIVE_DESTINATION_HOSTS", raising=False)
    assert any("per-destination host pinning" in m for m in ni.validate("salesforce", SF, **KW)["provider"])
    outcome, fake = run()
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_transport_unavailable", [])


# --- request contract ---------------------------------------------------------


def test_exact_requests_contact_mode():
    outcome, fake = run()
    assert outcome.kind == "delivered" and len(fake.calls) == 2
    token, composite = fake.calls
    assert token["method"] == "POST" and token["url"] == TOKEN_URL
    assert token["headers"]["Content-Type"] == "application/x-www-form-urlencoded" and "Authorization" not in token["headers"]
    assert token["json"] == {"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    assert token["raw"] == b"grant_type=client_credentials&client_id=" + CLIENT_ID.encode() + b"&client_secret=" + CLIENT_SECRET.encode()
    assert composite["method"] == "POST" and composite["url"] == COMPOSITE_URL
    assert composite["headers"]["Authorization"] == f"Bearer {TOKEN}" and composite["headers"]["Content-Type"].startswith("application/json")
    assert composite["json"]["allOrNone"] is True and composite["json"]["collateSubrequests"] is False
    account, person, participation = composite["json"]["compositeRequest"]
    assert account == {
        "method": "PATCH",
        "url": f"/services/data/{sf.API_VERSION}/sobjects/Account/Webinar_Org_Id__c/acme-1",
        "referenceId": "account",
        "body": {"Webinar_Org_Id__c": "acme-1", "Name": "Example Co"},
    }
    assert person == {
        "method": "PATCH",
        "url": f"/services/data/{sf.API_VERSION}/sobjects/Contact/Webinar_Person_Id__c/{PID}",
        "referenceId": "person",
        "body": {"Webinar_Person_Id__c": PID, "Email": "person@example.com", "FirstName": "Sample", "LastName": "Person", "Title": "Role", "Account": {"Webinar_Org_Id__c": "acme-1"}},
    }
    assert participation == {
        "method": "PATCH",
        "url": f"/services/data/{sf.API_VERSION}/sobjects/Webinar_Participation__c/Webinar_Registration_Id__c/{RID}",
        "referenceId": "participation",
        "body": {
            "Webinar_Registration_Id__c": RID,
            "Event_Title__c": "Sample webinar",
            "Attendance__c": "unknown",
            "Starts_At__c": "2026-01-02T12:00:00Z",
            "Contact__r": {"Webinar_Person_Id__c": PID},
        },
    }
    assert all(r["method"] == "PATCH" and "/sobjects/" in r["url"] for r in composite["json"]["compositeRequest"])  # never a plain create


def test_lead_mode_requests():
    outcome, fake = run(LEAD, responses=(TOKEN_OK, composite_ok("person", "participation")))
    assert outcome.kind == "delivered"
    subs = subrequests(fake)
    assert set(subs) == {"person", "participation"}
    assert subs["person"]["url"].endswith(f"/sobjects/Lead/Webinar_Person_Id__c/{PID}")
    assert subs["person"]["body"]["Company"] == "Example Co" and subs["person"]["body"]["LastName"] == "Person"
    assert "Account" not in subs["person"]["body"]
    assert subs["participation"]["body"]["Lead__r"] == {"Webinar_Person_Id__c": PID}


@pytest.mark.parametrize(
    "org_ids",
    [
        ({"namespace": "crm", "value": "acme-1", "verified": False, "source": "browser"},),
        ({"namespace": "crm", "value": "acme-1", "verified": True, "source": "trusted_api"}, {"namespace": "crm", "value": "acme-2", "verified": True, "source": "trusted_api"}),
        (),
        ({"namespace": "other", "value": "acme-1", "verified": True, "source": "trusted_api"},),
    ],
)
def test_no_account_without_single_verified_org(org_ids):
    outcome, fake = run(SF, body(org_ids), responses=(TOKEN_OK, composite_ok("person", "participation")))
    assert outcome.kind == "delivered"
    subs = subrequests(fake)
    assert "account" not in subs and "Account" not in subs["person"]["body"]
    assert "Example Co" not in json.dumps(subs["person"]) and "Example Co" not in json.dumps(subs["participation"])  # never a match key


def test_account_name_falls_back_to_org_id():
    b = body()
    b["profile"].pop("company")
    _, fake = run(SF, b)
    assert subrequests(fake)["account"]["body"] == {"Webinar_Org_Id__c": "acme-1", "Name": "acme-1"}


def test_empty_mapping_writes_only_ids():
    _, fake = run({**SF, "native_participation_field_map": ""})
    assert subrequests(fake)["participation"]["body"] == {"Webinar_Registration_Id__c": RID, "Contact__r": {"Webinar_Person_Id__c": PID}}


@pytest.mark.parametrize("event_type,attendance", [("registration.updated", "unknown"), ("attendance.checked_in", "unknown"), ("attendance.recorded", "attended"), ("attendance.recorded", "no_show")])
def test_event_types_map_to_properties(event_type, attendance):
    b = body()
    b["registration"]["attendance"] = attendance
    creds = {**SF, "native_participation_field_map": "webinar_event_type=Last_Event__c,webinar_attendance=Attendance__c"}
    outcome, fake = run(creds, b, event_type)
    assert outcome.kind == "delivered"
    assert subrequests(fake)["participation"]["body"]["Last_Event__c"] == event_type
    assert subrequests(fake)["participation"]["body"]["Attendance__c"] == attendance


def test_unknown_attendance_skipped_before_any_call():
    b = body()
    b["registration"]["attendance"] = "weird"
    outcome, fake = run(SF, b, "attendance.recorded")
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_attendance_unknown", [])


def test_no_urls_or_custom_fields_ever_sent():
    b = body()
    b["event"]["occurrence"].update({"joinUrl": "https://zoom.example/j/SECRET1", "waitingRoomUrl": "https://zoom.example/w/SECRET2"})
    b["profile"]["customFields"] = {"secret_answer": "SECRET3"}
    b["profile"]["phone"] = "+15550100"
    for call in run(SF, b)[1].calls:
        assert "SECRET" not in json.dumps(call["json"]) and "+15550100" not in json.dumps(call["json"]) and "https://" not in json.dumps(call["json"])


# --- identity / required fields / consent ------------------------------------------


def test_unverified_identity_never_sent():
    for mutate in (lambda b: b["identity"].update(verified=False), lambda b: b["registration"].update(identityVerified=False)):
        b = body()
        mutate(b)
        outcome, fake = run(SF, b)
        assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_identity_unverified", [])


@pytest.mark.parametrize(
    "creds,mutate,reason",
    [
        (SF, lambda b: b["profile"].pop("lastName"), "native_contact_fields_missing"),
        (SF, lambda b: b.pop("profile"), "native_contact_fields_missing"),
        (LEAD, lambda b: b["profile"].pop("company"), "native_lead_fields_missing"),
        (LEAD, lambda b: b["profile"].pop("lastName"), "native_lead_fields_missing"),
    ],
)
def test_required_person_fields_skip_without_placeholders(creds, mutate, reason):
    b = body()
    mutate(b)
    outcome, fake = run(creds, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", reason, [])


def test_nested_mapped_value_refused_before_any_call():
    b = body()
    b["event"]["title"] = {"nested": "x"}
    outcome, fake = run(SF, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "participation: payload_unsupported", [])


def test_path_values_refused_before_any_call():
    outcome, fake = run(SF, body(({"namespace": "crm", "value": "acme 1", "verified": True, "source": "trusted_api"},)))
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "account: payload_unsupported", [])
    _, fake = run(SF, body(({"namespace": "crm", "value": "acme/1?x=1", "verified": True, "source": "trusted_api"},)))
    assert subrequests(fake)["account"]["url"].endswith("/Webinar_Org_Id__c/acme%2F1%3Fx%3D1")


def test_composite_size_limit_fails_before_token_call():
    b = body()
    b["event"]["title"] = "x" * sf.MAX_COMPOSITE_BYTES
    outcome, fake = run(SF, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "composite: payload_too_large", [])


def test_consent_rechecked_before_composite_call():
    fake = ReadFake([TOKEN_OK, COMPOSITE_OK])
    stop = ni.Outcome("cancelled", "consent_not_granted_mid_sequence")
    ctx = ni.NativeContext(post=fake.post, request=fake.request, renew=fake.renew, check=lambda: stop)
    outcome = ni.execute("salesforce", SF, body(), "registration.created", ctx)
    assert outcome == stop and [c["url"] for c in fake.calls] == [TOKEN_URL]


def test_two_calls_per_message_within_budget():
    fake = ReadFake([TOKEN_OK, COMPOSITE_OK])
    ctx = fake.ctx()
    assert ni.execute("salesforce", SF, body(), "registration.created", ctx).kind == "delivered" and ctx.calls == 2 <= ni.MAX_CALLS


# --- token handling -------------------------------------------------------------


def test_token_instance_mismatch_is_dead_and_not_cached():
    other = (200, {"access_token": TOKEN, "instance_url": "https://other.my.salesforce.com"})
    outcome, fake = run(SF, responses=(other, COMPOSITE_OK))
    assert (outcome.kind, outcome.error, len(fake.calls)) == ("dead", "token: instance_mismatch", 1)
    assert TOKEN not in outcome.error
    outcome, fake = run()  # a fresh token request, nothing cached from the refused one
    assert outcome.kind == "delivered" and fake.calls[0]["url"] == TOKEN_URL


@pytest.mark.parametrize("instance", ["http://acme.my.salesforce.com", "https://acme.my.salesforce.com:8443", "https://evil.net/acme.my.salesforce.com", "", None, 5])
def test_token_instance_must_be_the_configured_https_host(instance):
    outcome, fake = run(SF, responses=((200, {"access_token": TOKEN, "instance_url": instance}), COMPOSITE_OK))
    assert (outcome.kind, outcome.error, len(fake.calls)) == ("dead", "token: instance_mismatch", 1)


def test_token_missing_in_response_is_retried():
    outcome, fake = run(SF, responses=((200, {"instance_url": f"https://{HOST}"}), COMPOSITE_OK))
    assert (outcome.kind, outcome.error, len(fake.calls)) == ("retry", "token: response_missing_token", 1)


def test_token_cached_in_memory_and_reused():
    first, f1 = run()
    second, f2 = run(SF, responses=(COMPOSITE_OK,))
    assert first.kind == second.kind == "delivered"
    assert [c["url"] for c in f2.calls] == [COMPOSITE_URL] and f2.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    # Other credentials or another host never share the cached token.
    third, f3 = run({**SF, "native_client_secret": "AnotherSecretValue000000"}, responses=(TOKEN_OK, COMPOSITE_OK))
    assert third.kind == "delivered" and f3.calls[0]["url"] == TOKEN_URL


def test_cached_token_rejected_is_forgotten_and_retried():
    assert run()[0].kind == "delivered"
    outcome, fake = run(SF, responses=((401, {}),))
    assert (outcome.kind, outcome.error, outcome.status, [c["url"] for c in fake.calls]) == ("retry", "composite: token_rejected", 401, [COMPOSITE_URL])
    outcome, fake = run()
    assert outcome.kind == "delivered" and [c["url"] for c in fake.calls] == [TOKEN_URL, COMPOSITE_URL]


def test_fresh_token_rejected_is_dead():
    outcome, fake = run(SF, responses=(TOKEN_OK, (401, {})))
    assert (outcome.kind, outcome.error) == ("dead", "composite: http_status: 401")


def test_preview_never_caches_or_reads_the_token_cache():
    assert ni.preview("salesforce", SF) and hosting_base.cached_token("salesforce", sf._cache_key(HOST, {"client_id": ni.REDACTED, "client_secret": ni.REDACTED})) is None
    assert run()[0].kind == "delivered"
    assert [c["step"] for c in ni.preview("salesforce", SF)] == ["token", "composite"]  # the real cached token is never used for previews


# --- responses / failures -------------------------------------------------------


@pytest.mark.parametrize(
    "result,kind,error",
    [
        ((429, {}, 42), "retry", "http_status: 429"),
        ((500, {}), "retry", "http_status: 500"),
        ((503, {}, 7), "retry", "http_status: 503"),
        ((302, {}), "retry", "redirect_not_followed: http 302"),
        ((400, {"error": "invalid_client", "error_description": CLIENT_SECRET}), "dead", "http_status: 400"),
        ((403, {}), "dead", "http_status: 403"),
        (TimeoutError(), "retry", "timeout"),
        (ConnectionResetError(CLIENT_SECRET), "retry", "transport_error: ConnectionResetError"),
        (ValueError(f"echo {TOKEN} person@example.com"), "retry", "transport_error: ValueError"),
    ],
)
@pytest.mark.parametrize("step", ["token", "composite"])
def test_failures_stop_and_never_echo_secrets(step, result, kind, error):
    if isinstance(result, tuple) and len(result) > 2:
        result = (result[0], result[1], result[2])
    fake = ReadFake([result] if step == "token" else [TOKEN_OK, result])

    def request(method, url, data, headers):
        r = fake.responses.pop(0)
        fake.calls.append(url)
        if isinstance(r, Exception):
            raise r
        return (r[0], json.dumps(r[1]).encode()) + tuple(r[2:])

    ctx = ni.NativeContext(post=None, request=request, renew=lambda: True)
    outcome = ni.execute("salesforce", SF, body(), "registration.created", ctx)
    assert (outcome.kind, outcome.error) == (kind, f"{step}: {error}")
    assert not any(s in outcome.error for s in SECRETS) and "@" not in outcome.error
    if isinstance(result, tuple) and len(result) > 2:
        assert outcome.retry_after == result[2]


def _composite(entries):
    return (200, {"compositeResponse": [{"referenceId": ref, "httpStatusCode": status, "body": b} for ref, status, b in entries]})


HALTED = [{"errorCode": "PROCESSING_HALTED", "message": "The transaction was rolled back since another operation in the same transaction failed."}]


def test_subrequest_failure_names_root_cause_step_without_vendor_text():
    dup = [{"errorCode": "DUPLICATES_DETECTED", "message": f"You're creating a duplicate record for {CLIENT_SECRET} person@example.com"}]
    outcome, fake = run(SF, responses=(TOKEN_OK, _composite([("account", 400, HALTED), ("person", 400, dup), ("participation", 400, HALTED)])))
    assert (outcome.kind, outcome.error, outcome.status) == ("dead", "person: http_status: 400", 400)
    assert "DUPLICATES" not in outcome.error and "@" not in outcome.error and CLIENT_SECRET not in outcome.error


@pytest.mark.parametrize("status,kind", [(503, "retry"), (429, "retry"), (500, "retry"), (300, "dead"), (404, "dead"), (401, "dead")])
def test_subrequest_status_mapping(status, kind):
    outcome, _ = run(SF, responses=(TOKEN_OK, _composite([("account", 400, HALTED), ("person", 400, HALTED), ("participation", status, [{"errorCode": "X"}])])))
    assert (outcome.kind, outcome.error) == (kind, f"participation: http_status: {status}")


@pytest.mark.parametrize("payload", [{}, {"compositeResponse": "x"}, {"compositeResponse": [{"referenceId": "person", "httpStatusCode": 201}]}, {"compositeResponse": [{"httpStatusCode": "201"}] * 3}])
def test_unparsable_composite_response_is_retried(payload):
    outcome, _ = run(SF, responses=(TOKEN_OK, (200, payload)))
    assert (outcome.kind, outcome.error) == ("retry", "composite: response_unparsable")


def test_update_status_200_also_delivered():
    outcome, _ = run(SF, responses=(TOKEN_OK, composite_ok("account", "person", "participation", status=200)))
    assert outcome.kind == "delivered"


def test_retry_after_partial_failure_targets_same_records():
    first, f1 = run(SF, responses=(TOKEN_OK, (503, {})))
    assert (first.kind, first.error) == ("retry", "composite: http_status: 503")
    second, f2 = run(SF, responses=(COMPOSITE_OK,))  # token still cached
    assert second.kind == "delivered"
    assert f1.calls[1]["json"] == f2.calls[0]["json"]  # identical external-id upserts, never a plain create
    assert [r["url"] for r in f2.calls[0]["json"]["compositeRequest"]] == [r["url"] for r in f1.calls[1]["json"]["compositeRequest"]]


# --- preview ------------------------------------------------------------------


def test_preview_redacts_secrets_and_token():
    calls = ni.preview("salesforce", SF)
    assert [c["url"] for c in calls] == [TOKEN_URL, COMPOSITE_URL]
    assert not any(s in repr(calls) for s in SECRETS)
    assert calls[0]["body"] == {"grant_type": "client_credentials", "client_id": ni.REDACTED, "client_secret": ni.REDACTED}
    assert calls[1]["headers"]["Authorization"] == ni.REDACTED
    assert [r["referenceId"] for r in calls[1]["body"]["compositeRequest"]] == ["account", "person", "participation"]
    assert ni.preview_refusal("salesforce", SF) is None


def test_preview_empty_without_allowlisted_host(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["salesforce.com"]
    assert ni.preview("salesforce", SF) == [] and ni.preview_refusal("salesforce", SF) is None


@pytest.mark.parametrize(
    "creds,code",
    [
        ({**SF, "native_participation_field_map": "webinar_event_title=Same__c,webinar_attendance=Same__c"}, "native_config_invalid"),
        ({**SF, "native_person_match": ""}, "native_match_strategy_unconfirmed"),
        ({**SF, "native_person_external_id_field": ""}, "native_config_incomplete"),
    ],
)
def test_refused_preview_reports_bounded_code(creds, code):
    assert ni.preview("salesforce", creds) == []
    refusal = ni.preview_refusal("salesforce", creds)
    assert refusal == (code, ni.PREVIEW_REFUSALS[code]) and not any(s in repr(refusal) for s in SECRETS)


# --- delivery (host pinning, config change, never another host) -----------------


class RequestTransport:
    """Stands in for ``ssrf.request_json_bounded``: answers by URL and records the pinned host set."""

    def __init__(self, on_first=None, composite_status=None):
        self.calls, self.on_first, self.composite_status = [], on_first, composite_status

    def __call__(self, method, url, data, headers, timeout, total, max_bytes, allowed_hosts=None):
        self.calls.append({"method": method, "url": url, "data": data, "headers": dict(headers), "allowed": allowed_hosts})
        if len(self.calls) == 1 and self.on_first:
            self.on_first()
        if url.endswith("/services/oauth2/token"):
            return 200, json.dumps(TOKEN_OK[1]).encode(), None, False
        if self.composite_status:
            return self.composite_status, b"{}", None, False
        refs = [r["referenceId"] for r in json.loads(data)["compositeRequest"]]
        return 200, json.dumps(composite_ok(*refs)[1]).encode(), None, False


def claimed(occurrence, creds, **dest_kw):
    from webinars.outbox import enqueue_event

    make_destination(credentials_encrypted=encrypt_secret(json.dumps(creds)), **dest_kw)
    person = make_person(email=f"p{uuid.uuid4().hex[:8]}@example.com", first_name="Pat", last_name="Lovelace", company="Example Co", job_title="Role")
    reg = make_registration(occurrence, person=person)
    with transaction.atomic():
        assert len(enqueue_event("registration.created", registration=reg, occurrence=occurrence)) == 1
    [msg] = webinars.delivery.claim_batch("w1")
    return msg, reg


def _deliver(msg):
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    return msg


def test_e2e_delivered_to_pinned_host(occurrence, monkeypatch, caplog):
    fake = RequestTransport()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg, reg = claimed(occurrence, SF)
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED and msg.attempts == 1 and msg.last_error == ""
    assert [c["url"] for c in fake.calls] == [TOKEN_URL, COMPOSITE_URL]
    assert all(c["allowed"] == frozenset({HOST}) for c in fake.calls)
    assert fake.calls[0]["headers"]["Content-Type"] == "application/x-www-form-urlencoded" and b"client_secret=" + CLIENT_SECRET.encode() in fake.calls[0]["data"]
    assert fake.calls[1]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    sent = json.loads(fake.calls[1]["data"])
    person = next(r for r in sent["compositeRequest"] if r["referenceId"] == "person")
    assert person["body"]["LastName"] == "Lovelace" and person["body"]["Email"] == reg.person.email
    assert not any(s in caplog.text for s in SECRETS)


@pytest.mark.parametrize("status,expected", [(503, S.PENDING), (400, S.DEAD)])
def test_e2e_failures_keep_secrets_out_of_last_error(occurrence, monkeypatch, status, expected, caplog):
    fake = RequestTransport(composite_status=status)
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg, _ = claimed(occurrence, SF)
    msg = _deliver(msg)
    assert msg.status == expected and msg.last_error == f"composite: http_status: {status}" and msg.last_status_code == status
    assert not any(s in msg.last_error or s in caplog.text for s in SECRETS)


@pytest.mark.parametrize("target", ["https://api.hubapi.com", "https://hooks.example.net", "https://other.my.salesforce.com"])
def test_e2e_credentials_never_sent_to_another_host(target, occurrence, monkeypatch, settings, caplog):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", HOST, "api.hubapi.com", "other.my.salesforce.com"]
    msg, _ = claimed(occurrence, SF)
    monkeypatch.setattr(ni, "base_url", lambda provider, credentials: target)
    msg = _deliver(msg)  # the real transport refuses before any connection (no_network would fail otherwise)
    # Another vendor's static host is refused by the adapter itself; anything else by the pinned transport.
    expected = "native_config_incomplete" if target == "https://api.hubapi.com" else "token: unsafe_destination"
    assert msg.status == S.DEAD and msg.last_error == expected
    assert not any(s in msg.last_error or s in caplog.text for s in SECRETS)


@pytest.mark.parametrize(
    "mutate",
    [
        dict(credentials_encrypted=encrypt_secret(json.dumps({**SF, "native_participation_field_map": ""}))),
        dict(allowed_groups=["identity", "profile", "registration", "event"]),
        dict(include_private_links=True),
    ],
    ids=["credentials", "allowed_groups", "private_links"],
)
def test_e2e_config_change_after_token_stops_before_composite(occurrence, monkeypatch, mutate):
    msg, _ = claimed(occurrence, SF)
    fake = RequestTransport(on_first=lambda: Destination.objects.filter(pk=msg.destination_id).update(**mutate))
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg = _deliver(msg)
    assert [c["url"] for c in fake.calls] == [TOKEN_URL]  # no stale composite write
    assert msg.status == S.DEAD and msg.last_error == "native_config_changed_mid_sequence"


def test_e2e_without_request_transport_is_dead(occurrence, monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    msg, _ = claimed(occurrence, SF)
    msg = _deliver(msg)
    assert msg.status == S.DEAD and msg.last_error == "native_transport_unavailable"


def test_e2e_test_send_unsupported(occurrence, monkeypatch):
    fake = RequestTransport()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg, _ = claimed(occurrence, SF)
    OutboxMessage.objects.filter(pk=msg.pk).update(event_type=webinars.delivery.NATIVE_TEST_EVENT_TYPE)
    msg.refresh_from_db()
    msg = _deliver(msg)
    assert msg.status == S.CANCELLED and msg.last_error == "native_test_send_unsupported" and fake.calls == []
