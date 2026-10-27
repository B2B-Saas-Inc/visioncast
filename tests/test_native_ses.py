"""Amazon SES native adapter: SigV4 signer vectors, SendEmail contract, region hosts, redaction,
status mapping, retry semantics and end-to-end delivery.

No network: transports are fakes and socket connects raise. The request shape follows the SES
API v2 SendEmail / Message / MessageHeader references and the SES endpoints table fetched
2026-10-27; the signer is checked against the AWS SigV4 test-suite vectors published in
awslabs/aws-c-auth (``tests/aws-signing-test-suite/v4``). Nothing has been checked against a
real AWS account.
"""
import datetime
import hashlib
import json
import logging
import socket

import pytest

import webinars.delivery
import webinars.ssrf
from tests.conftest import JOIN_URL
from tests.test_native_integrations import Fake, _claimed
from tests.test_transactional_email import mail_body
from webinars import native_integrations as ni
from webinars.models import Destination, OutboxMessage
from webinars.native_providers import _transactional_email as tx
from webinars.native_providers import ses

pytestmark = pytest.mark.django_db

ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
REGION = "eu-west-2"
HOST = f"email.{REGION}.amazonaws.com"
SEND_URL = f"https://{HOST}/v2/email/outbound-emails"
SES = {
    "native_provider": "ses",
    "native_region": REGION,
    "native_access_key_id": ACCESS_KEY,
    "native_secret_access_key": SECRET,
    "native_from_address": "events@example.com",
    "native_from_name": "Example Events",
    "native_reply_to": "hello@example.com",
    "native_subject_prefix": "",
    "native_configuration_set": "webinars",
    "native_send_ack": "confirmed",
    "native_processing_ack": "confirmed",
}
KW = dict(event_types=list(tx.EVENT_TYPES), allowed_groups=list(Destination.GROUPS), required_consents=[])
S = OutboxMessage.Status
DELIVERY_ID = ni.SAMPLE_BODY["delivery_metadata"]["deliveryId"]
NOW = datetime.datetime(2026, 10, 27, 12, 0, 5, tzinfo=datetime.timezone.utc)

# awslabs/aws-c-auth tests/aws-signing-test-suite/v4 (fetched 2026-10-27)
SUITE_KEY, SUITE_SECRET = "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
SUITE_NOW = datetime.datetime(2015, 8, 30, 12, 36, tzinfo=datetime.timezone.utc)
SUITE_SCOPE = "20150830/us-east-1/service/aws4_request"
VECTORS = {
    "get-vanilla": dict(
        method="GET",
        headers={},
        payload=b"",
        canonical="GET\n/\n\nhost:example.amazonaws.com\nx-amz-date:20150830T123600Z\n\nhost;x-amz-date\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        hashed="bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63",
        signature="5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
        signed="host;x-amz-date",
    ),
    "post-vanilla": dict(
        method="POST",
        headers={},
        payload=b"",
        canonical="POST\n/\n\nhost:example.amazonaws.com\nx-amz-date:20150830T123600Z\n\nhost;x-amz-date\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        hashed="553f88c9e4d10fc9e109e2aeb65f030801b70c2f6468faca261d401ae622fc87",
        signature="5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b",
        signed="host;x-amz-date",
    ),
    "post-x-www-form-urlencoded": dict(
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "13", "x-amz-content-sha256": "9095672bbd1f56dfc5b65f3e153adc8731a4a654192329106275f4c7b24d0b6e"},
        payload=b"Param1=value1",
        canonical=(
            "POST\n/\n\ncontent-length:13\ncontent-type:application/x-www-form-urlencoded\nhost:example.amazonaws.com\n"
            "x-amz-content-sha256:9095672bbd1f56dfc5b65f3e153adc8731a4a654192329106275f4c7b24d0b6e\nx-amz-date:20150830T123600Z\n\n"
            "content-length;content-type;host;x-amz-content-sha256;x-amz-date\n9095672bbd1f56dfc5b65f3e153adc8731a4a654192329106275f4c7b24d0b6e"
        ),
        hashed="b1edd1d03544c25390e32085d55b57acc9a3961bb59415ff86c45c3d89d16cfb",
        signature="d3875051da38690788ef43de4db0d8f280229d82040bfac253562e56c3f20e0b",
        signed="content-length;content-type;host;x-amz-content-sha256;x-amz-date",
    ),
    "get-header-value-trim": dict(
        method="GET",
        headers={"My-Header1": " value1", "My-Header2": ' "a   b   c"'},
        payload=b"",
        canonical=(
            "GET\n/\n\nhost:example.amazonaws.com\nmy-header1:value1\nmy-header2:\"a b c\"\nx-amz-date:20150830T123600Z\n\n"
            "host;my-header1;my-header2;x-amz-date\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        ),
        hashed="a726db9b0df21c14f559d0a978e563112acb1b9e05476f0a6a1c7d68f28605c7",
        signature="acc3ed3afb60bb290fc8d2dd0098b9911fcaa05412b367055dee359757a9c736",
        signed="host;my-header1;my-header2;x-amz-date",
    ),
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", HOST, "email.us-east-1.amazonaws.com", "api.segment.io"]
    settings.PUBLIC_BASE_URL = "https://webinars.example.com"
    settings.PUBLIC_EVENT_URL_TEMPLATE = ""


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    monkeypatch.setattr(ses, "clock", lambda: NOW)


def run(creds=SES, b=None, event_type="registration.created", fake=None):
    fake = fake or Fake()
    return ni.execute("ses", creds, b if b is not None else mail_body(), event_type, fake.ctx()), fake


def dest(**kw):
    return Destination(**{**KW, **kw})


# --- SigV4 ------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_sigv4_matches_the_aws_test_suite(name):
    v = VECTORS[name]
    authorization, amz_date, canonical, string_to_sign = ses.sigv4(
        v["method"], "example.amazonaws.com", "/", "", v["headers"], v["payload"],
        access_key_id=SUITE_KEY, secret_access_key=SUITE_SECRET, region="us-east-1", service="service", now=SUITE_NOW,
    )
    assert amz_date == "20150830T123600Z"
    assert canonical == v["canonical"]
    assert string_to_sign == f"AWS4-HMAC-SHA256\n20150830T123600Z\n{SUITE_SCOPE}\n{v['hashed']}"
    assert authorization == f"AWS4-HMAC-SHA256 Credential={SUITE_KEY}/{SUITE_SCOPE}, SignedHeaders={v['signed']}, Signature={v['signature']}"


def test_sigv4_canonicalises_query_and_path():
    _auth, _d, canonical, _s = ses.sigv4("GET", "example.amazonaws.com", "/a b/c", "zeta=1&Alpha=a b", {}, b"", access_key_id=SUITE_KEY, secret_access_key=SUITE_SECRET, region="us-east-1", service="service", now=SUITE_NOW)
    assert canonical.split("\n")[1:3] == ["/a%20b/c", "Alpha=a%20b&zeta=1"]


def test_sigv4_signs_payload_bytes_and_changes_with_date_or_secret():
    args = dict(access_key_id=ACCESS_KEY, secret_access_key=SECRET, region=REGION, service="ses")
    a1 = ses.sigv4("POST", HOST, ses.SEND_PATH, "", {"host": HOST}, b'{"a":1}', now=NOW, **args)[0]
    a2 = ses.sigv4("POST", HOST, ses.SEND_PATH, "", {"host": HOST}, b'{"a":2}', now=NOW, **args)[0]
    a3 = ses.sigv4("POST", HOST, ses.SEND_PATH, "", {"host": HOST}, b'{"a":1}', now=NOW + datetime.timedelta(seconds=1), **args)[0]
    a4 = ses.sigv4("POST", HOST, ses.SEND_PATH, "", {"host": HOST}, b'{"a":1}', now=NOW, **{**args, "secret_access_key": SECRET + "x"})[0]
    assert len({a1, a2, a3, a4}) == 4 and SECRET not in a1 and f"Credential={ACCESS_KEY}/20261027/{REGION}/ses/aws4_request" in a1


# --- schema / credentials ---------------------------------------------------------


def test_offered_and_schema_driven():
    assert dict(ni.PROVIDER_CHOICES)["ses"] == "Amazon SES"
    region = ni.FIELDS["ses"][0]
    assert region.name == "region" and dict(region.choices)[REGION] == f"{REGION} ({HOST})"
    assert ni.PROCESSING_ACK in ni.FIELDS["ses"] and tx.SEND_ACK in ni.FIELDS["ses"]
    assert "ses" not in ni.ANALYTICS_PROVIDERS and "ses" in ni.NEEDS_REQUEST_TRANSPORT
    assert ni.supported_event_types("ses") == tx.EVENT_TYPES
    assert {f.name for f in ni.FIELDS["ses"] if f.secret} == {"access_key_id", "secret_access_key"}


def test_regions_are_the_documented_ses_endpoints():
    assert ni.HOSTS["ses"] == {r: f"email.{r}.amazonaws.com" for r in ses.REGIONS}
    for required in ("us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-west-2", "eu-central-1", "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ca-central-1", "sa-east-1"):
        assert required in ses.REGIONS
    assert not any(r.startswith("us-gov") for r in ses.REGIONS)
    assert set(ni.HOSTS["ses"].values()) <= ni.VENDOR_HOSTS
    assert ni.base_url("ses", SES) == f"https://{HOST}" and ni.allowed_hosts("ses", SES) == frozenset({HOST})
    other = {**SES, "native_region": "us-east-1"}
    assert ni.allowed_hosts("ses", other) == frozenset({"email.us-east-1.amazonaws.com"})


@pytest.mark.parametrize("region", ["eu-west-9", "EU-WEST-2", "", "email.eu-west-2.amazonaws.com"])
def test_unknown_region_has_no_host(region):
    creds = {**SES, "native_region": region}
    assert ni.validate("ses", creds, **KW)
    assert ni.base_url("ses", creds) is None and ni.allowed_hosts("ses", creds) == frozenset()
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])


def test_credentials_are_set_only():
    rendered = repr(ni.config_of(SES, "ses")) + repr(ni.secrets_set(SES, "ses"))
    assert SECRET not in rendered and ACCESS_KEY not in rendered
    assert ni.secrets_set(SES, "ses") == {"access_key_id": True, "secret_access_key": True}
    posted = {f.name: ni.config_of(SES, "ses").get(f.name, "") for f in ni.FIELDS["ses"]}
    out, _ = ni.merge_credentials(SES, "ses", posted)
    assert out["native_secret_access_key"] == SECRET and out["native_access_key_id"] == ACCESS_KEY


def test_valid_config():
    assert ni.validate("ses", SES, **KW) == {}
    assert ni.check_send_policy("ses", SES, dest()) is None
    minimal = {k: v for k, v in SES.items() if k not in ("native_from_name", "native_reply_to", "native_configuration_set")}
    assert ni.validate("ses", minimal, **KW) == {}


@pytest.mark.parametrize("key", ["native_processing_ack", "native_send_ack", "native_from_address", "native_access_key_id", "native_secret_access_key"])
def test_required_fields(key):
    creds = {k: v for k, v in SES.items() if k != key}
    assert f"native-{key[len('native_'):]}" in ni.validate("ses", creds, **KW)
    assert ni.check_send_policy("ses", creds, dest()) == "native_config_invalid"


@pytest.mark.parametrize("field,value", [("access_key_id", "akia-lower"), ("access_key_id", "SHORT"), ("configuration_set", "has space"), ("from_address", "Example <events@example.com>")])
def test_field_formats(field, value):
    assert f"native-{field}" in ni.validate("ses", {**SES, f"native_{field}": value}, **KW)


def test_host_must_be_operator_allowlisted(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    assert any(HOST in m for m in ni.validate("ses", SES, **KW)["provider"])
    assert ni.check_send_policy("ses", SES, dest()) == "native_config_invalid"


# --- request contract ---------------------------------------------------------------


def test_exact_request_and_signature():
    outcome, fake = run()
    assert outcome.kind == "delivered"
    [call] = fake.calls
    assert call["method"] == "POST" and call["url"] == SEND_URL
    payload = call["json"]
    assert set(payload) == {"FromEmailAddress", "Destination", "ReplyToAddresses", "Content", "ConfigurationSetName"}
    assert payload["FromEmailAddress"] == "Example Events <events@example.com>"
    assert payload["Destination"] == {"ToAddresses": ["person@example.com"]}
    assert payload["ReplyToAddresses"] == ["hello@example.com"] and payload["ConfigurationSetName"] == "webinars"
    simple = payload["Content"]["Simple"]
    assert set(simple) == {"Subject", "Body", "Headers"}
    assert simple["Subject"] == {"Data": "You're registered: Sample webinar", "Charset": "UTF-8"}
    assert simple["Body"]["Text"]["Charset"] == "UTF-8" and simple["Body"]["Text"]["Data"].startswith("Hi Sample,")
    assert simple["Body"]["Html"]["Data"].startswith("<!DOCTYPE html>")
    assert simple["Headers"] == [{"Name": "X-Webinar-Delivery-Id", "Value": DELIVERY_ID}, {"Name": "X-Webinar-Event-Type", "Value": "registration.created"}]
    assert call["headers"]["X-Amz-Date"] == "20261027T120005Z"
    authorization = call["headers"]["Authorization"]
    assert authorization.startswith(f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY}/20261027/{REGION}/ses/aws4_request, SignedHeaders=content-type;host;x-amz-date, Signature=")
    assert SECRET not in json.dumps(call) and "Idempotency-Key" not in call["headers"]
    # The signature covers the exact bytes NativeContext.call sends (compact sorted JSON).
    expected, _d, canonical, _s = ses.sigv4(
        "POST", HOST, ses.SEND_PATH, "", {"host": HOST, "content-type": call["headers"]["Content-Type"]}, ni._encode(payload),
        access_key_id=ACCESS_KEY, secret_access_key=SECRET, region=REGION, service="ses", now=NOW,
    )
    assert authorization == expected
    assert canonical.endswith(hashlib.sha256(ni._encode(payload)).hexdigest())


def test_optional_settings_omitted_when_blank():
    creds = {k: v for k, v in SES.items() if k not in ("native_from_name", "native_reply_to", "native_configuration_set")}
    [call] = run(creds)[1].calls
    assert call["json"]["FromEmailAddress"] == "events@example.com"
    assert "ReplyToAddresses" not in call["json"] and "ConfigurationSetName" not in call["json"]


def test_subject_prefix_and_overrides_apply():
    creds = {**SES, "native_subject_prefix": "[Example]", "native_body_reminder_due": "Starts {starts_at_local}."}
    [call] = run(creds, event_type="reminder.due")[1].calls
    assert call["json"]["Content"]["Simple"]["Subject"]["Data"] == "[Example] Reminder: Sample webinar starts Tue 3 Nov 2026, 14:00 GMT"
    assert call["json"]["Content"]["Simple"]["Body"]["Text"]["Data"] == "Starts Tue 3 Nov 2026, 14:00 GMT."


@pytest.mark.parametrize("event_type,attendance", [("occurrence.rescheduled", None), ("occurrence.cancelled", None), ("attendance.recorded", "attended"), ("attendance.recorded", "no_show"), ("follow_up.requested", None)])
def test_message_per_event_type(event_type, attendance):
    b = mail_body()
    if attendance:
        b["registration"]["attendance"] = attendance
    outcome, fake = run(SES, b, event_type)
    assert outcome.kind == "delivered"
    assert fake.calls[0]["json"]["Content"]["Simple"]["Headers"][1]["Value"] == event_type


@pytest.mark.parametrize("event_type", ["registration.updated", "attendance.checked_in"])
def test_unsupported_events_cancelled(event_type):
    outcome, fake = run(SES, mail_body(), event_type)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_event_unsupported", [])


def test_managed_room_link_wins_and_private_link_never_leaks():
    b = mail_body()
    b["event"]["occurrence"].update({"managedRoomUrl": "https://webinars.example.com/w/ROOM1/", "joinUrl": "https://zoom.example/j/PRIVATE1"})
    text = json.dumps(run(SES, b)[1].calls[0]["json"])
    assert "https://webinars.example.com/w/ROOM1/" in text and "PRIVATE" not in text


@pytest.mark.parametrize("mutate,error", [
    (lambda b: b["identity"].update(verified=False), "native_identity_unverified"),
    (lambda b: (b["profile"].pop("email"), b["identity"].pop("email")), "native_no_email"),
    (lambda b: b["registration"].update(status="cancelled"), "native_registration_cancelled"),
])
def test_skips_before_any_call(mutate, error):
    b = mail_body()
    mutate(b)
    outcome, fake = run(SES, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", error, [])


def test_send_ack_unconfirmed_and_missing_delivery_id_are_dead():
    outcome, fake = run({**SES, "native_send_ack": ""})
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_match_strategy_unconfirmed", [])
    b = mail_body()
    b["delivery_metadata"] = {}
    outcome, fake = run(SES, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_delivery_id_missing", [])


def test_request_size_bound_checked_before_signing(monkeypatch):
    monkeypatch.setattr(tx, "MAX_REQUEST_BYTES", 100)
    outcome, fake = run()
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "send: payload_too_large", [])


# --- responses / failures / retries ------------------------------------------------


class StatusFake:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def ctx(self):
        def request(method, url, data, headers):
            self.calls.append({"url": url, "headers": dict(headers), "data": data})
            r = self.results.pop(0) if self.results else (200, b'{"MessageId": "0100018"}')
            if isinstance(r, Exception):
                raise r
            return r

        return ni.NativeContext(post=None, request=request, renew=lambda: True)


def test_accepted_is_delivered_with_or_without_message_id():
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    log = logging.getLogger("webinars.native_providers.ses")
    log.addHandler(handler)
    try:
        for raw in (b'{"MessageId": "010001-example-id"}', b"{}"):
            fake = StatusFake((200, raw))
            assert ni.execute("ses", SES, mail_body(), "registration.created", fake.ctx()).kind == "delivered"
    finally:
        log.removeHandler(handler)
    logged = "\n".join(r.getMessage() for r in records)
    assert "accepted as MessageId 010001-example-id" in logged and SECRET not in logged and ACCESS_KEY not in logged


@pytest.mark.parametrize(
    "result,kind,error",
    [
        ((429, b"", 3), "retry", "http_status: 429"),  # TooManyRequestsException
        ((500, b""), "retry", "http_status: 500"),
        ((503, b""), "retry", "http_status: 503"),
        ((302, b""), "retry", "redirect_not_followed: http 302"),
        ((400, b""), "dead", "http_status: 400"),  # MessageRejected, SendingPaused, BadRequest...
        ((403, b""), "dead", "http_status: 403"),  # signature or permission errors
        ((404, b""), "dead", "http_status: 404"),
        (TimeoutError(), "retry", "timeout"),
        (ConnectionResetError(SECRET), "retry", "transport_error: ConnectionResetError"),
        (ValueError(f"echo {SECRET} person@example.com"), "retry", "transport_error: ValueError"),
    ],
)
def test_failures_never_echo_secrets(result, kind, error):
    fake = StatusFake(result)
    outcome = ni.execute("ses", SES, mail_body(), "registration.created", fake.ctx())
    assert (outcome.kind, outcome.error) == (kind, f"send: {error}")
    assert SECRET not in outcome.error and ACCESS_KEY not in outcome.error and "@" not in outcome.error
    if isinstance(result, tuple) and len(result) > 2:
        assert outcome.retry_after == result[2]


def test_retry_resends_same_message_with_fresh_signature(monkeypatch):
    first = run()[1].calls[0]
    monkeypatch.setattr(ses, "clock", lambda: NOW + datetime.timedelta(minutes=5))
    again = run()[1].calls[0]
    # No idempotency key exists for SES: the same message goes out again (a possible duplicate),
    # carrying the same delivery id header for downstream dedupe; only the date and signature differ.
    assert first["json"] == again["json"]
    assert first["headers"]["X-Amz-Date"] != again["headers"]["X-Amz-Date"]
    assert first["headers"]["Authorization"] != again["headers"]["Authorization"]
    assert first["json"]["Content"]["Simple"]["Headers"][0] == {"Name": "X-Webinar-Delivery-Id", "Value": DELIVERY_ID}


# --- preview / redaction --------------------------------------------------------------


def test_preview_redacts_authorization_and_shows_rendered_message():
    [call] = ni.preview("ses", SES)
    assert call["url"] == SEND_URL and call["headers"]["Authorization"] == ni.REDACTED
    assert "X-Amz-Date" in call["headers"]
    text = repr(call)
    assert SECRET not in text and ACCESS_KEY not in text and "Signature=" not in text
    assert call["body"]["Content"]["Simple"]["Subject"]["Data"] == "You're registered: Sample webinar"
    assert "Hi Sample," in call["body"]["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert ni.preview_refusal("ses", SES) is None


def test_preview_empty_without_host():
    assert ni.preview("ses", {**SES, "native_region": "mars-1"}) == []


@pytest.mark.parametrize(
    "creds, code",
    [
        ({**SES, "native_send_ack": ""}, "native_match_strategy_unconfirmed"),
        ({**SES, "native_subject_registration_created": "{nope}"}, "native_config_invalid"),
        ({k: v for k, v in SES.items() if k != "native_from_address"}, "native_config_incomplete"),
    ],
)
def test_refused_preview_reports_bounded_code(creds, code):
    assert ni.preview("ses", creds) == []
    refusal = ni.preview_refusal("ses", creds)
    assert refusal == (code, ni.PREVIEW_REFUSALS[code]) and SECRET not in repr(refusal)


# --- delivery -------------------------------------------------------------------------


class RecordingRequest:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, method, url, data, headers, timeout, total, max_bytes, allowed_hosts=None):
        host = webinars.ssrf._validated_target(url)[0]
        webinars.ssrf._check_native_request(method, host, headers, allowed_hosts)
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "data": data, "allowed": allowed_hosts})
        return self.status, b'{"MessageId": "0100018"}', None, False


@pytest.fixture
def transport(monkeypatch):
    fake = RecordingRequest()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    return fake


def _deliver(msg):
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    return msg


def test_e2e_delivered_signed_and_never_logged(occurrence, transport, caplog):
    msg, reg = _claimed(occurrence, SES)
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED and msg.attempts == 1
    [call] = transport.calls
    assert call["url"] == SEND_URL and call["allowed"] == frozenset({HOST})
    authorization = call["headers"]["Authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=") and call["headers"]["X-Amz-Date"] == "20261027T120005Z"
    sent = json.loads(call["data"])
    assert sent["Destination"] == {"ToAddresses": [reg.person.email]}
    assert sent["Content"]["Simple"]["Headers"][0]["Value"] == str(msg.id)
    assert JOIN_URL not in call["data"].decode()
    # The signature is over the bytes that were sent.
    expected = ses.sigv4("POST", HOST, ses.SEND_PATH, "", {"host": HOST, "content-type": call["headers"]["Content-Type"]}, call["data"], access_key_id=ACCESS_KEY, secret_access_key=SECRET, region=REGION, service="ses", now=NOW)[0]
    assert authorization == expected
    signature = authorization.rsplit("Signature=", 1)[1]
    assert SECRET not in caplog.text and ACCESS_KEY not in caplog.text and signature not in caplog.text
    assert SECRET not in msg.last_error and signature not in msg.last_error


def test_e2e_private_link_only_with_include_private_links(occurrence, transport):
    msg, _ = _claimed(occurrence, SES, include_private_links=True)
    _deliver(msg)
    assert f"Join link: {JOIN_URL}" in json.loads(transport.calls[0]["data"])["Content"]["Simple"]["Body"]["Text"]["Data"]


def test_e2e_managed_room_link_replaces_provider_link(occurrence, transport):
    occurrence.waiting_room_enabled = True
    occurrence.waiting_room_code = "ROOMCODE1234"
    occurrence.save()
    msg, _ = _claimed(occurrence, SES, include_private_links=True)
    _deliver(msg)
    text = json.loads(transport.calls[0]["data"])["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert "Join link: https://webinars.example.com/w/ROOMCODE1234/" in text and JOIN_URL not in text


@pytest.mark.parametrize("status,expected", [(503, S.PENDING), (429, S.PENDING), (400, S.DEAD), (403, S.DEAD)])
def test_e2e_failures(occurrence, transport, status, expected, caplog):
    transport.status = status
    msg, _ = _claimed(occurrence, SES)
    msg = _deliver(msg)
    assert msg.status == expected and msg.last_error == f"send: http_status: {status}"
    assert SECRET not in caplog.text and "Signature=" not in caplog.text


def test_e2e_operator_required_consent_blocks_before_the_call(occurrence, transport):
    msg, _ = _claimed(occurrence, SES)
    Destination.objects.filter(pk=msg.destination_id).update(required_consents=["sms"])
    msg = _deliver(msg)
    assert transport.calls == [] and msg.status == S.CANCELLED


def test_e2e_region_change_after_enqueue_pins_the_new_host_only(occurrence, transport):
    msg, _ = _claimed(occurrence, SES)
    Destination.objects.filter(pk=msg.destination_id).update(url="https://email.us-east-1.amazonaws.com")
    from webinars.crypto import encrypt_secret

    Destination.objects.filter(pk=msg.destination_id).update(credentials_encrypted=encrypt_secret(json.dumps({**SES, "native_region": "us-east-1"})))
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED
    [call] = transport.calls
    assert call["url"].startswith("https://email.us-east-1.amazonaws.com/") and call["allowed"] == frozenset({"email.us-east-1.amazonaws.com"})
    assert "/20261027/us-east-1/ses/aws4_request" in call["headers"]["Authorization"]


def test_e2e_never_sent_to_another_host(occurrence, monkeypatch, transport, caplog):
    msg, _ = _claimed(occurrence, SES)
    monkeypatch.setattr(ni, "base_url", lambda provider, credentials: "https://api.segment.io")
    msg = _deliver(msg)
    assert transport.calls == [] and msg.status == S.DEAD and msg.last_error.endswith(": unsafe_destination")
    assert SECRET not in caplog.text


def test_e2e_without_request_transport_dead(occurrence, monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    msg, _ = _claimed(occurrence, SES)
    msg = _deliver(msg)
    assert msg.status == S.DEAD and msg.last_error == "native_transport_unavailable"
