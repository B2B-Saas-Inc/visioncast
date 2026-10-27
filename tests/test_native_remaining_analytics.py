"""Mixpanel and RudderStack native adapters: mocked request contracts, consent, credentials and host policy.

No network: transports are fakes and socket connects raise. These pin our request shapes to the
contracts described in docs/native-integrations.md. Some of them were checked against primary
vendor docs fetched 2026-10-25 (see that file); the rest, and all live behaviour, are unverified.
"""
import base64
import re
import socket
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

import webinars.delivery
import webinars.ssrf
from tests.test_native_integrations import Fake, _claimed, body
from webinars import native_integrations as ni
from webinars.models import Destination, OutboxMessage
from webinars.ssrf import UnsafeDestination

pytestmark = pytest.mark.django_db

ACK = {"native_processing_ack": "confirmed"}
MP_USER = "svc.abc123.mp-service-account"
MP_SECRET = "mpServiceSecret_456"
RS_KEY = "rs_write_key_789"
RS_HOST = "acme.dataplane.rudderstack.com"
MIXPANEL = {
    "native_provider": "mixpanel",
    "native_region": "eu",
    "native_project_id": "1234567",
    "native_service_account_username": MP_USER,
    "native_service_account_secret": MP_SECRET,
    **ACK,
}
RUDDER = {"native_provider": "rudderstack", "native_region": "dataplane", "native_dataplane_host": RS_HOST, "native_write_key": RS_KEY, **ACK}
SECRETS = (MP_USER, MP_SECRET, RS_KEY)
OCCURRED_S = 1767268800  # SAMPLE_BODY occurredAt 2026-01-01T12:00:00Z
GROUPS = [g for g in Destination.GROUPS if g != "profile"]
KW = dict(event_types=["registration.created"], allowed_groups=GROUPS, required_consents=["analytics"])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", "api-eu.mixpanel.com", "api.mixpanel.com", RS_HOST]


@pytest.fixture
def pinned(monkeypatch):
    """Delivery pins per-destination hosts (delivery now sets this itself; kept explicit for these unit tests)."""
    monkeypatch.setattr(webinars.delivery, "NATIVE_DESTINATION_HOSTS", True, raising=False)


def dest(**kw):
    return Destination(**{**KW, **kw})


def run(creds, b=None, event_type="registration.created", fake=None):
    fake = fake or Fake()
    return ni.execute(creds["native_provider"], creds, b if b is not None else body(), event_type, fake.ctx()), fake


def consents(analytics):
    return {"analytics": {"granted": analytics, "source": "trusted_api", "serverVerified": True}}


def basic(user, pw=""):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


# --- schema / credentials -----------------------------------------------------


def test_offered_and_schema_driven():
    labels = dict(ni.PROVIDER_CHOICES)
    assert labels["mixpanel"] == "Mixpanel" and labels["rudderstack"] == "RudderStack"
    for p in ("mixpanel", "rudderstack"):
        assert ni.FIELDS[p][0].name == "region" and ni.FIELDS[p][0].choices
        assert ni.PROCESSING_ACK in ni.FIELDS[p]
        assert p in ni.ANALYTICS_PROVIDERS
    assert set(ni.HOSTS["mixpanel"].values()) <= ni.VENDOR_HOSTS
    assert "rudderstack" not in ni.HOSTS and RS_HOST not in ni.VENDOR_HOSTS


def test_secrets_never_in_config_or_flags():
    for creds in (MIXPANEL, RUDDER):
        p = creds["native_provider"]
        rendered = repr(ni.config_of(creds, p)) + repr(ni.secrets_set(creds, p))
        assert not any(s in rendered for s in SECRETS)
    assert ni.secrets_set(MIXPANEL, "mixpanel") == {"service_account_username": True, "service_account_secret": True}
    assert ni.config_of(RUDDER, "rudderstack")["dataplane_host"] == RS_HOST


def test_blank_secret_keeps_stored_value_and_switch_drops():
    posted = {"region": "eu", "project_id": "1234567", "service_account_username": "", "service_account_secret": "", "processing_ack": "confirmed"}
    out, _ = ni.merge_credentials(MIXPANEL, "mixpanel", posted)
    assert out["native_service_account_secret"] == MP_SECRET and out["native_service_account_username"] == MP_USER
    out, notes = ni.merge_credentials(MIXPANEL, "rudderstack", {"region": "dataplane", "dataplane_host": RS_HOST, "write_key": ""})
    assert not any(v in out.values() for v in (MP_USER, MP_SECRET)) and "native_write_key" not in out and notes


def test_username_with_colon_rejected():
    assert "native-service_account_username" in ni.validate("mixpanel", {**MIXPANEL, "native_service_account_username": "user:name"}, **KW)


@pytest.mark.parametrize("field,value", [("project_id", "abc"), ("project_id", "12 3"), ("region", "au")])
def test_mixpanel_field_validation(field, value):
    assert f"native-{field}" in ni.validate("mixpanel", {**MIXPANEL, f"native_{field}": value}, **KW)


def test_missing_secret_is_dead_without_calls(pinned):
    for creds, key in ((MIXPANEL, "native_service_account_secret"), (RUDDER, "native_write_key")):
        outcome, fake = run({**creds, key: ""})
        assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])


# --- consent / processing ack -------------------------------------------------


def test_valid_configs(pinned):
    assert ni.validate("mixpanel", MIXPANEL, **KW) == {}
    assert ni.validate("rudderstack", RUDDER, **KW) == {}


@pytest.mark.parametrize("creds", [MIXPANEL, RUDDER])
def test_processing_ack_required(creds, pinned):
    c = {k: v for k, v in creds.items() if k != "native_processing_ack"}
    assert "native-processing_ack" in ni.validate(c["native_provider"], c, **KW)
    assert ni.check_send_policy(c["native_provider"], c, dest()) == "native_config_invalid"


@pytest.mark.parametrize("creds", [MIXPANEL, RUDDER])
def test_analytics_consent_required(creds, pinned):
    assert "required_consents" in ni.validate(creds["native_provider"], creds, **{**KW, "required_consents": []})
    assert ni.check_send_policy(creds["native_provider"], creds, dest(required_consents=["marketing"])) == "native_consent_policy_invalid"


@pytest.mark.parametrize("creds", [MIXPANEL, RUDDER])
def test_snapshot_without_analytics_grant_is_cancelled(creds, pinned):
    for value in (False, None):
        b = body()
        b["consents"] = consents(value) if value is not None else {}
        outcome, fake = run(creds, b)
        assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_analytics_consent_missing", [])
    b = body()
    b["consents"] = consents(True)
    assert run(creds, b)[0].kind == "delivered"


@pytest.mark.parametrize("creds", [MIXPANEL, RUDDER])
def test_unverified_identity_never_sent(creds, pinned):
    b = body()
    b["identity"]["verified"] = False
    outcome, fake = run(creds, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_identity_unverified", [])


# --- RudderStack host policy (fail closed) ------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "https://acme.dataplane.rudderstack.com",
        "acme.dataplane.rudderstack.com:443",
        "acme.dataplane.rudderstack.com/v1",
        "user@acme.dataplane.rudderstack.com",
        "*.dataplane.rudderstack.com",
        "Acme.Dataplane.Rudderstack.com",
        "acme.dataplane.rudderstack.com.",
        "10.0.0.1",
        "localhost",
        "rudder",
        "",
    ],
)
def test_bad_host_formats_rejected(host, settings, pinned):
    settings.WEBHOOK_HOST_ALLOWLIST = [host or "x.example.com", RS_HOST]
    creds = {**RUDDER, "native_dataplane_host": host}
    assert ni.validate("rudderstack", creds, **KW)
    assert ni.base_url("rudderstack", creds) is None and ni.allowed_hosts("rudderstack", creds) == frozenset()
    outcome, fake = run(creds)
    assert (outcome.kind, fake.calls) == ("dead", [])


@pytest.mark.parametrize(
    "entries,host",
    [
        (["rudderstack.com"], RS_HOST),  # parent domain does not admit sub-domains here
        (["*.dataplane.rudderstack.com"], RS_HOST),  # wildcard does not either
        ([".dataplane.rudderstack.com"], RS_HOST),
        ([RS_HOST], RS_HOST + ".evil.net"),  # suffix trick
        ([RS_HOST], "evil" + RS_HOST),
        (["hooks.example.net"], RS_HOST),
    ],
)
def test_host_needs_exact_operator_entry(entries, host, settings, pinned):
    settings.WEBHOOK_HOST_ALLOWLIST = entries
    creds = {**RUDDER, "native_dataplane_host": host}
    errors = ni.validate("rudderstack", creds, **KW)
    assert any("exactly" in m for m in errors.get("provider", []))
    assert ni.check_send_policy("rudderstack", creds, dest()) == "native_config_invalid"
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])


def test_other_vendor_host_refused(settings, pinned):
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.segment.io"]
    errors = ni.validate("rudderstack", {**RUDDER, "native_dataplane_host": "api.segment.io"}, **KW)
    assert "native-dataplane_host" in errors


def test_host_removed_from_allowlist_after_enable_stops_send(settings, pinned):
    assert ni.check_send_policy("rudderstack", RUDDER, dest()) is None
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    assert ni.check_send_policy("rudderstack", RUDDER, dest()) == "native_config_invalid"
    assert run(RUDDER)[1].calls == []


def test_allowed_hosts_pins_single_host():
    assert ni.allowed_hosts("rudderstack", RUDDER) == frozenset({RS_HOST})
    assert ni.allowed_hosts("mixpanel", MIXPANEL) == frozenset({"api-eu.mixpanel.com"})


def test_delivery_declares_destination_host_pinning():
    assert webinars.delivery.NATIVE_DESTINATION_HOSTS is True and ni.destination_hosts_supported()
    assert ni.validate("rudderstack", RUDDER, **KW) == {}
    assert ni.check_send_policy("rudderstack", RUDDER, dest()) is None


def test_rudderstack_unsendable_without_delivery_host_pinning(monkeypatch):
    # An older delivery module without the constant: fail closed.
    monkeypatch.delattr(webinars.delivery, "NATIVE_DESTINATION_HOSTS", raising=False)
    errors = ni.validate("rudderstack", RUDDER, **KW)
    assert any("per-destination host pinning" in m for m in errors["provider"])
    assert ni.check_send_policy("rudderstack", RUDDER, dest()) == "native_transport_unavailable"
    outcome, fake = run(RUDDER)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_transport_unavailable", [])
    # Mixpanel uses static vendor hosts and is unaffected.
    assert ni.check_send_policy("mixpanel", MIXPANEL, dest()) is None


# --- Mixpanel mapping ---------------------------------------------------------


def test_mixpanel_exact_request():
    outcome, fake = run(MIXPANEL)
    assert outcome.kind == "delivered"
    (call,) = fake.calls
    assert call["method"] == "POST"
    assert call["url"] == "https://api-eu.mixpanel.com/import?strict=1&project_id=1234567"
    assert call["headers"]["Authorization"] == basic(MP_USER, MP_SECRET)
    assert call["headers"]["Content-Type"].startswith("application/json")
    (event,) = call["json"]
    props = event["properties"]
    assert event["event"] == "webinar registered"
    assert props["distinct_id"] == ni.SAMPLE_BODY["identity"]["personId"]
    assert props["time"] == OCCURRED_S * 1000
    assert len(props["$insert_id"]) == 36
    assert props["webinar_registration_id"] == ni.SAMPLE_BODY["registration"]["id"]
    # No profile data, no secrets in the body.
    text = repr(call["json"])
    assert "person@example.com" not in text and "Sample" not in text.replace("Sample webinar", "")
    assert not any(s in text for s in SECRETS)


def test_mixpanel_insert_id_stable_across_retries_and_distinct_per_delivery():
    first = run(MIXPANEL)[1].calls[0]["json"][0]["properties"]["$insert_id"]
    again = run(MIXPANEL)[1].calls[0]["json"][0]["properties"]["$insert_id"]
    b = body()
    b["delivery_metadata"] = {"deliveryId": "00000000-0000-4000-8000-000000000099"}
    other = run(MIXPANEL, b)[1].calls[0]["json"][0]["properties"]["$insert_id"]
    assert first == again != other


def test_mixpanel_attendance_names():
    b = body()
    b["registration"]["attendance"] = "no_show"
    assert run(MIXPANEL, b, "attendance.recorded")[1].calls[0]["json"][0]["event"] == "webinar no-show"
    b["registration"]["attendance"] = "weird"
    assert run(MIXPANEL, b, "attendance.recorded")[0].error == "native_attendance_unknown"


def test_mixpanel_no_timestamp_not_invented():
    b = body(occurredAt="not-a-date")
    outcome, fake = run(MIXPANEL, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_no_timestamp", [])


@pytest.mark.parametrize("creds", [MIXPANEL, RUDDER])
def test_missing_delivery_id_is_dead(creds, pinned):
    b = body()
    b["delivery_metadata"] = {}
    outcome, fake = run(creds, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_delivery_id_missing", [])


# --- RudderStack mapping ------------------------------------------------------


def test_rudderstack_exact_requests(pinned):
    outcome, fake = run(RUDDER)
    assert outcome.kind == "delivered"
    identify, track = fake.calls
    assert identify["url"] == f"https://{RS_HOST}/v1/identify" and track["url"] == f"https://{RS_HOST}/v1/track"
    assert identify["headers"]["Authorization"] == track["headers"]["Authorization"] == basic(RS_KEY)
    pid = ni.SAMPLE_BODY["identity"]["personId"]
    assert identify["json"]["userId"] == track["json"]["userId"] == pid
    assert identify["json"]["traits"]["email"] == "person@example.com"
    assert track["json"]["event"] == "webinar registered"
    assert track["json"]["timestamp"] == ni.SAMPLE_BODY["occurredAt"]
    assert identify["json"]["messageId"] != track["json"]["messageId"]
    seg_ids = {c["json"]["messageId"] for c in run({"native_provider": "segment", "native_region": "us", "native_write_key": "segkey123", **ACK})[1].calls}
    assert not seg_ids & {identify["json"]["messageId"], track["json"]["messageId"]}


def test_rudderstack_without_profile_sends_track_only(pinned):
    b = body()
    b.pop("profile")
    (track,) = run(RUDDER, b)[1].calls
    assert track["url"].endswith("/v1/track") and "traits" not in track["json"]


# --- vendor contract facts (primary docs fetched 2026-10-25) ------------------


class RawFake(Fake):
    """Also records the exact bytes handed to the transport."""

    def __init__(self):
        super().__init__()
        self.sizes = []

    def post(self, url, data, headers):
        self.sizes.append(len(data))
        return super().post(url, data, headers)


def test_mixpanel_import_record_fields():
    (call,) = run(MIXPANEL)[1].calls
    (event,) = call["json"]
    props = event["properties"]
    assert set(event) == {"event", "properties"}
    # Required: time (we send ms), distinct_id, $insert_id (<= 36 bytes, alphanumeric/hyphen).
    assert props["time"] == OCCURRED_S * 1000 and props["distinct_id"]
    assert re.fullmatch(r"[A-Za-z0-9-]{1,36}", props["$insert_id"]) and len(props["$insert_id"].encode()) <= 36
    # ip=0: no geolocation from our server's request IP.
    assert props["ip"] == 0
    assert len(props) < 255
    assert all(isinstance(v, (str, int, float, bool)) for v in props.values())


def test_mixpanel_ip_zero_shown_in_preview():
    (call,) = ni.preview("mixpanel", MIXPANEL)
    assert call["body"][0]["properties"]["ip"] == 0


def _sized(creds, step_index, target):
    """A body whose ``step_index`` request serializes to exactly ``target`` bytes (via the event title)."""
    b = body()
    b["event"]["title"] = "x"
    fake = Fake()
    run(creds, b, fake=fake)
    base = len(ni._encode(fake.calls[step_index]["json"]))
    b["event"]["title"] = "x" * (1 + target - base)
    return b


def test_mixpanel_event_size_limit_on_serialized_bytes():
    limit = ni.MIXPANEL_MAX_EVENT_BYTES
    fake = RawFake()
    outcome, _ = run(MIXPANEL, _sized(MIXPANEL, 0, limit - 1), fake=fake)
    assert outcome.kind == "delivered" and fake.sizes == [limit - 1]
    outcome, fake = run(MIXPANEL, _sized(MIXPANEL, 0, limit))
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "import: payload_too_large", [])
    # UTF-8 bytes, not characters: 500k characters, 1 MB of bytes.
    b = body()
    b["event"]["title"] = "é" * (limit // 2)
    outcome, fake = run(MIXPANEL, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "import: payload_too_large", [])


def test_mixpanel_property_key_limit(monkeypatch):
    monkeypatch.setattr(ni, "MIXPANEL_MAX_PROPERTIES", 3)
    outcome, fake = run(MIXPANEL)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "import: payload_unsupported", [])


@pytest.mark.parametrize("value", [{"a": {"b": {"c": {"d": 1}}}}, ["a"], {"a": 1}])
@pytest.mark.parametrize("creds,error", [(MIXPANEL, "import: payload_unsupported"), (RUDDER, "track: payload_unsupported")])
def test_nested_or_array_properties_refused_before_any_call(creds, error, value, pinned):
    b = body()
    b["event"]["title"] = value
    outcome, fake = run(creds, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", error, [])  # RudderStack: identify not sent either


def test_rudderstack_nested_trait_refused(pinned):
    b = body()
    b["profile"]["firstName"] = {"given": "Sample"}
    outcome, fake = run(RUDDER, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "identify: payload_unsupported", [])


def test_rudderstack_call_size_limit_on_serialized_bytes(pinned):
    limit = ni.RUDDERSTACK_MAX_CALL_BYTES
    fake = RawFake()
    outcome, _ = run(RUDDER, _sized(RUDDER, 1, limit), fake=fake)
    assert outcome.kind == "delivered" and fake.sizes[1] == limit
    outcome, fake = run(RUDDER, _sized(RUDDER, 1, limit + 1))
    # Checked for every step before the first call: no identify is sent ahead of an oversize track.
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "track: payload_too_large", [])
    b = body()
    b["event"]["title"] = "é" * (limit // 2)
    outcome, fake = run(RUDDER, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "track: payload_too_large", [])


def test_rudderstack_request_basics(pinned):
    identify, track = run(RUDDER)[1].calls
    for call in (identify, track):
        assert call["headers"]["Content-Type"].startswith("application/json")
        # curl -u <write_key>: -> key as username, empty password.
        assert base64.b64decode(call["headers"]["Authorization"].split(" ", 1)[1]).decode() == f"{RS_KEY}:"
        assert call["json"]["userId"] and "anonymousId" not in call["json"]
    assert set(identify["json"]["traits"]) <= {"email", "firstName", "lastName", "title"}


def test_rudderstack_omits_unparseable_timestamp(pinned):
    b = body(occurredAt="not-a-date")
    calls = run(RUDDER, b)[1].calls
    assert calls and all("timestamp" not in c["json"] for c in calls)
    # Segment's behaviour is deliberately unchanged by this pass.
    seg = {"native_provider": "segment", "native_region": "us", "native_write_key": "segkey123", **ACK}
    assert all(c["json"]["timestamp"] == "not-a-date" for c in run(seg, b)[1].calls)


# --- status / transport failures ----------------------------------------------


class StatusFake:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def ctx(self):
        def post(url, data, headers):
            self.calls += 1
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        return ni.NativeContext(post=post, request=None, renew=lambda: True)


@pytest.mark.parametrize(
    "result,kind,error",
    [
        ((429, "", 30), "retry", "http_status: 429"),
        ((500, ""), "retry", "http_status: 500"),
        ((503, ""), "retry", "http_status: 503"),
        ((302, ""), "retry", "redirect_not_followed: http 302"),
        ((400, ""), "dead", "http_status: 400"),  # Mixpanel strict=1: any invalid record
        ((401, ""), "dead", "http_status: 401"),
        ((404, ""), "dead", "http_status: 404"),  # RudderStack: source disabled
        ((413, ""), "dead", "http_status: 413"),
        (TimeoutError(), "retry", "timeout"),
        (ConnectionResetError(MP_SECRET), "retry", "transport_error: ConnectionResetError"),
        (ValueError(f"echo {MP_SECRET} person@example.com"), "retry", "transport_error: ValueError"),
    ],
)
@pytest.mark.parametrize("creds", [MIXPANEL, RUDDER])
def test_status_and_transport_failures(creds, result, kind, error, pinned):
    fake = StatusFake(result)
    outcome = ni.execute(creds["native_provider"], creds, body(), "registration.created", fake.ctx())
    assert outcome.kind == kind and outcome.error.endswith(error)
    assert not any(s in outcome.error for s in SECRETS) and "@" not in outcome.error
    assert fake.calls == 1  # first failing step stops the sequence
    if result == (429, "", 30):
        assert outcome.retry_after == 30


# --- preview ------------------------------------------------------------------


@pytest.mark.parametrize("creds", [MIXPANEL, RUDDER])
def test_preview_redacts_credentials(creds):
    calls = ni.preview(creds["native_provider"], creds)
    assert calls
    text = repr(calls)
    assert not any(s in text for s in SECRETS)
    assert all(c["headers"]["Authorization"] == ni.REDACTED for c in calls)


def test_preview_empty_for_unallowlisted_dataplane(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["rudderstack.com"]
    assert ni.preview("rudderstack", RUDDER) == []


# --- delivery host pinning ----------------------------------------------------

S = OutboxMessage.Status
OTHER_HOSTS = ["api.segment.io", "hooks.example.net"]


class RecordingRequest:
    """Stands in for ``ssrf.request_json_bounded``; records the host set delivery passed."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, data, headers, timeout, total, max_bytes, allowed_hosts=None):
        self.calls.append({"url": url, "allowed": allowed_hosts})
        return 200, b"{}", None, False


class Transport:
    """Stands in for ``post_json_pinned`` (delivery's in-process test seam)."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, data, headers, timeout):
        self.calls.append({"url": url, "headers": dict(headers)})
        return 200, ""


@pytest.fixture
def transport(monkeypatch):
    fake = Transport()
    monkeypatch.setattr(webinars.delivery, "post_json_pinned", fake, raising=False)
    monkeypatch.setattr(webinars.ssrf, "post_json_pinned", fake)
    return fake


def leased():
    return SimpleNamespace(lease_expires_at=timezone.now() + timedelta(seconds=120))


def test_native_seams_default_to_vendor_hosts(monkeypatch, settings):
    """Compatibility: callers that do not pass ``allowed_hosts`` keep the static vendor set."""
    settings.WEBHOOK_HOST_ALLOWLIST = [*OTHER_HOSTS, "api-eu.mixpanel.com"]
    fake = RecordingRequest()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    webinars.delivery._native_request(leased(), "POST", "https://api.segment.io/v1/track", b"{}", {})
    webinars.delivery._native_post(leased(), "https://api.segment.io/v1/track", b"{}", {})
    assert [c["allowed"] for c in fake.calls] == [ni.VENDOR_HOSTS, ni.VENDOR_HOSTS]
    pinned_set = frozenset({"api-eu.mixpanel.com"})
    webinars.delivery._native_post(leased(), "https://api-eu.mixpanel.com/import", b"[]", {}, allowed_hosts=pinned_set)
    assert fake.calls[-1]["allowed"] == pinned_set


@pytest.mark.parametrize("allowed", [frozenset({"api-eu.mixpanel.com"}), frozenset()])
def test_native_seams_refuse_hosts_outside_the_pinned_set(allowed, monkeypatch, settings, transport):
    settings.WEBHOOK_HOST_ALLOWLIST = [*OTHER_HOSTS, "api-eu.mixpanel.com"]
    auth = {"Authorization": basic(MP_USER, MP_SECRET)}
    # post_json_pinned seam: checked in-process before the fake transport is called.
    with pytest.raises(UnsafeDestination):
        webinars.delivery._native_post(leased(), "https://api.segment.io/v1/track", b"{}", auth, allowed_hosts=allowed)
    assert transport.calls == []
    # Real path: ssrf.request_json_bounded refuses before spawning a child (no network).
    monkeypatch.setattr(webinars.delivery, "post_json_pinned", webinars.delivery._PINNED)
    for call in (
        lambda: webinars.delivery._native_post(leased(), "https://api.segment.io/v1/track", b"{}", auth, allowed_hosts=allowed),
        lambda: webinars.delivery._native_request(leased(), "POST", "https://hooks.example.net/x", b"{}", auth, allowed_hosts=allowed),
    ):
        with pytest.raises(UnsafeDestination):
            call()


@pytest.mark.parametrize("creds,host", [(MIXPANEL, "api-eu.mixpanel.com"), (RUDDER, RS_HOST)])
def test_delivery_pins_destination_host(creds, host, occurrence, monkeypatch):
    fake = RecordingRequest()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg, _ = _claimed(occurrence, creds, required_consents=["analytics"])
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    assert msg.status == S.DELIVERED and fake.calls
    for call in fake.calls:
        assert call["allowed"] == frozenset({host}) and call["url"].startswith(f"https://{host}/")


@pytest.mark.parametrize("creds,host", [(MIXPANEL, "api-eu.mixpanel.com"), (RUDDER, RS_HOST)])
def test_e2e_delivered_through_seam_to_own_host(creds, host, occurrence, transport):
    msg, _ = _claimed(occurrence, creds, required_consents=["analytics"])
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    assert msg.status == S.DELIVERED and transport.calls
    assert all(c["url"].startswith(f"https://{host}/") for c in transport.calls)


@pytest.mark.parametrize(
    "creds,target",
    [
        (MIXPANEL, "https://api.segment.io"),  # another static vendor host (in VENDOR_HOSTS)
        (MIXPANEL, "https://api.mixpanel.com"),  # same vendor, other residency than configured
        (RUDDER, "https://api.segment.io"),
        (RUDDER, "https://hooks.example.net"),  # operator-allowlisted, but not this destination's host
    ],
)
def test_credentials_never_sent_to_another_host(creds, target, occurrence, monkeypatch, settings, transport, caplog):
    """An adapter that built a URL on any host but the configured one is refused before the call."""
    settings.WEBHOOK_HOST_ALLOWLIST = [*OTHER_HOSTS, "api.mixpanel.com", "api-eu.mixpanel.com", RS_HOST]
    msg, _ = _claimed(occurrence, creds, required_consents=["analytics"])
    monkeypatch.setattr(ni, "base_url", lambda provider, credentials: target)
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    assert transport.calls == []
    assert msg.status == S.DEAD and msg.last_error.endswith(": unsafe_destination")
    assert not any(s in msg.last_error or s in caplog.text for s in SECRETS)
