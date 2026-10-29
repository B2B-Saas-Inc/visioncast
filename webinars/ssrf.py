"""SSRF-safe outbound webhook delivery.

Rules (all must pass):

* ``https`` only, no userinfo, no fragment, port 443 or in ``WEBHOOK_ALLOWED_PORTS``.
* Hostname (lower-cased, IDNA-encoded, trailing dot removed) must equal or be a
  sub-domain of an entry in ``WEBHOOK_HOST_ALLOWLIST``. Empty allowlist rejects all.
* IP-literal hosts and ``localhost`` / ``.localhost`` / ``.local`` / ``.internal`` /
  ``.localdomain`` / ``.home.arpa`` are rejected even if allowlisted.
* Every address returned by DNS must be globally routable (no private, loopback,
  link-local/metadata, multicast, reserved, unspecified, CGNAT, ULA, documentation,
  benchmarking; IPv4-mapped / NAT64 / 6to4 / Teredo embedded IPv4 is checked too).
* The TCP connection is made to the vetted IP itself (no second DNS lookup, so DNS
  rebinding cannot swap the target) while TLS SNI, certificate verification and the
  ``Host`` header use the hostname.
* Redirects are never followed; the caller treats 3xx as failure.
* At most 2 KB of response body is read (64 KB for ``request_json_bounded``, the
  POST/PUT variant used by native vendor adapters).
"""
import http.client
import ipaddress
import re
import socket
import ssl
from urllib.parse import urlsplit

from django.conf import settings

MAX_RESPONSE_BYTES = 2048
SNIPPET_CHARS = 200

_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa", ".lan", ".intranet", ".corp")
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_NUMERICISH_RE = re.compile(r"^(0x[0-9a-f]+|[0-9]+)$")

_BLOCKED_V4 = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.31.196.0/24",
        "192.52.193.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "192.175.48.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
    )
]
_BLOCKED_V6 = [
    ipaddress.ip_network(n)
    for n in (
        "::/128",
        "::1/128",
        "::/96",  # deprecated IPv4-compatible
        "::ffff:0:0/96",  # IPv4-mapped (embedded address also checked)
        "64:ff9b:1::/48",  # local-use NAT64
        "100::/64",  # discard
        "2001:db8::/32",  # documentation
        "2001:10::/28",
        "fc00::/7",  # ULA, includes fd00:ec2::254 (AWS IMDS IPv6)
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
]
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class UnsafeDestination(Exception):
    """The destination URL or its resolved addresses are not allowed."""


class ResolutionFailed(UnsafeDestination):
    """DNS lookup failed (possibly transient). Subclass so callers that only
    catch UnsafeDestination still fail closed."""


def _setting_list(name, default):
    value = getattr(settings, name, default)
    if value is None:
        return list(default)
    if isinstance(value, (str, int)):
        value = [v for v in str(value).split(",") if v.strip()]
    return list(value)


def _normalize_host(host):
    if not host:
        raise UnsafeDestination("Missing host")
    host = host.strip().rstrip(".").lower()
    if not host:
        raise UnsafeDestination("Missing host")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise UnsafeDestination("Invalid hostname") from None
    return ascii_host.lower()


def _allowlist():
    entries = []
    for raw in _setting_list("WEBHOOK_HOST_ALLOWLIST", []):
        raw = str(raw).strip()
        if raw.startswith("*."):
            raw = raw[2:]
        raw = raw.lstrip(".")
        if not raw:
            continue
        try:
            entries.append(_normalize_host(raw))
        except UnsafeDestination:
            continue
    return entries


def _allowed_ports():
    ports = set()
    for p in _setting_list("WEBHOOK_ALLOWED_PORTS", [443]):
        try:
            ports.add(int(p))
        except (TypeError, ValueError):
            continue
    ports.add(443)
    return ports


def _is_ip_literal(host):
    candidate = host.strip("[]").split("%", 1)[0]
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        pass
    # Shorthand / numeric forms that some resolvers accept (e.g. "2130706433", "0x7f.1", "127.1").
    labels = candidate.split(".")
    return all(_NUMERICISH_RE.match(label) for label in labels if label)


def _pinned_ok(host, pinned_hosts):
    """A host pinned in code (native vendor host) passes without an operator allowlist entry,
    unless the operator set NATIVE_VENDOR_HOSTS_IMPLICIT=0."""
    if not pinned_hosts or not getattr(settings, "NATIVE_VENDOR_HOSTS_IMPLICIT", True):
        return False
    pinned = set()
    for h in pinned_hosts:
        try:
            pinned.add(_normalize_host(str(h)))
        except UnsafeDestination:
            continue
    return host in pinned


def validate_destination_url(url: str, *, pinned_hosts=()) -> None:
    if not isinstance(url, str) or not url or len(url) > 2000:
        raise UnsafeDestination("Invalid URL")
    if any(c in url for c in "\r\n\t ") or any(ord(c) < 0x20 for c in url):
        raise UnsafeDestination("URL contains whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeDestination("Invalid URL") from None
    if parts.scheme.lower() != "https":
        raise UnsafeDestination("Only https URLs are allowed")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise UnsafeDestination("Userinfo in URL is not allowed")
    if parts.fragment or url.endswith("#"):
        raise UnsafeDestination("Fragments are not allowed")
    if "\\" in url:
        raise UnsafeDestination("Backslashes are not allowed")
    port = port or 443
    if port not in _allowed_ports():
        raise UnsafeDestination("Port not allowed")
    raw_host = parts.hostname or ""
    if not raw_host or raw_host.startswith("[") or ":" in raw_host or _is_ip_literal(raw_host):
        raise UnsafeDestination("IP-literal hosts are not allowed")
    host = _normalize_host(raw_host)
    if _is_ip_literal(host):
        raise UnsafeDestination("IP-literal hosts are not allowed")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES) or "." not in host:
        raise UnsafeDestination("Internal hostnames are not allowed")
    if not all(_LABEL_RE.match(label) for label in host.split(".")):
        raise UnsafeDestination("Invalid hostname")
    allow = _allowlist()
    if not any(host == entry or host.endswith("." + entry) for entry in allow) and not _pinned_ok(host, pinned_hosts):
        raise UnsafeDestination("Host is not on WEBHOOK_HOST_ALLOWLIST")


def _embedded_ipv4(ip):
    """Return IPv4 addresses embedded in an IPv6 address (mapped, NAT64, 6to4, Teredo)."""
    found = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip in _NAT64:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip.teredo is not None:
        found.extend(ip.teredo)  # (server, client)
    return found


def is_public_address(value) -> bool:
    try:
        ip = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return False
    if ip.version == 4:
        return not any(ip in net for net in _BLOCKED_V4)
    if getattr(ip, "is_site_local", False):
        return False
    if any(ip in net for net in _BLOCKED_V6):
        return False
    for inner in _embedded_ipv4(ip):
        if not is_public_address(inner):
            return False
    # 6to4 / Teredo are relay mechanisms we never need for webhook receivers.
    if ip.sixtofour is not None or ip.teredo is not None:
        return False
    return True


def resolve_safe_addresses(host: str, port: int) -> list:
    host = _normalize_host(host)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        raise ResolutionFailed("DNS resolution failed") from None
    addresses = []
    for family, _type, _proto, _canon, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise UnsafeDestination("Unexpected address family")
        addr = str(sockaddr[0]).split("%", 1)[0]
        if addr not in addresses:
            addresses.append(addr)
    if not addresses:
        raise UnsafeDestination("Host resolved to no addresses")
    for addr in addresses:
        if not is_public_address(addr):
            # Do not echo the address: it may disclose internal topology in logs/admin.
            raise UnsafeDestination("Host resolves to a non-public address")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection to a pre-vetted IP with SNI/cert verification against ``host``."""

    def __init__(self, host, *, pinned_ip, port, timeout, context):
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip
        self._tls_context = context

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _tls_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def sanitize_snippet(raw: bytes) -> str:
    text = raw[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace")
    text = "".join(c if (c.isprintable() and ord(c) < 0x7F) else " " for c in text)
    return " ".join(text.split())[:SNIPPET_CHARS]


def post_json_pinned(url, body: bytes, headers: dict, timeout: float):
    """POST ``body`` to ``url`` over a pinned, verified TLS connection.

    Returns ``(status, snippet)``. Raises ``UnsafeDestination`` for policy
    violations and ``OSError`` / ``ssl.SSLError`` / ``http.client.HTTPException``
    for transport failures. Redirects are returned as-is (never followed).
    """
    return _post_validated(*_validated_target(url), body, headers, timeout)


def _validated_target(url, pinned_hosts=()):
    """Policy checks that need Django settings; returns ``(host, port, path)``."""
    validate_destination_url(url, pinned_hosts=pinned_hosts)
    parts = urlsplit(url)
    host = _normalize_host(parts.hostname)
    port = parts.port or 443
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return host, port, path


def _post_validated(host, port, path, body, headers, timeout):
    """DNS vetting + pinned TLS POST. Uses no Django settings, so it can run in a child."""
    addresses = resolve_safe_addresses(host, port)
    clean_headers = {k: v for k, v in (headers or {}).items() if k.lower() not in ("host", "content-length", "connection")}
    clean_headers["Connection"] = "close"
    if isinstance(body, str):
        body = body.encode("utf-8")

    conn = _PinnedHTTPSConnection(host, pinned_ip=addresses[0], port=port, timeout=timeout, context=_tls_context())
    try:
        # http.client adds ``Host: <hostname>[:port]`` from ``host`` automatically.
        conn.request("POST", path, body=body, headers=clean_headers)
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read(MAX_RESPONSE_BYTES)
        return status, sanitize_snippet(raw)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Hard total deadline (DNS + connect + TLS + request + response)
# ---------------------------------------------------------------------------
#
# Socket timeouts are per operation and ``getaddrinfo`` has none, so a slow resolver or
# a receiver that drips headers can hold a send far past the delivery lease. The
# network part therefore runs in a spawned child process that is terminated (then
# killed) at the deadline. Threads are not used: a timed-out thread would keep the
# network operation alive. Spawn (not fork) so the child never inherits DB sockets.

TERMINATE_GRACE_SECONDS = 2.0


class DeadlineExceeded(TimeoutError):
    """The whole send did not finish within ``total_timeout``; the child was killed."""


def _bounded_child(conn, host, port, path, body, headers, timeout):
    try:
        status, snippet = _post_validated(host, port, path, body, headers, timeout)
        result = ("ok", status, snippet)
    except ResolutionFailed:
        result = ("resolution_failed", "", "")
    except UnsafeDestination as exc:
        result = ("unsafe", str(exc)[:200], "")
    except (socket.timeout, TimeoutError):
        result = ("timeout", "", "")
    except ssl.SSLError as exc:
        result = ("tls", type(exc).__name__, "")
    except http.client.HTTPException as exc:
        result = ("http", type(exc).__name__, "")
    except OSError as exc:
        result = ("os", type(exc).__name__, "")
    except Exception as exc:  # never send exception text (may contain PII)
        result = ("os", type(exc).__name__, "")
    try:
        conn.send(result)
    finally:
        conn.close()


def _raise_child_result(result):
    kind, detail, snippet = result
    if kind == "ok":
        return detail, snippet
    if kind == "resolution_failed":
        raise ResolutionFailed("DNS resolution failed")
    if kind == "unsafe":
        raise UnsafeDestination(detail)
    if kind == "timeout":
        raise TimeoutError("timed out")
    if kind == "tls":
        raise ssl.SSLError(detail)
    if kind == "http":
        raise http.client.HTTPException(detail)
    raise OSError(detail)


def _stop(proc):
    if proc.is_alive():
        proc.terminate()
        proc.join(TERMINATE_GRACE_SECONDS)
    if proc.is_alive():
        proc.kill()
        proc.join(TERMINATE_GRACE_SECONDS)


# ---------------------------------------------------------------------------
# Method-aware bounded request for native vendor adapters (webinars/native_integrations.py)
# ---------------------------------------------------------------------------
#
# Same URL policy, DNS vetting, pinned TLS, no redirects and hard total deadline as
# ``post_json_bounded``. Differences: GET (no body; read-back and dedupe lookups), POST, PUT or
# PATCH only (the vendor contracts we call: Pipedrive v2 updates are PATCH), the
# response body is returned up to ``MAX_NATIVE_RESPONSE_BYTES`` (larger bodies are dropped
# and flagged), and ``Retry-After`` is returned parsed and capped. Requests carrying a
# credential header (``CREDENTIAL_HEADERS``) must name the exact hosts they may go to
# (``allowed_hosts``), so credentials can never follow a user-entered URL. Method and headers
# are constants built by the adapters, not user input.

MAX_NATIVE_RESPONSE_BYTES = 65536
NATIVE_METHODS = frozenset({"GET", "POST", "PUT", "PATCH"})
# Request headers that carry vendor credentials (lower-case): Bearer/Basic auth, Brevo's
# ``api-key``, ActiveCampaign's ``api-token`` and Pipedrive's ``x-api-token``. Such a request
# must name its allowed hosts (``_check_native_request``).
CREDENTIAL_HEADERS = frozenset({"authorization", "api-key", "api-token", "x-api-token"})
MAX_RETRY_AFTER_SECONDS = 3600
_RETRY_AFTER_RAW_CHARS = 64


def parse_retry_after(value, now=None):
    """Seconds from a ``Retry-After`` value (delta-seconds or HTTP-date), capped; else None."""
    import datetime
    from email.utils import parsedate_to_datetime

    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _RETRY_AFTER_RAW_CHARS:
        return None
    if value.isdigit():
        return min(int(value), MAX_RETRY_AFTER_SECONDS)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    seconds = int((when - now).total_seconds())
    return max(0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def _check_native_request(method, host, headers, allowed_hosts):
    if method not in NATIVE_METHODS:
        raise UnsafeDestination("Method not allowed")
    if allowed_hosts is not None:
        if host not in {_normalize_host(h) for h in allowed_hosts}:
            raise UnsafeDestination("Host is not an allowed vendor host")
    elif any(k.lower() in CREDENTIAL_HEADERS for k in (headers or {})):
        raise UnsafeDestination("Credentialed requests need an explicit vendor host")


def _request_validated(host, port, path, method, body, headers, timeout, max_bytes, methods=NATIVE_METHODS):
    """DNS vetting + pinned TLS request. Returns ``(status, raw, retry_after_raw, truncated)``.

    Uses no Django settings, so it can run in a child.
    """
    if method not in methods:
        raise UnsafeDestination("Method not allowed")
    addresses = resolve_safe_addresses(host, port)
    clean_headers = {k: v for k, v in (headers or {}).items() if k.lower() not in ("host", "content-length", "connection")}
    clean_headers["Connection"] = "close"
    if isinstance(body, str):
        body = body.encode("utf-8")
    conn = _PinnedHTTPSConnection(host, pinned_ip=addresses[0], port=port, timeout=timeout, context=_tls_context())
    try:
        conn.request(method, path, body=body, headers=clean_headers)
        resp = conn.getresponse()
        raw = resp.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        if truncated:
            raw = b""  # never hand back a partial document
        retry_after = (resp.getheader("Retry-After") or "")[:_RETRY_AFTER_RAW_CHARS]
        return resp.status, raw, retry_after, truncated
    finally:
        conn.close()


def _bounded_request_child(conn, host, port, path, method, body, headers, timeout, max_bytes):
    try:
        result = ("ok", _request_validated(host, port, path, method, body, headers, timeout, max_bytes), "")
    except ResolutionFailed:
        result = ("resolution_failed", "", "")
    except UnsafeDestination as exc:
        result = ("unsafe", str(exc)[:200], "")
    except (socket.timeout, TimeoutError):
        result = ("timeout", "", "")
    except ssl.SSLError as exc:
        result = ("tls", type(exc).__name__, "")
    except http.client.HTTPException as exc:
        result = ("http", type(exc).__name__, "")
    except Exception as exc:  # never send exception text (may contain PII)
        result = ("os", type(exc).__name__, "")
    try:
        conn.send(result)
    finally:
        conn.close()


def request_json_bounded(
    method,
    url,
    body,
    headers,
    timeout,
    total_timeout,
    max_response_bytes=MAX_NATIVE_RESPONSE_BYTES,
    allowed_hosts=None,
    _target=None,
):
    """Bounded GET/POST/PUT returning ``(status, raw_body, retry_after_seconds, truncated)``.

    ``raw_body`` is at most ``max_response_bytes`` (capped at ``MAX_NATIVE_RESPONSE_BYTES``);
    when the response is larger it is ``b""`` and ``truncated`` is True. Redirects are
    returned, never followed. ``_target`` is a test seam with ``_bounded_request_child``'s
    signature.
    """
    import multiprocessing

    method = str(method).upper()
    host, port, path = _validated_target(url, pinned_hosts=allowed_hosts or ())
    _check_native_request(method, host, headers, allowed_hosts)
    if method == "GET":
        body = b""  # lookups never carry a body
    elif isinstance(body, str):
        body = body.encode("utf-8")
    max_bytes = max(1, min(int(max_response_bytes), MAX_NATIVE_RESPONSE_BYTES))
    total_timeout = max(0.1, float(total_timeout))
    per_op = max(0.1, min(float(timeout), total_timeout))
    ctx = multiprocessing.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_target or _bounded_request_child,
        args=(send_conn, host, port, path, method, body, dict(headers or {}), per_op, max_bytes),
        daemon=True,
    )
    try:
        proc.start()
        send_conn.close()
        if not recv_conn.poll(total_timeout):
            raise DeadlineExceeded("native request exceeded total deadline")
        try:
            result = recv_conn.recv()
        except (EOFError, OSError):
            raise OSError("child_exited") from None
        if result[0] != "ok":
            _raise_child_result(result)
        status, raw, retry_after_raw, truncated = result[1]
        raw = bytes(raw or b"")
        if len(raw) > max_bytes:  # do not trust the child's bound
            raw, truncated = b"", True
        return int(status), raw, parse_retry_after(retry_after_raw), bool(truncated)
    finally:
        _stop(proc)
        proc.join(TERMINATE_GRACE_SECONDS)
        recv_conn.close()
        try:
            proc.close()
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Vendor API request (hosting providers, Stripe): adds GET and PATCH
# ---------------------------------------------------------------------------
#
# ``request_json_bounded`` keeps its POST/PUT-only policy unchanged. Hosting and payment
# adapters also need GET (read back / reconcile) and PATCH (Zoom update). This variant
# always requires ``allowed_hosts`` (a constant pinned set of vendor hosts, never a URL
# typed by a user), sends no body with GET, never follows redirects (a 3xx is returned
# to the adapter, which treats it as failure, so credentials never follow a Location)
# and runs under the same hard total deadline in a spawned child.

VENDOR_METHODS = frozenset({"GET", "POST", "PUT", "PATCH"})


class TransportUnavailable(UnsafeDestination):
    """The request process could not be started: nothing reached the network."""


def _vendor_request_child(conn, host, port, path, method, body, headers, timeout, max_bytes):
    try:
        result = ("ok", _request_validated(host, port, path, method, body, headers, timeout, max_bytes, VENDOR_METHODS), "")
    except ResolutionFailed:
        result = ("resolution_failed", "", "")
    except UnsafeDestination as exc:
        result = ("unsafe", str(exc)[:200], "")
    except (socket.timeout, TimeoutError):
        result = ("timeout", "", "")
    except ssl.SSLError as exc:
        result = ("tls", type(exc).__name__, "")
    except http.client.HTTPException as exc:
        result = ("http", type(exc).__name__, "")
    except Exception as exc:  # never send exception text (may contain PII)
        result = ("os", type(exc).__name__, "")
    try:
        conn.send(result)
    finally:
        conn.close()


def vendor_request_bounded(
    method,
    url,
    body,
    headers,
    timeout,
    total_timeout,
    *,
    allowed_hosts,
    max_response_bytes=MAX_NATIVE_RESPONSE_BYTES,
    _target=None,
):
    """Bounded GET/POST/PUT/PATCH to a pinned vendor host.

    Returns ``(status, raw_body, retry_after_seconds, truncated)`` like
    ``request_json_bounded``. ``allowed_hosts`` is mandatory and must be non-empty; the
    URL must still pass ``validate_destination_url`` (https, port, operator allowlist).
    """
    import multiprocessing

    method = str(method).upper()
    if method not in VENDOR_METHODS:
        raise UnsafeDestination("Method not allowed")
    if not allowed_hosts:
        raise UnsafeDestination("Vendor requests need an explicit vendor host")
    host, port, path = _validated_target(url, pinned_hosts=allowed_hosts or ())
    if host not in {_normalize_host(h) for h in allowed_hosts}:
        raise UnsafeDestination("Host is not an allowed vendor host")
    if method == "GET":
        body = b""
    elif isinstance(body, str):
        body = body.encode("utf-8")
    body = body or b""
    max_bytes = max(1, min(int(max_response_bytes), MAX_NATIVE_RESPONSE_BYTES))
    total_timeout = max(0.1, float(total_timeout))
    per_op = max(0.1, min(float(timeout), total_timeout))
    ctx = multiprocessing.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_target or _vendor_request_child,
        args=(send_conn, host, port, path, method, body, dict(headers or {}), per_op, max_bytes),
        daemon=True,
    )
    try:
        proc.start()
    except (AssertionError, OSError, RuntimeError):
        # E.g. a daemonic caller cannot have children. Nothing was sent, so this is a
        # definitive failure (callers map UnsafeDestination to "failed"), never "unknown".
        recv_conn.close()
        send_conn.close()
        raise TransportUnavailable("could not start the request process") from None
    try:
        send_conn.close()
        if not recv_conn.poll(total_timeout):
            raise DeadlineExceeded("vendor request exceeded total deadline")
        try:
            result = recv_conn.recv()
        except (EOFError, OSError):
            raise OSError("child_exited") from None
        if result[0] != "ok":
            _raise_child_result(result)
        status, raw, retry_after_raw, truncated = result[1]
        raw = bytes(raw or b"")
        if len(raw) > max_bytes:
            raw, truncated = b"", True
        return int(status), raw, parse_retry_after(retry_after_raw), bool(truncated)
    finally:
        _stop(proc)
        proc.join(TERMINATE_GRACE_SECONDS)
        recv_conn.close()
        try:
            proc.close()
        except ValueError:
            pass


def post_json_bounded(url, body: bytes, headers: dict, timeout: float, total_timeout: float, _target=None):
    """``post_json_pinned`` with a hard ``total_timeout`` covering DNS and the whole exchange.

    URL policy is checked in this process (it needs settings); DNS vetting, the pinned
    connection and the request run in a child that is terminated at the deadline, in which
    case ``DeadlineExceeded`` (a ``TimeoutError``) is raised. The worst-case wall time is
    ``total_timeout + 2 * TERMINATE_GRACE_SECONDS`` plus process start-up.
    ``_target`` is a test seam (a module-level function with ``_bounded_child``'s signature).
    """
    import multiprocessing

    host, port, path = _validated_target(url)
    if isinstance(body, str):
        body = body.encode("utf-8")
    total_timeout = max(0.1, float(total_timeout))
    per_op = max(0.1, min(float(timeout), total_timeout))
    ctx = multiprocessing.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_target or _bounded_child,
        args=(send_conn, host, port, path, body, dict(headers or {}), per_op),
        daemon=True,
    )
    try:
        proc.start()
        send_conn.close()  # the child holds its own copy; EOF if it dies without a result
        if not recv_conn.poll(total_timeout):
            raise DeadlineExceeded("webhook send exceeded total deadline")
        try:
            result = recv_conn.recv()
        except (EOFError, OSError):
            raise OSError("child_exited") from None
        return _raise_child_result(result)
    finally:
        _stop(proc)
        proc.join(TERMINATE_GRACE_SECONDS)
        recv_conn.close()
        try:
            proc.close()
        except ValueError:
            pass  # still running after kill (should not happen); leave it to the OS
