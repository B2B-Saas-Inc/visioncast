import socket

import pytest

from webinars.ssrf import UnsafeDestination, resolve_safe_addresses, validate_destination_url


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    settings.WEBHOOK_ALLOWED_PORTS = [443]


def test_allowlisted_https_ok():
    validate_destination_url("https://hooks.example.net/webhook")
    validate_destination_url("https://eu.hooks.example.net/webhook?x=1")


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.net/webhook",
        "ftp://hooks.example.net/",
        "https://evil.example.org/webhook",
        "https://hooks.example.net.evil.org/",
        "https://evilhooks.example.net/",
        "https://127.0.0.1/webhook",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/webhook",
        "https://2130706433/",
        "https://user:pass@hooks.example.net/webhook",
        "https://hooks.example.net@evil.org/",
        "https://hooks.example.net:8443/webhook",
        "https://localhost/webhook",
        "https://hooks.example.net/webhook#frag",
        "",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeDestination):
        validate_destination_url(url)


def test_empty_allowlist_rejects_everything(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = []
    with pytest.raises(UnsafeDestination):
        validate_destination_url("https://hooks.example.net/webhook")


def _fake_dns(monkeypatch, *addresses):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        out = []
        for a in addresses:
            if ":" in a:
                out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (a, port, 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)))
        return out

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "100.64.0.1",
        "0.0.0.0",
        "fd00:ec2::254",
        "fe80::1",
    ],
)
def test_private_resolution_rejected(monkeypatch, addr):
    _fake_dns(monkeypatch, addr)
    with pytest.raises(UnsafeDestination):
        resolve_safe_addresses("hooks.example.net", 443)


def test_public_resolution_ok(monkeypatch):
    _fake_dns(monkeypatch, "93.184.216.34")
    assert resolve_safe_addresses("hooks.example.net", 443) == ["93.184.216.34"]


def test_mixed_public_and_private_rejected(monkeypatch):
    _fake_dns(monkeypatch, "93.184.216.34", "10.0.0.1")
    with pytest.raises(UnsafeDestination):
        resolve_safe_addresses("hooks.example.net", 443)


def test_dns_failure_is_unsafe(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(UnsafeDestination):
        resolve_safe_addresses("hooks.example.net", 443)


def test_post_json_pinned_never_connects_to_private(monkeypatch):
    from webinars import ssrf

    _fake_dns(monkeypatch, "10.0.0.1")
    connected = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: connected.append(a) or (_ for _ in ()).throw(OSError()))
    with pytest.raises(UnsafeDestination):
        ssrf.post_json_pinned("https://hooks.example.net/webhook", b"{}", {}, 1.0)
    assert connected == []
