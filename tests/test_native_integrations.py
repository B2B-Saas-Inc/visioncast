"""Native adapters (webinars/native_integrations.py): mocked HTTP contracts, policy and delivery.

No network: every transport is a fake. These tests pin our request shapes to the vendor
contracts we implemented; they are not a substitute for owner-approved sandbox checks.
"""
import base64
import copy
import json

import pytest
from django.db import transaction
from django.utils import timezone

import webinars.delivery
import webinars.ssrf
from tests.conftest import make_destination, make_registration
from webinars import native_integrations as ni
from webinars.crypto import encrypt_secret
from webinars.models import ConsentRecord, Destination, OutboxMessage
from webinars.ssrf import UnsafeDestination

pytestmark = pytest.mark.django_db
S = OutboxMessage.Status
VENDOR_HOSTS = ["hooks.example.net", "us.i.posthog.com", "eu.i.posthog.com", "track.customer.io", "track-eu.customer.io", "api.hubapi.com", "api.attio.com"]
ORG = {"namespace": "crm", "value": "acme-1", "verified": True, "source": "trusted_api"}

ACK = {"native_processing_ack": "confirmed"}
POSTHOG = {"native_provider": "posthog", "native_region": "us", "native_api_key": "phc_supersecret_value", "native_group_type": "company", "native_org_namespace": "crm", **ACK}
CUSTOMERIO = {"native_provider": "customerio", "native_region": "eu", "native_site_id": "site123", "native_api_key": "cio-key-456", "native_object_type_id": "1", "native_org_namespace": "crm", **ACK}
HUBSPOT = {
    **ACK, "native_person_match": "custom_id",
    "native_provider": "hubspot", "native_region": "global", "native_access_token": "hs-token-789",
    "native_contact_id_property": "webinar_person_id", "native_company_id_property": "webinar_org_id",
    "native_participation_object_type": "2-123", "native_participation_id_property": "webinar_registration_id",
    "native_org_namespace": "crm",
}
ATTIO = {
    **ACK, "native_person_match": "custom_id",
    "native_provider": "attio", "native_region": "global", "native_access_token": "attio-tok-000",
    "native_person_match_attribute": "webinar_person_id", "native_company_match_attribute": "webinar_org_id",
    "native_participation_list": "webinar_participation", "native_org_namespace": "crm",
}


@pytest.fixture(autouse=True)
def vendor_allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = list(VENDOR_HOSTS)


def body(org_ids=(ORG,), **overrides):
    b = copy.deepcopy(ni.SAMPLE_BODY)
    b["organization"] = {"externalIds": [dict(o) for o in org_ids]}
    b.update(overrides)
    return b


class Fake:
    """post -> (status, snippet); request -> (status, raw). ``responses`` is consumed in order."""

    def __init__(self, responses=(), renew=True):
        self.responses = list(responses)
        self.calls = []
        self.renew_ok = renew
        self.renews = 0

    def _next(self):
        r = self.responses.pop(0) if self.responses else 200
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, url, data, headers):
        self.calls.append({"method": "POST", "url": url, "json": json.loads(data), "headers": dict(headers)})
        return self._next(), ""

    def request(self, method, url, data, headers):
        self.calls.append({"method": method, "url": url, "json": json.loads(data), "headers": dict(headers)})
        r = self._next()
        status, payload = r if isinstance(r, tuple) else (r, {})
        return status, json.dumps(payload).encode()

    def renew(self):
        self.renews += 1
        return self.renew_ok

    def ctx(self):
        return ni.NativeContext(post=self.post, request=self.request, renew=self.renew)


def run(creds, b=None, event_type="registration.created", fake=None):
    fake = fake or Fake()
    return ni.execute(creds["native_provider"], creds, b if b is not None else body(), event_type, fake.ctx()), fake


# --- 1. contracts -----------------------------------------------------------


def test_posthog_single_batch_identify_group_capture():
    out, fake = run(POSTHOG)
    assert out.kind == "delivered"
    [call] = fake.calls
    assert call["url"] == "https://us.i.posthog.com/batch/" and call["method"] == "POST"
    assert "Authorization" not in call["headers"]
    assert call["json"]["api_key"] == "phc_supersecret_value"
    identify, group, capture = call["json"]["batch"]
    assert identify["event"] == "$identify" and identify["distinct_id"] == ni.SAMPLE_BODY["identity"]["personId"]
    assert identify["properties"]["$set"]["email"] == "person@example.com"
    assert group["event"] == "$groupidentify"
    assert group["properties"]["$group_type"] == "company" and group["properties"]["$group_key"] == "acme-1"
    assert capture["event"] == "webinar registered"
    assert capture["properties"]["$groups"] == {"company": "acme-1"}
    assert capture["properties"]["webinar_registration_id"] == ni.SAMPLE_BODY["registration"]["id"]
    _, again = run(POSTHOG)
    assert [e["uuid"] for e in again.calls[0]["json"]["batch"]] == [e["uuid"] for e in call["json"]["batch"]]


@pytest.mark.parametrize(
    "org_ids",
    [
        ({**ORG, "verified": False, "source": "browser"},),  # unverified
        (ORG, {**ORG, "value": "acme-2"}),  # ambiguous
        (),  # company name only (profile.company)
        ({**ORG, "namespace": "other"},),
    ],
)
def test_posthog_no_group_without_single_verified_org(org_ids):
    out, fake = run(POSTHOG, body(org_ids))
    assert out.kind == "delivered"
    events = fake.calls[0]["json"]["batch"]
    assert [e["event"] for e in events] == ["$identify", "webinar registered"]
    assert "$groups" not in events[-1]["properties"]


def test_posthog_eu_region():
    _, fake = run({**POSTHOG, "native_region": "eu"})
    assert fake.calls[0]["url"] == "https://eu.i.posthog.com/batch/"


def test_customerio_three_entity_posts():
    out, fake = run(CUSTOMERIO)
    assert out.kind == "delivered"
    assert [c["url"] for c in fake.calls] == ["https://track-eu.customer.io/api/v2/entity"] * 3
    auth = "Basic " + base64.b64encode(b"site123:cio-key-456").decode()
    assert all(c["headers"]["Authorization"] == auth for c in fake.calls)
    org, person, event = (c["json"] for c in fake.calls)
    assert org["type"] == "object" and org["identifiers"] == {"object_type_id": "1", "object_id": "acme-1"}
    assert person["action"] == "identify" and person["identifiers"] == {"id": ni.SAMPLE_BODY["identity"]["personId"]}
    assert person["cio_relationships"] == [{"identifiers": {"object_type_id": "1", "object_id": "acme-1"}}]
    assert event["action"] == "event" and event["name"] == "webinar registered"
    _, us = run({**CUSTOMERIO, "native_region": "us"})
    assert us.calls[0]["url"] == "https://track.customer.io/api/v2/entity"


HS_OK = [(200, {"results": [{"id": "101"}]}), (200, {"results": [{"id": "201"}]}), (200, {"results": [{"id": "301"}]}), (200, {}), (200, {})]


def test_hubspot_upserts_and_associations():
    out, fake = run(HUBSPOT, fake=Fake(HS_OK))
    assert out.kind == "delivered"
    base = "https://api.hubapi.com"
    assert [c["url"] for c in fake.calls] == [
        base + "/crm/v3/objects/contacts/batch/upsert",
        base + "/crm/v3/objects/companies/batch/upsert",
        base + "/crm/v3/objects/2-123/batch/upsert",
        base + "/crm/v4/associations/contacts/companies/batch/associate/default",
        base + "/crm/v4/associations/contacts/2-123/batch/associate/default",
    ]
    assert all(c["method"] == "POST" and c["headers"]["Authorization"] == "Bearer hs-token-789" for c in fake.calls)
    contact = fake.calls[0]["json"]["inputs"][0]
    assert contact["idProperty"] == "webinar_person_id" and contact["id"] == ni.SAMPLE_BODY["identity"]["personId"]
    assert fake.calls[1]["json"]["inputs"][0]["id"] == "acme-1"
    assert fake.calls[2]["json"]["inputs"][0]["id"] == ni.SAMPLE_BODY["registration"]["id"]
    assert fake.calls[3]["json"]["inputs"] == [{"from": {"id": "101"}, "to": {"id": "201"}}]
    assert fake.calls[4]["json"]["inputs"] == [{"from": {"id": "101"}, "to": {"id": "301"}}]
    assert fake.renews == 4  # lease renewed before every call after the first


def test_hubspot_missing_id_retries():
    out, fake = run(HUBSPOT, fake=Fake([(200, {"results": []})]))
    assert (out.kind, out.error) == ("retry", "contact: response_missing_id")
    assert len(fake.calls) == 1


def test_attio_assert_records_and_list_entry():
    fake = Fake([(200, {"data": {"id": {"record_id": "c-1"}}}), (200, {"data": {"id": {"record_id": "p-1"}}}), (200, {})])
    out, fake = run(ATTIO, fake=fake)
    assert out.kind == "delivered"
    assert [c["method"] for c in fake.calls] == ["PUT", "PUT", "PUT"]
    assert fake.calls[0]["url"] == "https://api.attio.com/v2/objects/companies/records?matching_attribute=webinar_org_id"
    assert fake.calls[1]["url"] == "https://api.attio.com/v2/objects/people/records?matching_attribute=webinar_person_id"
    values = fake.calls[1]["json"]["data"]["values"]
    assert values["company"] == [{"target_object": "companies", "target_record_id": "c-1"}]
    assert values["webinar_person_id"] == ni.SAMPLE_BODY["identity"]["personId"]
    assert fake.calls[2]["url"] == "https://api.attio.com/v2/lists/webinar_participation/entries"
    entry = fake.calls[2]["json"]["data"]
    assert entry["parent_record_id"] == "p-1" and entry["parent_object"] == "people"


# --- 2. identity skips ----------------------------------------------------------


def test_unverified_no_person_and_unknown_attendance_cancelled():
    unverified = body()
    unverified["identity"]["verified"] = False
    out, fake = run(POSTHOG, unverified)
    assert (out.kind, out.error, fake.calls) == ("cancelled", "native_identity_unverified", [])
    no_person = body()
    del no_person["identity"]
    assert run(POSTHOG, no_person)[0].error == "native_no_person"
    out, fake = run(POSTHOG, body(), event_type="attendance.recorded")
    assert (out.kind, out.error, fake.calls) == ("cancelled", "native_attendance_unknown", [])


# --- 3. status mapping --------------------------------------------------------


@pytest.mark.parametrize("status,kind", [(429, "retry"), (503, "retry"), (400, "dead"), (401, "dead"), (302, "retry")])
def test_status_mapping(status, kind):
    out, _ = run(POSTHOG, fake=Fake([status]))
    assert out.kind == kind and out.status == status and "supersecret" not in out.error


@pytest.mark.parametrize(
    "exc,kind,error",
    [
        (OSError("boom person@example.com"), "retry", "batch: transport_error: OSError"),
        (UnsafeDestination("x"), "dead", "batch: unsafe_destination"),
        (TimeoutError(), "retry", "batch: timeout"),
    ],
)
def test_transport_exceptions(exc, kind, error):
    out, _ = run(POSTHOG, fake=Fake([exc]))
    assert (out.kind, out.error) == (kind, error)


def test_transport_unavailable_is_dead():
    out, _ = run(ATTIO, fake=Fake([ni.TransportUnavailable()]))
    assert (out.kind, out.error) == ("dead", "native_transport_unavailable")


def test_max_calls_and_lost_lease():
    fake = Fake()
    ctx = fake.ctx()
    ctx.calls = ni.MAX_CALLS
    assert ni.execute("posthog", POSTHOG, body(), "registration.created", ctx).error == "native_max_calls_exceeded"
    assert fake.calls == []
    out, fake = run(CUSTOMERIO, fake=Fake(renew=False))
    assert out.kind == "lost_lease" and len(fake.calls) == 1


# --- 4. partial failure -------------------------------------------------------


def test_hubspot_partial_failure_retry_is_idempotent():
    first, f1 = run(HUBSPOT, fake=Fake(HS_OK[:2] + [503]))
    assert (first.kind, first.error) == ("retry", "participation: http_status: 503")
    second, f2 = run(HUBSPOT, fake=Fake(HS_OK))
    assert second.kind == "delivered"
    assert [c["json"] for c in f1.calls] == [c["json"] for c in f2.calls[:3]]


# --- 5. validation -------------------------------------------------------------

OK = dict(event_types=["registration.created"], allowed_groups=list(Destination.GROUPS), required_consents=["analytics"])


def test_validate_posthog(settings):
    assert ni.validate("posthog", POSTHOG, **OK) == {}
    assert "native-api_key" in ni.validate("posthog", {**POSTHOG, "native_api_key": ""}, **OK)
    assert "native-region" in ni.validate("posthog", {**POSTHOG, "native_region": "mars"}, **OK)
    assert "native-org_namespace" in ni.validate("posthog", {**POSTHOG, "native_org_namespace": ""}, **OK)
    assert "required_consents" in ni.validate("posthog", POSTHOG, **{**OK, "required_consents": []})
    assert "event_types" in ni.validate("posthog", POSTHOG, **{**OK, "event_types": ["occurrence.cancelled"]})
    assert "allowed_groups" in ni.validate("posthog", POSTHOG, **{**OK, "allowed_groups": ["event"]})
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    assert "provider" not in ni.validate("posthog", POSTHOG, **OK)  # pinned vendor host needs no operator entry
    settings.NATIVE_VENDOR_HOSTS_IMPLICIT = False
    assert "WEBHOOK_HOST_ALLOWLIST" in " ".join(ni.validate("posthog", POSTHOG, **OK)["provider"])
    assert ni.validate("", {}, **OK) == {}


def test_validate_request_transport_gate(monkeypatch):
    kw = {**OK, "required_consents": []}
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    assert "transport" in " ".join(ni.validate("hubspot", HUBSPOT, **kw)["provider"])
    assert "provider" in ni.validate("attio", ATTIO, **kw)
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", lambda *a, **k: None, raising=False)
    assert ni.validate("hubspot", HUBSPOT, **kw) == {}
    assert ni.validate("attio", ATTIO, **kw) == {}


# --- 6. merge -------------------------------------------------------------------


def test_merge_credentials():
    existing = {"native_provider": "posthog", "native_region": "us", "native_api_key": "old-secret", "x_custom": "keep"}
    out, notes = ni.merge_credentials(existing, "posthog", {"region": "eu", "api_key": ""})
    assert out["native_api_key"] == "old-secret" and out["native_region"] == "eu" and out["x_custom"] == "keep" and notes == []
    out, notes = ni.merge_credentials(existing, "customerio", {"region": "us", "site_id": "s1abcd", "api_key": ""})
    assert out["native_provider"] == "customerio" and "native_api_key" not in out and out["x_custom"] == "keep"
    assert notes and "PostHog" in notes[0]
    out, notes = ni.merge_credentials(existing, "", {})
    assert out == {"x_custom": "keep"} and notes


# --- 7. preview -------------------------------------------------------------------


@pytest.mark.parametrize("creds,secret", [(POSTHOG, "phc_supersecret_value"), (CUSTOMERIO, "cio-key-456"), (HUBSPOT, "hs-token-789")])
def test_preview_redacts(creds, secret):
    calls = ni.preview(creds["native_provider"], creds)
    assert calls
    rendered = json.dumps(calls)
    assert secret not in rendered and "site123" not in rendered
    for c in calls:
        assert c["headers"].get("Authorization", ni.REDACTED) == ni.REDACTED
    if creds is POSTHOG:
        assert calls[0]["body"]["api_key"] == ni.REDACTED


# --- 8. end to end through delivery.deliver ------------------------------------


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


def _claimed(occurrence, creds, *, consent=True, **dest_kw):
    from webinars.outbox import enqueue_event

    if creds is not None:
        dest_kw["credentials_encrypted"] = creds if isinstance(creds, str) else encrypt_secret(json.dumps(creds))
    make_destination(**dest_kw)
    reg = make_registration(occurrence)
    if consent:
        ConsentRecord.objects.create(person=reg.person, registration=reg, purpose="analytics", granted=True, source="trusted_api", server_verified=True)
    with transaction.atomic():
        assert len(enqueue_event("registration.created", registration=reg, occurrence=occurrence)) == 1
    [msg] = webinars.delivery.claim_batch("w1")
    return msg, reg


def _deliver(msg):
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    return msg


def test_e2e_posthog_delivered(occurrence, transport, caplog):
    msg, _ = _claimed(occurrence, POSTHOG, required_consents=["analytics"])
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED and msg.attempts == 1
    [call] = transport.calls
    assert call["url"] == "https://us.i.posthog.com/batch/"
    assert "Webinar-Signature" not in call["headers"]
    sent = json.loads(call["body"])
    assert sent["batch"][-1]["properties"]["webinar_delivery_id"] == str(msg.id)
    assert "supersecret" not in msg.last_error and "supersecret" not in caplog.text


def test_e2e_posthog_withdrawn_consent_cancelled(occurrence, transport):
    msg, reg = _claimed(occurrence, POSTHOG, required_consents=["analytics"])
    ConsentRecord.objects.filter(person=reg.person).update(withdrawn_at=timezone.now())
    msg = _deliver(msg)
    assert msg.status == S.CANCELLED and msg.last_error == "consent_not_granted" and transport.calls == []


@pytest.mark.parametrize("status,expected", [(503, S.PENDING), (400, S.DEAD)])
def test_e2e_posthog_failures(occurrence, transport, status, expected):
    transport.status = status
    msg, _ = _claimed(occurrence, POSTHOG, required_consents=["analytics"])
    msg = _deliver(msg)
    assert msg.status == expected and msg.attempts == 1 and msg.last_status_code == status
    assert msg.last_error == f"batch: http_status: {status}"


def test_e2e_hubspot_without_request_transport_dead(occurrence, transport, monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    msg, _ = _claimed(occurrence, HUBSPOT, consent=False)
    msg = _deliver(msg)
    assert msg.status == S.DEAD and msg.last_error == "native_transport_unavailable" and transport.calls == []


def test_e2e_generic_webhook_unchanged(occurrence, transport):
    msg, _ = _claimed(occurrence, {"api_token": "x"}, consent=False)
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED
    [call] = transport.calls
    assert call["url"] == "https://hooks.example.net/webhook" and "Webinar-Signature" in call["headers"]


def test_e2e_corrupt_credentials_fail_closed(occurrence, transport):
    msg, _ = _claimed(occurrence, "not-a-fernet-token", consent=False)
    msg = _deliver(msg)
    assert msg.status == S.PENDING and msg.last_error == "credentials_unavailable" and transport.calls == []
