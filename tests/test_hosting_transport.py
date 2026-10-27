"""``ssrf.vendor_request_bounded`` (hosting/Stripe) and the unchanged POST/PUT-only policy
of ``ssrf.request_json_bounded``. No DNS or sockets: child targets are stand-ins.

Keep module-level imports light: spawned children import this module to find targets.
"""
import json
import multiprocessing

import pytest

from webinars import ssrf

URL = "https://api.zoom.us/v2/webinars/123"
PINNED = frozenset({"api.zoom.us", "zoom.us"})


@pytest.fixture
def allow(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["zoom.us", "hooks.example.net"]
    settings.WEBHOOK_ALLOWED_PORTS = [443]


@pytest.fixture
def no_spawn(monkeypatch):
    spawned = []
    monkeypatch.setattr(multiprocessing, "get_context", lambda *a: spawned.append(a) or None)
    return spawned


def _echo_child(conn, host, port, path, method, body, headers, timeout, max_bytes):
    conn.send(("ok", (200, json.dumps({"host": host, "path": path, "method": method, "body": body.decode()}).encode(), "", False), ""))
    conn.close()


@pytest.mark.parametrize("method", ["DELETE", "HEAD", "OPTIONS"])
def test_request_json_bounded_rejects_other_methods(allow, no_spawn, method):
    # GET (lookups), POST, PUT and PATCH are the native methods; nothing else reaches a child.
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.request_json_bounded(method, URL, b"{}", {}, 10, 5, allowed_hosts=PINNED)
    assert no_spawn == []


@pytest.mark.parametrize("method", ["DELETE", "HEAD", "OPTIONS", "TRACE", "get\r\nX: y"])
def test_vendor_rejects_other_methods(allow, no_spawn, method):
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.vendor_request_bounded(method, URL, b"", {}, 10, 5, allowed_hosts=PINNED)
    assert no_spawn == []


@pytest.mark.parametrize("allowed", [None, frozenset(), set()])
def test_vendor_requires_pinned_hosts(allow, no_spawn, allowed):
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.vendor_request_bounded("GET", URL, b"", {"Authorization": "Bearer x"}, 10, 5, allowed_hosts=allowed)
    assert no_spawn == []


def test_vendor_rejects_unpinned_host_even_if_allowlisted(allow, no_spawn):
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.vendor_request_bounded("POST", "https://hooks.example.net/x", b"{}", {"Authorization": "Bearer x"}, 10, 5,
                                    allowed_hosts=PINNED)
    assert no_spawn == []


def test_vendor_still_needs_operator_allowlist(settings, no_spawn):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.vendor_request_bounded("GET", URL, b"", {}, 10, 5, allowed_hosts=PINNED)
    assert no_spawn == []


@pytest.mark.parametrize("url", ["http://api.zoom.us/v2/x", "https://user@api.zoom.us/v2/x", "https://api.zoom.us:8443/v2/x"])
def test_vendor_url_policy(allow, no_spawn, url):
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.vendor_request_bounded("GET", url, b"", {}, 10, 5, allowed_hosts=PINNED)


def test_vendor_get_sends_no_body(allow):
    status, raw, _ra, truncated = ssrf.vendor_request_bounded(
        "get", URL + "?a=1", b"should-be-dropped", {}, 10, 20, allowed_hosts=PINNED, _target=_echo_child
    )
    data = json.loads(raw)
    assert status == 200 and not truncated
    assert data == {"host": "api.zoom.us", "path": "/v2/webinars/123?a=1", "method": "GET", "body": ""}


def test_vendor_patch_passes_body(allow):
    _s, raw, _ra, _t = ssrf.vendor_request_bounded("PATCH", URL, b'{"x":1}', {}, 10, 20, allowed_hosts=PINNED,
                                                   _target=_echo_child)
    assert json.loads(raw)["method"] == "PATCH" and json.loads(raw)["body"] == '{"x":1}'


def test_request_validated_default_methods_unchanged():
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf._request_validated("api.zoom.us", 443, "/", "DELETE", b"", {}, 1, 10)
