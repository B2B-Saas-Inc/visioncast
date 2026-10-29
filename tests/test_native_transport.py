"""Native vendor transport (``ssrf.request_json_bounded``) and its use by native delivery.

No DNS or network I/O: child targets are stand-ins that never open sockets, the
in-process request test replaces DNS and the pinned connection, and delivery tests
replace the transport function.

Keep module-level imports light: spawned children import this module to find targets.
"""
import datetime
import json
import multiprocessing
import os
import time

import pytest

from webinars import ssrf

URL = "https://api.hubapi.com/crm/v3/objects/contacts/batch/upsert"
VENDORS = frozenset({"api.hubapi.com", "api.attio.com", "us.i.posthog.com"})
AUTH = {"Authorization": "Bearer tok-secret-1", "Content-Type": "application/json"}


@pytest.fixture
def allow(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hubapi.com", "attio.com", "posthog.com", "hooks.example.net"]
    settings.WEBHOOK_ALLOWED_PORTS = [443]


# ---------------------------------------------------------------- child targets (module-level)


def _ok_child(conn, host, port, path, method, body, headers, timeout, max_bytes):
    conn.send(("ok", (201, json.dumps({"host": host, "path": path, "method": method, "timeout": timeout, "max": max_bytes}).encode(), "120", False), ""))
    conn.close()


def _oversize_child(conn, *args):
    conn.send(("ok", (200, b"x" * (ssrf.MAX_NATIVE_RESPONSE_BYTES + 10), "", False), ""))
    conn.close()


def _hang_child(conn, *args):
    time.sleep(120)


def _tls_child(conn, *args):
    conn.send(("tls", "SSLCertVerificationError", ""))
    conn.close()


def _unsafe_child(conn, *args):
    conn.send(("unsafe", "Host resolves to a non-public address", ""))
    conn.close()


def _crash_child(conn, *args):
    os._exit(3)


# ---------------------------------------------------------------- Retry-After parsing


def test_parse_retry_after_seconds_and_cap():
    assert ssrf.parse_retry_after("120") == 120
    assert ssrf.parse_retry_after(" 0 ") == 0
    assert ssrf.parse_retry_after("999999999") == ssrf.MAX_RETRY_AFTER_SECONDS


def test_parse_retry_after_http_date():
    now = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    assert ssrf.parse_retry_after("Thu, 01 Jan 2026 12:02:00 GMT", now=now) == 120
    assert ssrf.parse_retry_after("Thu, 01 Jan 2026 11:00:00 GMT", now=now) == 0
    assert ssrf.parse_retry_after("Fri, 01 Jan 2027 12:00:00 GMT", now=now) == ssrf.MAX_RETRY_AFTER_SECONDS


@pytest.mark.parametrize("value", [None, "", "soon", "-5", "1.5", "9" * 100, "Thu, 99 Foo 2026"])
def test_parse_retry_after_rejects_garbage(value):
    assert ssrf.parse_retry_after(value) is None


# ---------------------------------------------------------------- policy before spawn


@pytest.fixture
def no_spawn(monkeypatch):
    spawned = []
    monkeypatch.setattr(multiprocessing, "get_context", lambda *a: spawned.append(a) or None)
    return spawned


@pytest.mark.parametrize("method", ["DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE", "post\r\nX: y"])
def test_only_post_and_put(allow, no_spawn, method):
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.request_json_bounded(method, URL, b"{}", {}, 10, 5, allowed_hosts=VENDORS, _target=_ok_child)
    assert no_spawn == []


@pytest.mark.parametrize("credential_header", ["authorization", "api-key", "Api-Key", "x-api-token", "X-Api-Token"])
def test_credentials_need_explicit_vendor_hosts(allow, no_spawn, credential_header):
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.request_json_bounded("POST", "https://hooks.example.net/x", b"{}", AUTH, 10, 5, _target=_ok_child)
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.request_json_bounded("POST", "https://hooks.example.net/x", b"{}", AUTH, 10, 5, allowed_hosts=VENDORS, _target=_ok_child)
    # Lower-case header name is still a credential.
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.request_json_bounded("PUT", "https://hooks.example.net/x", b"{}", {credential_header: "x"}, 10, 5, _target=_ok_child)
    assert no_spawn == []


@pytest.mark.parametrize(
    "url",
    [
        "http://api.hubapi.com/x",
        "https://127.0.0.1/x",
        "https://api.hubapi.com.evil.example.org/x",
        "https://user:pw@api.hubapi.com/x",
        "https://api.hubapi.com:8443/x",
        "https://localhost/x",
    ],
)
def test_url_policy_unchanged(allow, no_spawn, url):
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.request_json_bounded("POST", url, b"{}", AUTH, 10, 5, allowed_hosts=VENDORS, _target=_ok_child)
    assert no_spawn == []


def test_vendor_host_must_also_be_operator_allowlisted(settings, no_spawn):
    settings.NATIVE_VENDOR_HOSTS_IMPLICIT = False
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.request_json_bounded("POST", URL, b"{}", AUTH, 10, 5, allowed_hosts=VENDORS, _target=_ok_child)
    assert no_spawn == []


def test_pinned_vendor_host_needs_no_operator_entry_by_default(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = []
    status, *_ = ssrf.request_json_bounded("POST", URL, b"{}", AUTH, 10, 5, allowed_hosts=VENDORS, _target=_ok_child)
    assert status == 201  # _ok_child answers 201
    with pytest.raises(ssrf.UnsafeDestination):  # the pin is the whole point: an unpinned host still needs the allowlist
        ssrf.request_json_bounded("POST", URL, b"{}", AUTH, 10, 5, allowed_hosts=frozenset({"other.example.net"}), _target=_ok_child)
    with pytest.raises(ssrf.UnsafeDestination):  # generic webhooks are unaffected
        ssrf.validate_destination_url(URL)


# ---------------------------------------------------------------- child execution


def test_child_result_parsed_and_bounded(allow):
    status, raw, retry_after, truncated = ssrf.request_json_bounded(
        "put", URL + "?a=1", b"{}", AUTH, 10, 3, max_response_bytes=10**9, allowed_hosts=VENDORS, _target=_ok_child
    )
    assert status == 201 and retry_after == 120 and truncated is False
    seen = json.loads(raw)
    assert seen == {"host": "api.hubapi.com", "path": "/crm/v3/objects/contacts/batch/upsert?a=1", "method": "PUT", "timeout": 3.0, "max": ssrf.MAX_NATIVE_RESPONSE_BYTES}


def test_parent_rejects_oversized_child_body(allow):
    status, raw, _ra, truncated = ssrf.request_json_bounded("POST", URL, b"{}", AUTH, 10, 20, allowed_hosts=VENDORS, _target=_oversize_child)
    assert status == 200 and raw == b"" and truncated is True


def test_hanging_request_killed_at_deadline(allow):
    start = time.monotonic()
    with pytest.raises(ssrf.DeadlineExceeded):
        ssrf.request_json_bounded("POST", URL, b"{}", AUTH, 10, 0.5, allowed_hosts=VENDORS, _target=_hang_child)
    assert time.monotonic() - start < 0.5 + 2 * ssrf.TERMINATE_GRACE_SECONDS + 10
    assert not [p for p in multiprocessing.active_children() if p.is_alive()]


@pytest.mark.parametrize(
    "target,exc",
    [(_tls_child, ssrf.ssl.SSLError), (_unsafe_child, ssrf.UnsafeDestination), (_crash_child, OSError)],
)
def test_child_errors_mapped(allow, target, exc):
    with pytest.raises(exc):
        ssrf.request_json_bounded("POST", URL, b"{}", AUTH, 10, 20, allowed_hosts=VENDORS, _target=target)


def test_post_json_bounded_still_works(allow):
    # Old webhook transport untouched: same seam, same result shape.
    def_target = __import__("tests.test_security_transport", fromlist=["_ok_child"])._ok_child
    status, snippet = ssrf.post_json_bounded("https://hooks.example.net/webhook", b"{}", {}, 10, 3, _target=def_target)
    assert status == 204 and snippet.startswith("hooks.example.net|443|/webhook")


# ---------------------------------------------------------------- in-process request (pinning, redirects, size)


class FakeResponse:
    def __init__(self, status, body, headers):
        self.status, self._body, self._headers = status, body, headers

    def read(self, n):
        return self._body[:n]

    def getheader(self, name):
        return self._headers.get(name)


class FakeConn:
    instances = []
    response = FakeResponse(200, b"{}", {})

    def __init__(self, host, *, pinned_ip, port, timeout, context):
        self.host, self.pinned_ip, self.port, self.requests, self.closed = host, pinned_ip, port, [], False
        FakeConn.instances.append(self)

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, dict(headers)))

    def getresponse(self):
        return FakeConn.response

    def close(self):
        self.closed = True


@pytest.fixture
def fake_net(monkeypatch):
    FakeConn.instances = []
    monkeypatch.setattr(ssrf, "resolve_safe_addresses", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(ssrf, "_PinnedHTTPSConnection", FakeConn)
    return FakeConn


def test_request_pins_vetted_ip_and_strips_hop_headers(fake_net):
    fake_net.response = FakeResponse(200, b'{"ok":1}', {"Retry-After": "7"})
    headers = {**AUTH, "Host": "evil.example", "Content-Length": "999", "Connection": "keep-alive"}
    status, raw, retry_after, truncated = ssrf._request_validated("api.hubapi.com", 443, "/p", "PUT", b"{}", headers, 5, 1024)
    assert (status, raw, retry_after, truncated) == (200, b'{"ok":1}', "7", False)
    [conn] = fake_net.instances
    assert conn.pinned_ip == "93.184.216.34" and conn.host == "api.hubapi.com" and conn.closed
    [(method, path, _body, sent)] = conn.requests
    assert method == "PUT" and path == "/p"
    assert "Host" not in sent and "Content-Length" not in sent and sent["Connection"] == "close"


def test_redirect_returned_not_followed(fake_net):
    fake_net.response = FakeResponse(302, b"", {"Location": "https://169.254.169.254/"})
    status, *_ = ssrf._request_validated("api.hubapi.com", 443, "/p", "POST", b"{}", AUTH, 5, 1024)
    assert status == 302
    assert len(fake_net.instances) == 1 and len(fake_net.instances[0].requests) == 1


def test_oversized_response_dropped(fake_net):
    fake_net.response = FakeResponse(200, b"a" * 2000, {})
    status, raw, _ra, truncated = ssrf._request_validated("api.hubapi.com", 443, "/p", "POST", b"{}", AUTH, 5, 1024)
    assert raw == b"" and truncated is True


def test_private_resolution_never_connects(monkeypatch):
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: [(ssrf.socket.AF_INET, 1, 6, "", ("10.0.0.5", 443))])
    FakeConn.instances = []
    monkeypatch.setattr(ssrf, "_PinnedHTTPSConnection", FakeConn)
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf._request_validated("api.hubapi.com", 443, "/p", "POST", b"{}", AUTH, 5, 1024)
    assert FakeConn.instances == []


def test_child_side_method_check(fake_net):
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf._request_validated("api.hubapi.com", 443, "/p", "DELETE", b"", AUTH, 5, 1024)
    assert fake_net.instances == []


# ---------------------------------------------------------------- native layer: parsing and scrubbing


def _ctx(responses):
    from webinars import native_integrations as ni

    calls = []

    def request(method, url, data, headers):
        calls.append((method, url))
        r = responses.pop(0)
        return r

    return ni.NativeContext(post=request, request=request, renew=lambda: True), calls


HUBSPOT = {
    "native_provider": "hubspot",
    "native_region": "global",
    "native_access_token": "hs-token-789",
    "native_person_match": "custom_id",
    "native_processing_ack": "confirmed",
    "native_contact_id_property": "webinar_person_id",
    "native_participation_object_type": "2-123",
    "native_participation_id_property": "webinar_registration_id",
}


def _body():
    import copy

    from webinars import native_integrations as ni

    return copy.deepcopy(ni.SAMPLE_BODY)


PII = b'{"message":"contact grace@example.com already exists","token":"hs-token-789"}'


@pytest.mark.parametrize(
    "response,kind,error",
    [
        ((200, b"<html>not json", None), "retry", "contact: response_unparsable"),
        ((200, None, None), "retry", "contact: response_too_large"),
        ((200, b'{"results":[]}', None), "retry", "contact: response_missing_id"),
        ((302, b"", None), "retry", "contact: redirect_not_followed: http 302"),
        ((409, PII, None), "dead", "contact: http_status: 409"),
        ((401, PII, None), "dead", "contact: http_status: 401"),
    ],
)
def test_response_handling_scrubs_remote_payload(response, kind, error):
    from webinars import native_integrations as ni

    ctx, _calls = _ctx([response])
    out = ni.execute("hubspot", HUBSPOT, _body(), "registration.created", ctx)
    assert (out.kind, out.error) == (kind, error)
    assert "grace" not in out.error and "hs-token" not in out.error


def test_retry_after_carried_on_429_and_503():
    from webinars import native_integrations as ni

    for status in (429, 503):
        ctx, _ = _ctx([(status, PII, 120)])
        out = ni.execute("hubspot", HUBSPOT, _body(), "registration.created", ctx)
        assert (out.kind, out.retry_after, out.status) == ("retry", 120, status)


def test_ids_only_parsed_from_json():
    from webinars import native_integrations as ni

    # Non-scalar or nested ids are refused, not stringified into the next request.
    ctx, calls = _ctx([(200, b'{"results":[{"id":{"evil":"x"}}]}', None)])
    out = ni.execute("hubspot", HUBSPOT, _body(), "registration.created", ctx)
    assert out.error == "contact: response_missing_id" and len(calls) == 1


# ---------------------------------------------------------------- idempotency on retry (what is and isn't proven)


def _recording_ctx(statuses):
    from webinars import native_integrations as ni

    sent = []

    def request(method, url, data, headers):
        sent.append({"method": method, "url": url, "json": json.loads(data)})
        status = statuses.pop(0) if statuses else 200
        return status, json.dumps({"results": [{"id": "77"}], "data": {"id": {"record_id": "rec-1"}}}).encode(), None

    return ni.NativeContext(post=lambda url, data, headers: request("POST", url, data, headers), request=request, renew=lambda: True), sent


def test_hubspot_retry_reuses_participation_upsert_key():
    """A retry after a failure past the participation upsert targets the same record key.

    This proves only that *we* send an upsert keyed by the registration id, never a create;
    whether HubSpot then guarantees a single record depends on the property being a
    unique-value property, which must be verified in the portal (not assumed here).
    """
    from webinars import native_integrations as ni

    first_ctx, first = _recording_ctx([200, 200, 503])
    assert ni.execute("hubspot", HUBSPOT, _body(), "registration.created", first_ctx).kind == "retry"
    second_ctx, second = _recording_ctx([])
    assert ni.execute("hubspot", HUBSPOT, _body(), "registration.created", second_ctx).kind == "delivered"
    part = [c for c in first + second if "/2-123/batch/upsert" in c["url"]]
    assert len(part) == 2 and part[0]["json"] == part[1]["json"]
    [inp] = part[0]["json"]["inputs"]
    assert inp["idProperty"] == "webinar_registration_id" and inp["id"] == ni.SAMPLE_BODY["registration"]["id"]
    assert not any(c["url"].endswith("/crm/v3/objects/2-123") for c in first + second)  # no plain create


def test_attio_retry_asserts_same_records():
    from webinars import native_integrations as ni

    creds = {
        "native_provider": "attio",
        "native_region": "global",
        "native_access_token": "attio-tok",
        "native_person_match": "custom_id",
        "native_processing_ack": "confirmed",
        "native_person_match_attribute": "webinar_person_id",
        "native_participation_list": "webinar_participation",
    }
    first_ctx, first = _recording_ctx([200, 500])
    assert ni.execute("attio", creds, _body(), "registration.created", first_ctx).kind == "retry"
    second_ctx, second = _recording_ctx([])
    assert ni.execute("attio", creds, _body(), "registration.created", second_ctx).kind == "delivered"
    assert all(c["method"] == "PUT" for c in first + second)  # assert, never POST-create
    assert first[0] == second[0]


def test_customerio_event_is_not_claimed_idempotent():
    """Customer.io identify is keyed, but the event has no dedupe id we could verify: a retry resends it."""
    from webinars import native_integrations as ni

    creds = {"native_provider": "customerio", "native_region": "us", "native_site_id": "s1", "native_api_key": "k1"}
    first_ctx, first = _recording_ctx([200, 503])  # person ok, event failed
    assert ni.execute("customerio", creds, _body(), "registration.created", first_ctx).kind == "retry"
    second_ctx, second = _recording_ctx([])
    ni.execute("customerio", creds, _body(), "registration.created", second_ctx)
    events = [c for c in first + second if c["json"].get("action") == "event"]
    assert len(events) == 2 and "id" not in events[0]["json"]


# ---------------------------------------------------------------- delivery integration


@pytest.mark.django_db
def test_delivery_passes_vendor_hosts_and_honours_retry_after(monkeypatch, settings, occurrence):
    from django.db import transaction
    from django.utils import timezone

    import webinars.delivery as delivery
    from tests.conftest import make_destination, make_registration
    from webinars import native_integrations as ni
    from webinars.crypto import encrypt_secret
    from webinars.models import OutboxMessage
    from webinars.outbox import enqueue_event

    settings.WEBHOOK_HOST_ALLOWLIST = ["api.hubapi.com", "hooks.example.net"]
    seen = []

    def fake(method, url, body, headers, timeout, total, max_bytes, allowed_hosts=None):
        seen.append({"method": method, "url": url, "allowed": allowed_hosts, "max": max_bytes, "total": total})
        return 429, b'{"message":"slow down grace@example.com"}', 1800, False

    monkeypatch.setattr(ssrf, "request_json_bounded", fake)
    make_destination(credentials_encrypted=encrypt_secret(json.dumps(HUBSPOT)), url="https://api.hubapi.com")
    reg = make_registration(occurrence)
    with transaction.atomic():
        enqueue_event("registration.created", registration=reg, occurrence=occurrence)
    [msg] = delivery.claim_batch("w1")
    before = timezone.now()
    delivery.deliver(msg)
    msg.refresh_from_db()
    assert msg.status == OutboxMessage.Status.PENDING and msg.attempts == 1 and msg.last_status_code == 429
    assert msg.last_error == "contact: http_status: 429"
    assert msg.next_attempt_at >= before + datetime.timedelta(seconds=1790)
    [call] = seen
    assert call["method"] == "POST" and call["url"].startswith("https://api.hubapi.com/")
    assert call["allowed"] == frozenset({"api.hubapi.com"}) and call["max"] == ni.MAX_RESPONSE_BYTES
    assert 0 < call["total"] <= settings.OUTBOX_LEASE_SECONDS


@pytest.mark.django_db
def test_delivery_oversized_response_retried(monkeypatch, settings, occurrence):
    from django.db import transaction

    import webinars.delivery as delivery
    from tests.conftest import make_destination, make_registration
    from webinars.crypto import encrypt_secret
    from webinars.models import OutboxMessage
    from webinars.outbox import enqueue_event

    settings.WEBHOOK_HOST_ALLOWLIST = ["api.hubapi.com", "hooks.example.net"]
    monkeypatch.setattr(ssrf, "request_json_bounded", lambda *a, **k: (200, b"", None, True))
    make_destination(credentials_encrypted=encrypt_secret(json.dumps(HUBSPOT)), url="https://api.hubapi.com")
    reg = make_registration(occurrence)
    with transaction.atomic():
        enqueue_event("registration.created", registration=reg, occurrence=occurrence)
    [msg] = delivery.claim_batch("w1")
    delivery.deliver(msg)
    msg.refresh_from_db()
    assert msg.status == OutboxMessage.Status.PENDING and msg.last_error == "contact: response_too_large"


def test_backoff_without_retry_after_unchanged():
    import webinars.delivery as delivery

    class R:
        def uniform(self, a, b):
            return 1.0

    assert delivery.backoff_seconds(1, rng=R()) == 60


def _echo_child(conn, host, port, path, method, body, headers, timeout, max_bytes):
    # Spawned: must be importable. Echoes what reached the child so the parent can assert on it.
    conn.send(("ok", (200, json.dumps({"method": method, "body": bytes(body or b"").decode()}).encode(), None, False), ""))
    conn.close()


def test_get_lookup_sends_no_body(allow):
    """GET is allowed for read-back / dedupe lookups and never carries a body."""
    status, raw, _retry, truncated = ssrf.request_json_bounded("GET", URL, b'{"ignored":1}', {}, 10, 5, allowed_hosts=VENDORS, _target=_echo_child)
    assert (status, truncated) == (200, False)
    assert json.loads(raw) == {"method": "GET", "body": ""}
