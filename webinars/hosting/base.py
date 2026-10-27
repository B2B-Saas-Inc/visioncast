"""Hosting adapter capability model, transport seam and outcome classification.

Every adapter declares what it can do (``Capabilities``) and how sure we are of the vendor
contract (``contract``):

* ``documented-unverified``: built from the vendor's public documentation, not yet run
  against a sandbox account (needs sandbox verification);
* ``unverified``: endpoint paths/fields are our best reading and are marked UNVERIFIED;
* ``none``: no API at all (manual links only, no outbound calls).

Unsupported actions are never simulated: the capability is ``False`` and the service
refuses the action with a clear message.

Outbound calls only go through ``webinars.ssrf.vendor_request_bounded`` with the adapter's
constant ``allowed_hosts`` (the URL must *also* be on the operator's WEBHOOK_HOST_ALLOWLIST).
Response bodies are parsed for a few ids/links only; they never reach errors or logs.
"""
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 10
TOTAL_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 65536
USER_AGENT = "webinar-platform-hosting/1"

_SECRET = re.compile(r"^[\x21-\x7e]{6,512}$")
SECRET_PATTERN = _SECRET
EXTERNAL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    help: str
    secret: bool = False
    required: bool = True
    pattern: object = None


@dataclass(frozen=True)
class Capabilities:
    create: bool = False
    update: bool = False
    attach: bool = False
    register_attendees: bool = False
    attendance_import: bool = False  # not implemented for any provider
    manual_link: bool = False

    def as_dict(self):
        return {
            "create": self.create,
            "update": self.update,
            "attach": self.attach,
            "registerAttendees": self.register_attendees,
            "attendanceImport": self.attendance_import,
            "manualLink": self.manual_link,
        }

    @property
    def provisioning(self):
        """True when any action makes an outbound call."""
        return self.create or self.update or self.attach or self.register_attendees


class Unsupported(Exception):
    """The adapter does not support this action (capability flag is false)."""


@dataclass
class Outcome:
    """Result of one provider call.

    ``kind``: ``succeeded`` | ``failed`` (definitive: nothing was created, safe to retry) |
    ``unknown`` (the request may have been processed; never blind-retry a create).
    """

    kind: str
    error: str = ""
    status_code: int = None
    external_id: str = ""
    join_url: str = ""
    host_url: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class Plan:
    """One planned request. ``body`` and ``headers`` never contain secrets (preview-safe)."""

    step: str
    method: str
    url: str
    body: dict = None
    form: dict = None  # x-www-form-urlencoded body (token requests); values redacted in previews
    headers: dict = field(default_factory=dict)

    def as_preview(self):
        out = {"step": self.step, "method": self.method, "url": self.url, "headers": dict(self.headers)}
        if self.body is not None:
            out["body"] = self.body
        if self.form is not None:
            out["form"] = {k: REDACTED for k in self.form}
        return out


REDACTED = "[redacted]"


def default_transport(method, url, body, headers, *, allowed_hosts):
    """The only outbound path. Looked up at call time so tests can monkeypatch ssrf."""
    from webinars import ssrf

    return ssrf.vendor_request_bounded(
        method,
        url,
        body,
        headers,
        TIMEOUT_SECONDS,
        TOTAL_TIMEOUT_SECONDS,
        allowed_hosts=allowed_hosts,
        max_response_bytes=MAX_RESPONSE_BYTES,
    )


class _Stop(Exception):
    def __init__(self, outcome):
        super().__init__(outcome.kind)
        self.outcome = outcome


def send(transport, allowed_hosts, step, method, url, *, json_body=None, form=None, headers=None, side_effect=True):
    """Perform one request and return the parsed JSON object (``{}`` for empty 2xx bodies).

    Raises ``_Stop`` with a sanitized ``Outcome``. ``side_effect`` says whether the request
    may have changed remote state (create/register): then anything that might have reached
    the vendor without a definitive answer is ``unknown``. Read-only/idempotent steps
    (token, GET, PATCH) map the same conditions to ``failed``/``unknown`` as noted.
    """
    from urllib.parse import urlencode

    from webinars import ssrf

    h = {"Accept": "application/json", "User-Agent": USER_AGENT}
    h.update(headers or {})
    if form is not None:
        body = urlencode(form).encode("utf-8")
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json; charset=utf-8"
    else:
        body = b""
    maybe_sent = "unknown" if side_effect else "failed"
    try:
        status, raw, _retry_after, truncated = transport(method, url, body, h, allowed_hosts=allowed_hosts)
    except ssrf.ResolutionFailed:
        # DNS failed before any connection: nothing was sent.
        raise _Stop(Outcome("failed", f"{step}: dns_resolution_failed")) from None
    except ssrf.UnsafeDestination:
        raise _Stop(
            Outcome("failed", f"{step}: destination not allowed (ask your operator to add the provider host to WEBHOOK_HOST_ALLOWLIST)")
        ) from None
    except (TimeoutError, OSError) as exc:  # DeadlineExceeded, socket.timeout and ssl.SSLError included
        kind = "timeout" if isinstance(exc, TimeoutError) else f"transport_error: {type(exc).__name__}"
        raise _Stop(Outcome(maybe_sent, f"{step}: {kind}")) from None
    except Exception as exc:  # http.client.HTTPException etc.: never echo text
        raise _Stop(Outcome(maybe_sent, f"{step}: transport_error: {type(exc).__name__}")) from None
    status = int(status)
    if 200 <= status < 300:
        if truncated:
            raise _Stop(Outcome(maybe_sent, f"{step}: response_too_large", status))
        if not raw:
            return {}
        try:
            data = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _Stop(Outcome(maybe_sent, f"{step}: response_unparsable", status)) from None
        if not isinstance(data, dict):
            raise _Stop(Outcome(maybe_sent, f"{step}: response_unexpected", status))
        return data
    if 300 <= status < 400:
        raise _Stop(Outcome("failed", f"{step}: redirect_not_followed: http {status}", status))
    if status in (401, 403):
        raise _Stop(Outcome("failed", f"{step}: http {status}: check the provider credentials and scopes", status))
    if status in (408, 425, 429) or status >= 500:
        raise _Stop(Outcome(maybe_sent, f"{step}: http {status}", status))
    raise _Stop(Outcome("failed", f"{step}: http {status}", status))


def scalar_id(data, *keys):
    """First scalar (str/int, not bool) id under ``keys`` in ``data`` or ``data['data']``."""
    for container in (data, data.get("data") if isinstance(data, dict) else None):
        if not isinstance(container, dict):
            continue
        for k in keys:
            v = container.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (str, int)) and str(v) and EXTERNAL_ID.match(str(v)):
                return str(v)
    return ""


def https_link(value, host_suffixes):
    """``value`` if it is an https URL whose host is one of / under ``host_suffixes``; else ''."""
    if not isinstance(value, str) or len(value) > 1000 or any(c in value for c in "\r\n\t "):
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or parts.username or parts.password or not host:
        return ""
    if not any(host == s or host.endswith("." + s) for s in host_suffixes):
        return ""
    return value


# ---------------------------------------------------------------------------
# OAuth access tokens: in-memory per process only (never stored in the database)
# ---------------------------------------------------------------------------

_TOKENS = {}
TOKEN_SAFETY_SECONDS = 120


def _token_key(provider, secrets):
    import hashlib
    import hmac

    from django.conf import settings

    material = json.dumps([provider, sorted((secrets or {}).items())], separators=(",", ":"))
    return hmac.new(settings.SECRET_KEY.encode(), material.encode(), hashlib.sha256).hexdigest()


def cached_token(provider, secrets):
    import time

    entry = _TOKENS.get(_token_key(provider, secrets))
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


def store_token(provider, secrets, token, expires_in):
    import time

    try:
        ttl = int(expires_in)
    except (TypeError, ValueError):
        ttl = 0
    ttl = max(0, min(ttl, 3600) - TOKEN_SAFETY_SECONDS)
    if ttl > 0:
        _TOKENS[_token_key(provider, secrets)] = (token, time.monotonic() + ttl)


def forget_token(provider, secrets):
    """Drop one cached token (e.g. after the vendor answered 401 to it)."""
    _TOKENS.pop(_token_key(provider, secrets), None)


def clear_token_cache():
    _TOKENS.clear()


def utc_z(dt):
    from datetime import timezone as dt_timezone

    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Adapter:
    provider = ""
    label = ""
    capabilities = Capabilities()
    contract = "none"
    allowed_hosts = frozenset()
    credential_fields = ()
    config_fields = ()
    notes = ()  # human-readable caveats (sandbox verification etc.)
    link_hosts = ()  # host suffixes accepted for attendee/host links in responses

    # ---- description / validation (no network)

    def describe(self):
        return {
            "provider": self.provider,
            "label": self.label,
            "capabilities": self.capabilities.as_dict(),
            "provisioning": self.capabilities.provisioning,
            "contract": self.contract,
            "allowedHosts": sorted(self.allowed_hosts),
            "credentialFields": [{"name": f.name, "label": f.label, "help": f.help, "required": f.required} for f in self.credential_fields],
            "configFields": [{"name": f.name, "label": f.label, "help": f.help, "required": f.required} for f in self.config_fields],
            "notes": list(self.notes),
        }

    def validate_config(self, config):
        errors = {}
        config = config or {}
        for key in config:
            if key not in {f.name for f in self.config_fields}:
                errors.setdefault(key, []).append("Unknown setting.")
        for f in self.config_fields:
            value = config.get(f.name, "")
            if not isinstance(value, str):
                errors.setdefault(f.name, []).append(f"{f.label} must be text.")
                continue
            if not value:
                if f.required:
                    errors.setdefault(f.name, []).append(f"{f.label} is required.")
                continue
            if f.pattern is not None and not f.pattern.match(value):
                errors.setdefault(f.name, []).append(f"{f.label} has an unexpected format.")
        return errors

    def validate_credentials(self, credentials):
        errors = {}
        for f in self.credential_fields:
            value = (credentials or {}).get(f.name, "")
            if not value:
                if f.required:
                    errors.setdefault(f.name, []).append(f"{f.label} is not set.")
                continue
            pattern = f.pattern or _SECRET
            if not pattern.match(value):
                errors.setdefault(f.name, []).append(f"{f.label} looks invalid.")
        return errors

    def allowlist_problems(self):
        """Hosts the operator has not allowlisted (WEBHOOK_HOST_ALLOWLIST). No DNS."""
        from webinars import ssrf

        missing = []
        for host in sorted(self.allowed_hosts):
            try:
                ssrf.validate_destination_url(f"https://{host}/")
            except ssrf.UnsafeDestination:
                missing.append(host)
        if missing:
            return [f"Ask your operator to add {', '.join(missing)} to WEBHOOK_HOST_ALLOWLIST before using {self.label}."]
        return []

    def occurrence_problems(self, occ, action):
        """Provider-specific checks on the occurrence (e.g. minimum duration)."""
        return []

    # ---- plans (pure, preview-safe) and calls

    def plan(self, action, occ, config, external_id=""):
        raise Unsupported(action)

    def run(self, action, occ, config, secrets, *, transport, external_id="", registration=None, account_key=""):
        raise Unsupported(action)

    def _send(self, transport, step, method, url, **kw):
        return send(transport, self.allowed_hosts, step, method, url, **kw)
