"""Native outbound adapters (PostHog, HubSpot, Customer.io, Attio) on the existing outbox.

Configuration lives in the destination's encrypted credentials (``Destination.credentials_encrypted``),
as flat ``native_*`` string keys; no model field is added. ``native_provider`` absent or empty
means the destination is a generic signed webhook, exactly as before.

Delivery (``webinars.delivery.deliver``) still does the claim/lease, erasure and consent checks
and records retry/dead/cancelled outcomes. This module only turns the re-filtered body
(``outbox.refilter_for_send``) into vendor requests through a ``NativeContext`` supplied by
``delivery``:

* ``post`` / ``request``: ``webinars.ssrf.request_json_bounded`` (POST/PUT only, pinned to
  ``VENDOR_HOSTS``, 64 KB response cap, parsed ``Retry-After``). Only ids are parsed out of
  response bodies; bodies never reach errors or logs. If that function is absent (older
  ssrf), HubSpot and Attio fail validation and are never sent.
* ``renew``: renews the lease before every call after the first; a lost lease stops the
  sequence without recording anything (the new owner will).

Record writes are upserts/asserts keyed by stable ids (platform person id, namespaced verified
organization id, registration id), never plain creates, so a retry of the whole sequence after
a partial failure targets the same records. That no duplicate results depends on vendor-side
uniqueness of the configured properties (unverified). Events are NOT idempotent: Customer.io
events can repeat on retry and PostHog uuid dedupe is unverified (see docs).

Identity rules: only ``identity.verified`` registrations are sent (an unknown browser using an
existing email must not write to trusted CRM data); organizations only from *verified*
external ids in the configured namespace, never from company names, and skipped when
ambiguous. Secrets are read only here, at send time, and never logged or returned in errors.
"""
import base64
import json
import re
import uuid
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from webinars.crypto import DecryptionError, decrypt_secret
from webinars.ssrf import ResolutionFailed, UnsafeDestination, validate_destination_url

PROVIDER_KEY = "native_provider"
NATIVE_PREFIX = "native_"
MAX_CALLS = 6
MAX_RESPONSE_BYTES = 65536
USER_AGENT = "webinar-platform-native/1"
SCHEMA_VERSION = "1"

# Person-linked events only; recording access has no real view evidence, so it is not offered.
NATIVE_EVENT_TYPES = ("registration.created", "registration.updated", "attendance.checked_in", "attendance.recorded")
REQUIRED_GROUPS = ("identity", "registration", "event")

# Strict per-vendor host allowlists. Credentials are only ever sent to these hosts, and the
# host must *also* be on the operator's WEBHOOK_HOST_ALLOWLIST (validated by ssrf).
HOSTS = {
    "posthog": {"us": "us.i.posthog.com", "eu": "eu.i.posthog.com"},
    "hubspot": {"global": "api.hubapi.com"},
    "customerio": {"us": "track.customer.io", "eu": "track-eu.customer.io"},
    "attio": {"global": "api.attio.com"},
}

VENDOR_HOSTS = frozenset(h for regions in HOSTS.values() for h in regions.values())

PROVIDER_CHOICES = [
    ("", "Generic signed webhook"),
    ("posthog", "PostHog"),
    ("hubspot", "HubSpot"),
    ("customerio", "Customer.io"),
    ("attio", "Attio"),
]
PROVIDER_LABELS = dict(PROVIDER_CHOICES)

_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_NAMESPACE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_HS_OBJECT = re.compile(r"^2-[0-9]{1,15}$")
_CIO_OBJECT = re.compile(r"^[0-9]{1,6}$")
_ATTIO_LIST = re.compile(r"^([a-z][a-z0-9_]{0,99}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_SECRET = re.compile(r"^[\x21-\x7e]{6,512}$")


@dataclass(frozen=True)
class Field:
    name: str  # credential key without the ``native_`` prefix
    label: str
    help: str
    secret: bool = False
    required: bool = True
    choices: tuple = ()
    pattern: object = None


ORG_NAMESPACE = Field(
    "org_namespace",
    "Organization ID namespace",
    "Which verified organization ID to use as the account key, e.g. crm_account_id. Only IDs set "
    "by a trusted server are used; company names are never used to match. Leave empty to skip organizations.",
    required=False,
    pattern=_NAMESPACE,
)

CONFIRMED = "confirmed"
# Operator statement, not a legal rule we infer: which consents (if any) gate delivery is the
# operator's "Required consent" choice; this only records that they made it deliberately.
PROCESSING_ACK = Field(
    "processing_ack",
    "Data processing confirmation",
    "Confirm you have a lawful basis to send registrants' data to this provider and that the "
    "Required consent settings reflect your policy. The platform does not decide this for you.",
    choices=(("", "Not confirmed"), (CONFIRMED, "Confirmed by the operator")),
)
PERSON_MATCH = Field(
    "person_match",
    "Match existing people by",
    "Email merges into existing contacts with the same email (needs the confirmation below). "
    "Custom ID only matches on our person ID property; an existing contact with the same email then fails "
    "the delivery (dead-lettered, nothing merged).",
    choices=(("email", "Email address (merge into existing contacts)"), ("custom_id", "Our person ID in a custom unique property")),
)
EMAIL_MERGE_ACK = Field(
    "email_merge_ack",
    "Email merge confirmation",
    "Required when matching by email: existing contacts with the registrant's email are updated. Only "
    "registrations whose identity the platform verified (browser capability or new person) are sent; this is "
    "not proof of mailbox ownership.",
    required=False,
    choices=(("", "Not confirmed"), (CONFIRMED, "Confirmed by the operator")),
)

FIELDS = {
    "posthog": (
        Field("region", "Region", "PostHog Cloud region of your project.", choices=(("us", "US Cloud"), ("eu", "EU Cloud"))),
        Field("api_key", "Project API key", "Server-side capture uses the project API key.", secret=True, pattern=_SECRET),
        Field(
            "group_type",
            "Group type",
            "PostHog group type for organizations, e.g. company. Must already exist in the project.",
            required=False,
            pattern=_SLUG,
        ),
        ORG_NAMESPACE,
        PROCESSING_ACK,
    ),
    "hubspot": (
        Field("region", "Region", "HubSpot API host.", choices=(("global", "api.hubapi.com"),)),
        Field("access_token", "Private app access token", "Needs CRM contacts, companies and custom-object write scopes.", secret=True, pattern=_SECRET),
        PERSON_MATCH,
        EMAIL_MERGE_ACK,
        Field("contact_id_property", "Contact ID property", "Unique-value contact property that stores our person ID, e.g. webinar_person_id. Required when matching by custom ID.", required=False, pattern=_SLUG),
        Field("company_id_property", "Company ID property", "Unique-value company property that stores the organization ID.", required=False, pattern=_SLUG),
        Field("participation_object_type", "Participation object type ID", "Custom object for webinar participation, e.g. 2-1234567.", pattern=_HS_OBJECT),
        Field("participation_id_property", "Participation ID property", "Unique-value property on that object that stores our registration ID.", pattern=_SLUG),
        ORG_NAMESPACE,
        PROCESSING_ACK,
    ),
    "customerio": (
        Field("region", "Region", "Customer.io account region.", choices=(("us", "US"), ("eu", "EU"))),
        Field("site_id", "Site ID", "Track API site ID.", secret=True, pattern=_SECRET),
        Field("api_key", "Track API key", "Track API key for the same workspace.", secret=True, pattern=_SECRET),
        Field("object_type_id", "Organization object type ID", "Customer.io object type ID for organizations, e.g. 1. Leave empty to skip.", required=False, pattern=_CIO_OBJECT),
        ORG_NAMESPACE,
        PROCESSING_ACK,
    ),
    "attio": (
        Field("region", "Region", "Attio API host.", choices=(("global", "api.attio.com"),)),
        Field("access_token", "Access token", "Workspace token with record and list-entry write access.", secret=True, pattern=_SECRET),
        PERSON_MATCH,
        EMAIL_MERGE_ACK,
        Field("person_match_attribute", "Person match attribute", "Unique attribute slug on People that stores our person ID. Required when matching by custom ID.", required=False, pattern=_SLUG),
        Field("company_match_attribute", "Company match attribute", "Unique attribute slug on Companies that stores the organization ID.", required=False, pattern=_SLUG),
        Field("participation_list", "Participation list", "List slug or ID; one entry per person with the webinar attributes below.", pattern=_ATTIO_LIST),
        ORG_NAMESPACE,
        PROCESSING_ACK,
    ),
}

# Per provider: the custom-ID field that custom_id matching needs.
CUSTOM_ID_FIELD = {"hubspot": "contact_id_property", "attio": "person_match_attribute"}

# Providers whose contract needs a response body (record ids) or PUT.
NEEDS_REQUEST_TRANSPORT = {"hubspot", "attio"}

EVENT_NAMES = {
    "registration.created": "webinar registered",
    "registration.updated": "webinar registration updated",
    "attendance.checked_in": "webinar checked in",
}
ATTENDANCE_EVENT_NAMES = {"attended": "webinar attended", "no_show": "webinar no-show"}


# ---------------------------------------------------------------------------
# Credentials / configuration
# ---------------------------------------------------------------------------


class CredentialsUnavailable(Exception):
    """Encrypted credentials exist but cannot be decrypted (key rotation, corruption)."""


def load_credentials(destination):
    """Decrypted ``{name: value}``. Raises ``CredentialsUnavailable``; never logs values."""
    if not destination.credentials_encrypted:
        return {}
    try:
        data = json.loads(decrypt_secret(destination.credentials_encrypted))
    except (DecryptionError, ValueError, TypeError):
        raise CredentialsUnavailable("credentials_unavailable") from None
    except Exception as exc:  # ImproperlyConfigured (no master key)
        raise CredentialsUnavailable("credentials_unavailable") from exc
    if not isinstance(data, dict):
        raise CredentialsUnavailable("credentials_unavailable")
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def provider_of(credentials):
    value = (credentials or {}).get(PROVIDER_KEY, "")
    return value if value in FIELDS else ""


def destination_provider(destination):
    """Provider for ``destination`` ('' = generic webhook). Raises ``CredentialsUnavailable``."""
    return provider_of(load_credentials(destination))


def config_of(credentials, provider):
    """Non-secret settings only (safe to render)."""
    return {f.name: credentials.get(NATIVE_PREFIX + f.name, "") for f in FIELDS.get(provider, ()) if not f.secret}


def secrets_set(credentials, provider):
    """``{field name: bool}`` for secret fields: set or not. Never the value."""
    return {f.name: bool(credentials.get(NATIVE_PREFIX + f.name)) for f in FIELDS.get(provider, ()) if f.secret}


def base_url(provider, credentials):
    host = HOSTS.get(provider, {}).get((credentials or {}).get(NATIVE_PREFIX + "region", ""))
    return f"https://{host}" if host else None


def merge_credentials(existing, provider, posted):
    """New credential dict for an explicit configuration submit.

    ``posted``: ``{field name: value}``. Blank secret fields keep the stored value (secrets
    are never re-rendered, so blank means "unchanged"). Generic (non ``native_``) credentials
    are kept. Changing provider drops the previous provider's ``native_*`` values; the
    returned notes say so. Returns ``(credentials, notes)``.
    """
    existing = dict(existing or {})
    notes = []
    previous = provider_of(existing)
    out = {k: v for k, v in existing.items() if not k.startswith(NATIVE_PREFIX)}
    if not provider:
        if previous:
            notes.append(f"{PROVIDER_LABELS[previous]} settings and secrets were removed; this is now a generic webhook.")
        return out, notes
    if provider not in FIELDS:
        raise ValueError("unknown provider")
    if previous and previous != provider:
        notes.append(f"{PROVIDER_LABELS[previous]} settings and secrets were removed.")
    out[PROVIDER_KEY] = provider
    for f in FIELDS[provider]:
        key = NATIVE_PREFIX + f.name
        value = (posted.get(f.name) or "").strip()
        if f.secret and not value and previous == provider and existing.get(key):
            out[key] = existing[key]
        elif value:
            out[key] = value
    return out, notes


def transport_supports_requests():
    import webinars.ssrf

    return callable(getattr(webinars.ssrf, "request_json_bounded", None))


def validate(provider, credentials, *, event_types=(), allowed_groups=(), required_consents=()):
    """``{field: [messages]}``; empty when the destination may be enabled. No network calls."""
    errors = {}

    def add(key, msg):
        errors.setdefault(key, []).append(msg)

    if not provider:
        return errors
    if provider not in FIELDS:
        add("provider", "Unknown provider.")
        return errors
    for f in FIELDS[provider]:
        value = (credentials or {}).get(NATIVE_PREFIX + f.name, "")
        if not value:
            if f.required:
                add(f"native-{f.name}", f"{f.label} is required.")
            continue
        if f.choices and value not in dict(f.choices):
            add(f"native-{f.name}", f"Choose a {f.label.lower()} from the list.")
        elif f.pattern is not None and not f.pattern.match(value):
            add(f"native-{f.name}", f"{f.label} has an unexpected format." if not f.secret else f"{f.label} looks invalid.")
    url = base_url(provider, credentials)
    if url:
        try:
            validate_destination_url(url)
        except UnsafeDestination:
            host = url.split("//", 1)[1]
            add("provider", f"Ask your operator to add {host} to WEBHOOK_HOST_ALLOWLIST before enabling.")
    cfg = config_of(credentials or {}, provider)
    org_targets = {"posthog": "group_type", "hubspot": "company_id_property", "customerio": "object_type_id", "attio": "company_match_attribute"}
    if bool(cfg.get("org_namespace")) != bool(cfg.get(org_targets[provider])):
        add("native-org_namespace", "Set both the organization ID namespace and the organization target, or neither.")
    bad_events = [e for e in (event_types or []) if e not in NATIVE_EVENT_TYPES]
    if bad_events:
        add("event_types", "Native integrations only support: " + ", ".join(NATIVE_EVENT_TYPES) + ".")
    groups = set(allowed_groups or [])
    missing = [g for g in REQUIRED_GROUPS if g not in groups]
    if missing:
        add("allowed_groups", "Native integrations need these data groups: " + ", ".join(missing) + ".")
    if provider == "posthog" and "analytics" not in (required_consents or []):
        # Product guardrail from the platform's own consent model (analytics purpose), not a CRM rule.
        add("required_consents", "PostHog is analytics: require Analytics consent.")
    if provider in CUSTOM_ID_FIELD:
        match = cfg.get("person_match")
        if match == "email" and cfg.get("email_merge_ack") != CONFIRMED:
            add("native-email_merge_ack", "Matching by email updates existing contacts: confirm this to enable.")
        if match == "custom_id" and not cfg.get(CUSTOM_ID_FIELD[provider]):
            add(f"native-{CUSTOM_ID_FIELD[provider]}", "Required when matching by custom ID.")
    if provider in NEEDS_REQUEST_TRANSPORT and not transport_supports_requests():
        add(
            "provider",
            f"{PROVIDER_LABELS[provider]} needs the response-reading transport (ssrf.request_json_bounded), which is missing; it can't be enabled.",
        )
    return errors


# ---------------------------------------------------------------------------
# Payload facts
# ---------------------------------------------------------------------------


class Skip(Exception):
    """The message must not be sent to this provider (recorded as cancelled)."""


def _facts(body, event_type, org_namespace):
    identity = body.get("identity") if isinstance(body.get("identity"), dict) else {}
    person_id = identity.get("personId")
    if not person_id:
        raise Skip("native_no_person")
    if identity.get("verified") is not True:
        raise Skip("native_identity_unverified")
    registration = body.get("registration") if isinstance(body.get("registration"), dict) else {}
    if registration.get("identityVerified") is False:
        raise Skip("native_identity_unverified")
    if not registration.get("id"):
        raise Skip("native_no_registration")
    profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    occ = event.get("occurrence") if isinstance(event.get("occurrence"), dict) else {}
    meta = body.get("delivery_metadata") if isinstance(body.get("delivery_metadata"), dict) else {}
    org_key = None
    if org_namespace and isinstance(body.get("organization"), dict):
        values = sorted(
            {
                str(o.get("value"))
                for o in body["organization"].get("externalIds") or []
                if isinstance(o, dict)
                and o.get("namespace") == org_namespace
                and o.get("verified") is True
                and o.get("source") == "trusted_api"
                and o.get("value")
            }
        )
        if len(values) == 1:  # ambiguous (several verified ids) -> no association rather than a guess
            org_key = values[0]
    properties = {
        "webinar_event_type": event_type,
        "webinar_event_id": event.get("id"),
        "webinar_event_title": event.get("title"),
        "webinar_occurrence_id": occ.get("id"),
        "webinar_starts_at": occ.get("startsAt"),
        "webinar_registration_id": registration.get("id"),
        "webinar_registration_status": registration.get("status"),
        "webinar_attendance": registration.get("attendance"),
        "webinar_schema_version": body.get("schemaVersion") or SCHEMA_VERSION,
        "webinar_delivery_id": meta.get("deliveryId"),
    }
    return {
        "person_id": str(person_id),
        "email": profile.get("email") or None,
        "first_name": profile.get("firstName") or None,
        "last_name": profile.get("lastName") or None,
        "job_title": profile.get("jobTitle") or None,
        "company_name": profile.get("company") or None,
        "has_profile": bool(profile),
        "org_key": org_key,
        "registration_id": str(registration["id"]),
        "attendance": registration.get("attendance"),
        "occurred_at": body.get("occurredAt"),
        "delivery_id": str(meta.get("deliveryId") or ""),
        "properties": properties,
    }


def _event_name(event_type, facts):
    if event_type == "attendance.recorded":
        name = ATTENDANCE_EVENT_NAMES.get(facts["attendance"])
        if not name:
            raise Skip("native_attendance_unknown")
        return name
    name = EVENT_NAMES.get(event_type)
    if not name:
        raise Skip("native_event_unsupported")
    return name


def unix_seconds(iso):
    """ISO-8601 UTC string (``outbox._iso_utc``) -> int Unix seconds, else None."""
    import datetime

    if not isinstance(iso, str) or not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def _stable_uuid(delivery_id, step):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"webinar-native:{delivery_id}:{step}"))


def _clean(d):
    return {k: v for k, v in d.items() if v not in (None, "")}


# ---------------------------------------------------------------------------
# Request plans (pure: used for delivery and for the redacted preview)
# ---------------------------------------------------------------------------


REDACTED = "[redacted]"


def plan_posthog(cfg, secrets, facts, event_type):
    """One ``POST /batch/``: $identify, $groupidentify, then the captured event."""
    group_type, org = cfg.get("group_type"), facts["org_key"]
    batch = []
    if facts["has_profile"]:
        batch.append(
            {
                "event": "$identify",
                "distinct_id": facts["person_id"],
                "uuid": _stable_uuid(facts["delivery_id"], "identify"),
                "timestamp": facts["occurred_at"],
                "properties": {
                    "$set": _clean(
                        {"email": facts["email"], "first_name": facts["first_name"], "last_name": facts["last_name"], "job_title": facts["job_title"]}
                    )
                },
            }
        )
    props = _clean(dict(facts["properties"]))
    if group_type and org:
        batch.append(
            {
                "event": "$groupidentify",
                "distinct_id": f"${group_type}_{org}",
                "uuid": _stable_uuid(facts["delivery_id"], "groupidentify"),
                "timestamp": facts["occurred_at"],
                "properties": {"$group_type": group_type, "$group_key": org, "$group_set": _clean({"name": facts["company_name"]})},
            }
        )
        props["$groups"] = {group_type: org}
    batch.append(
        {
            "event": _event_name(event_type, facts),
            "distinct_id": facts["person_id"],
            "uuid": _stable_uuid(facts["delivery_id"], "capture"),
            "timestamp": facts["occurred_at"],
            "properties": props,
        }
    )
    return [{"step": "batch", "method": "POST", "path": "/batch/", "json": {"api_key": secrets["api_key"], "batch": batch}, "auth": None}]


def plan_customerio(cfg, secrets, facts, event_type):
    """Track API v2 ``POST /api/v2/entity``: object identify, person identify (+relationship), event."""
    steps = []
    object_type, org = cfg.get("object_type_id"), facts["org_key"]
    person = {"type": "person", "identifiers": {"id": facts["person_id"]}, "action": "identify"}
    attrs = _clean({"email": facts["email"], "first_name": facts["first_name"], "last_name": facts["last_name"], "job_title": facts["job_title"]})
    if attrs:
        person["attributes"] = attrs
    if object_type and org:
        steps.append(
            {
                "step": "organization",
                "method": "POST",
                "path": "/api/v2/entity",
                "json": {
                    "type": "object",
                    "identifiers": {"object_type_id": object_type, "object_id": org},
                    "action": "identify",
                    "attributes": _clean({"name": facts["company_name"]}),
                },
            }
        )
        person["cio_relationships"] = [{"identifiers": {"object_type_id": object_type, "object_id": org}}]
    steps.append({"step": "person", "method": "POST", "path": "/api/v2/entity", "json": person})
    event = {
        "type": "person",
        "identifiers": {"id": facts["person_id"]},
        "action": "event",
        "name": _event_name(event_type, facts),
        "attributes": _clean(dict(facts["properties"])),
    }
    # Track v2 entity: timestamp is integer Unix seconds (docs.customer.io track_v2/entity, read 2026-10-23).
    ts = unix_seconds(facts["occurred_at"])
    if ts is not None:
        event["timestamp"] = ts
    steps.append({"step": "event", "method": "POST", "path": "/api/v2/entity", "json": event})
    token = base64.b64encode(f"{secrets['site_id']}:{secrets['api_key']}".encode()).decode()
    for s in steps:
        s["auth"] = f"Basic {token}"
    return steps


def _participation_props(facts):
    p = facts["properties"]
    return _clean(
        {
            "webinar_event_id": p["webinar_event_id"],
            "webinar_event_title": p["webinar_event_title"],
            "webinar_occurrence_id": p["webinar_occurrence_id"],
            "webinar_starts_at": p["webinar_starts_at"],
            "webinar_registration_status": p["webinar_registration_status"],
            "webinar_attendance": p["webinar_attendance"],
            "webinar_last_event_type": p["webinar_event_type"],
        }
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    kind: str  # delivered | retry | dead | cancelled | lost_lease
    error: str = ""
    status: int = None
    retry_after: int = None  # seconds, already capped by ssrf.parse_retry_after


class _Stop(Exception):
    def __init__(self, outcome):
        super().__init__(outcome.kind)
        self.outcome = outcome


class TransportUnavailable(Exception):
    """The method-aware transport (``ssrf.request_json_bounded``) is not installed."""


class NativeContext:
    """Bound to one claimed message by ``webinars.delivery``. Test seam: pass fakes."""

    def __init__(self, *, post, request, renew, check=None):
        self._post, self._request, self._renew, self._check = post, request, renew, check
        self.calls = 0

    def call(self, step, method, url, payload, headers, read):
        if self.calls >= MAX_CALLS:
            raise _Stop(Outcome("dead", "native_max_calls_exceeded"))
        if self.calls:
            if not self._renew():
                raise _Stop(Outcome("lost_lease"))
            # Re-check consent/erasure and the refreshed destination before every later step:
            # a withdrawal mid-sequence stops here. Writes already made are not recalled.
            stop = self._check() if self._check else None
            if stop is not None:
                raise _Stop(stop)
        self.calls += 1
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            if read or method != "POST":
                result = self._request(method, url, body, headers)
            else:
                result = self._post(url, body, headers)
            # (status, raw[, retry_after]); the legacy post seam returns (status, snippet).
            status = int(result[0])
            raw = result[1] if read else b""
            retry_after = result[2] if len(result) > 2 else None
        except TransportUnavailable:
            raise _Stop(Outcome("dead", "native_transport_unavailable")) from None
        except ResolutionFailed:
            raise _Stop(Outcome("retry", f"{step}: dns_resolution_failed")) from None
        except UnsafeDestination:
            raise _Stop(Outcome("dead", f"{step}: unsafe_destination")) from None
        except (TimeoutError, OSError) as exc:  # socket.timeout and ssl.SSLError are OSError subclasses
            kind = "timeout" if isinstance(exc, TimeoutError) else f"transport_error: {type(exc).__name__}"
            raise _Stop(Outcome("retry", f"{step}: {kind}")) from None
        except Exception as exc:  # http.client.HTTPException and anything else: no text (may echo PII)
            raise _Stop(Outcome("retry", f"{step}: transport_error: {type(exc).__name__}")) from None
        if 200 <= status < 300:
            if not read:
                return None
            if raw is None or len(raw) > MAX_RESPONSE_BYTES:
                raise _Stop(Outcome("retry", f"{step}: response_too_large", status))
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise _Stop(Outcome("retry", f"{step}: response_unparsable", status)) from None
        if 300 <= status < 400:
            raise _Stop(Outcome("retry", f"{step}: redirect_not_followed: http {status}", status))
        if status in (408, 425, 429) or status >= 500:
            raise _Stop(Outcome("retry", f"{step}: http_status: {status}", status, retry_after))
        # 4xx: auth, schema, conflict. Retrying the same request cannot succeed.
        raise _Stop(Outcome("dead", f"{step}: http_status: {status}", status))


def _headers(auth):
    h = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json", "User-Agent": USER_AGENT}
    if auth:
        h["Authorization"] = auth
    return h


def _first_id(data, *path):
    cur = data
    for p in path:
        if isinstance(cur, list):
            cur = cur[p] if isinstance(p, int) and len(cur) > p else None
        elif isinstance(cur, dict):
            cur = cur.get(p)
        else:
            cur = None
    return str(cur) if isinstance(cur, (str, int)) and str(cur) else None


def _person_match(cfg):
    """'email' or 'custom_id'. Checked by ``validate``; anything else stops before any write."""
    match = cfg.get("person_match")
    if match == "email" and cfg.get("email_merge_ack") == CONFIRMED:
        return "email"
    if match == "custom_id":
        return "custom_id"
    raise _Stop(Outcome("dead", "native_match_strategy_unconfirmed"))


def _require_match_inputs(provider, cfg, facts):
    if provider not in CUSTOM_ID_FIELD:
        return
    match = _person_match(cfg)
    if match == "email" and not facts["email"]:
        raise Skip("native_no_email")
    if match == "custom_id" and not cfg.get(CUSTOM_ID_FIELD[provider]):
        raise _Stop(Outcome("dead", "native_config_incomplete"))


def _run_hubspot(ctx, base, cfg, secrets, facts, event_type):
    auth = f"Bearer {secrets['access_token']}"
    cp, pp, obj = cfg.get("contact_id_property"), cfg["participation_id_property"], cfg["participation_object_type"]
    properties = {"email": facts["email"], "firstname": facts["first_name"], "lastname": facts["last_name"], "jobtitle": facts["job_title"]}
    if cp:
        properties[cp] = facts["person_id"]
    if _person_match(cfg) == "email":
        # Documented: contacts batch upsert accepts idProperty "email" (HubSpot CRM contacts v3).
        contact = {"idProperty": "email", "id": facts["email"], "properties": _clean(properties)}
    else:
        # Custom ID: an existing contact with the same email makes HubSpot reject the write (fails closed).
        contact = {"idProperty": cp, "id": facts["person_id"], "properties": _clean(properties)}
    data = ctx.call("contact", "POST", base + "/crm/v3/objects/contacts/batch/upsert", {"inputs": [contact]}, _headers(auth), True)
    contact_id = _first_id(data, "results", 0, "id")
    if not contact_id:
        raise _Stop(Outcome("retry", "contact: response_missing_id"))
    company_id = None
    if cfg.get("company_id_property") and facts["org_key"]:
        cpp = cfg["company_id_property"]
        company = {"idProperty": cpp, "id": facts["org_key"], "properties": _clean({cpp: facts["org_key"], "name": facts["company_name"]})}
        data = ctx.call("company", "POST", base + "/crm/v3/objects/companies/batch/upsert", {"inputs": [company]}, _headers(auth), True)
        company_id = _first_id(data, "results", 0, "id")
        if not company_id:
            raise _Stop(Outcome("retry", "company: response_missing_id"))
    part ={"idProperty": pp, "id": facts["registration_id"], "properties": {pp: facts["registration_id"], **_participation_props(facts)}}
    data = ctx.call("participation", "POST", base + f"/crm/v3/objects/{quote(obj)}/batch/upsert", {"inputs": [part]}, _headers(auth), True)
    part_id = _first_id(data, "results", 0, "id")
    if not part_id:
        raise _Stop(Outcome("retry", "participation: response_missing_id"))
    if company_id:
        ctx.call(
            "contact_company",
            "POST",
            base + "/crm/v4/associations/contacts/companies/batch/associate/default",
            {"inputs": [{"from": {"id": contact_id}, "to": {"id": company_id}}]},
            _headers(auth),
            True,
        )
    ctx.call(
        "contact_participation",
        "POST",
        base + f"/crm/v4/associations/contacts/{quote(obj)}/batch/associate/default",
        {"inputs": [{"from": {"id": contact_id}, "to": {"id": part_id}}]},
        _headers(auth),
        True,
    )


def _run_attio(ctx, base, cfg, secrets, facts, event_type):
    auth = _headers(f"Bearer {secrets['access_token']}")
    company_ref = None
    if cfg.get("company_match_attribute") and facts["org_key"]:
        ca = cfg["company_match_attribute"]
        values = _clean({ca: facts["org_key"], "name": facts["company_name"]})
        url = base + "/v2/objects/companies/records?" + urlencode({"matching_attribute": ca})
        data = ctx.call("company", "PUT", url, {"data": {"values": values}}, auth, True)
        cid = _first_id(data, "data", "id", "record_id")
        if not cid:
            raise _Stop(Outcome("retry", "company: response_missing_id"))
        company_ref = [{"target_object": "companies", "target_record_id": cid}]
    pa = cfg.get("person_match_attribute")
    values = {pa: facts["person_id"]} if pa else {}
    if facts["email"]:
        values["email_addresses"] = [facts["email"]]
    if company_ref:
        values["company"] = company_ref
    # Email mode asserts on Attio's email_addresses attribute; custom mode on our unique attribute.
    matching = "email_addresses" if _person_match(cfg) == "email" else pa
    url = base + "/v2/objects/people/records?" + urlencode({"matching_attribute": matching})
    data = ctx.call("person", "PUT", url, {"data": {"values": values}}, auth, True)
    pid = _first_id(data, "data", "id", "record_id")
    if not pid:
        raise _Stop(Outcome("retry", "person: response_missing_id"))
    entry = {"parent_record_id": pid, "parent_object": "people", "entry_values": _participation_props(facts) | {"webinar_registration_id": facts["registration_id"]}}
    ctx.call("participation", "PUT", base + f"/v2/lists/{quote(cfg['participation_list'])}/entries", {"data": entry}, auth, True)


def _run_simple(ctx, base, steps):
    for s in steps:
        ctx.call(s["step"], s["method"], base + s["path"], s["json"], _headers(s.get("auth")), False)


def execute(provider, credentials, body, event_type, ctx):
    """Run one provider delivery for an already re-filtered ``body``. Returns an ``Outcome``."""
    cfg = config_of(credentials, provider)
    secrets = {f.name: credentials.get(NATIVE_PREFIX + f.name, "") for f in FIELDS[provider] if f.secret}
    base = base_url(provider, credentials)
    if not base or not all(secrets.values()):
        return Outcome("dead", "native_config_incomplete")
    try:
        facts = _facts(body, event_type, cfg.get("org_namespace"))
        _require_match_inputs(provider, cfg, facts)
        if provider == "posthog":
            _run_simple(ctx, base, plan_posthog(cfg, secrets, facts, event_type))
        elif provider == "customerio":
            _run_simple(ctx, base, plan_customerio(cfg, secrets, facts, event_type))
        elif provider == "hubspot":
            _run_hubspot(ctx, base, cfg, secrets, facts, event_type)
        elif provider == "attio":
            _run_attio(ctx, base, cfg, secrets, facts, event_type)
        else:
            return Outcome("dead", "native_provider_unknown")
    except Skip as exc:
        return Outcome("cancelled", str(exc))
    except _Stop as stop:
        return stop.outcome
    finally:
        del secrets
    return Outcome("delivered")


def check_send_policy(provider, credentials, destination):
    """Send-time re-validation (config may have changed since enable). Returns a reason or None."""
    errors = validate(
        provider,
        credentials,
        event_types=destination.event_types,
        allowed_groups=destination.allowed_groups,
        required_consents=destination.required_consents,
    )
    if not errors:
        return None
    if "required_consents" in errors:
        return "native_consent_policy_invalid"
    if provider in NEEDS_REQUEST_TRANSPORT and not transport_supports_requests():
        return "native_transport_unavailable"
    return "native_config_invalid"


# ---------------------------------------------------------------------------
# Redacted preview (no network, no real person data)
# ---------------------------------------------------------------------------

SAMPLE_BODY = {
    "schemaVersion": SCHEMA_VERSION,
    "eventType": "registration.created",
    "occurredAt": "2026-01-01T12:00:00Z",
    "identity": {"personId": "00000000-0000-4000-8000-000000000001", "email": "person@example.com", "verified": True},
    "profile": {"email": "person@example.com", "firstName": "Sample", "lastName": "Person", "jobTitle": "Role", "company": "Example Co"},
    "organization": {"externalIds": []},
    "registration": {"id": "00000000-0000-4000-8000-000000000002", "status": "registered", "attendance": "unknown", "identityVerified": True},
    "event": {"id": "00000000-0000-4000-8000-000000000003", "title": "Sample webinar", "occurrence": {"id": "00000000-0000-4000-8000-000000000004", "startsAt": "2026-01-02T12:00:00Z"}},
    "delivery_metadata": {"deliveryId": "00000000-0000-4000-8000-000000000005"},
}


class _PreviewContext:
    """Records the planned calls instead of sending; returns fake ids for chained steps."""

    def __init__(self):
        self.calls = []

    def call(self, step, method, url, payload, headers, read):
        self.calls.append(
            {
                "step": step,
                "method": method,
                "url": url,
                "headers": {k: (REDACTED if k.lower() == "authorization" else v) for k, v in headers.items()},
                "body": _redact(payload),
            }
        )
        if not read:
            return None
        return {"results": [{"id": "<returned id>"}], "data": {"id": {"record_id": "<returned id>"}}}


def _redact(obj):
    if isinstance(obj, dict):
        return {k: (REDACTED if k == "api_key" else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def preview(provider, credentials, event_type="registration.created"):
    """Planned requests for a synthetic sample registration, secrets redacted. ``[]`` when not configured."""
    if provider not in FIELDS or not base_url(provider, credentials):
        return []
    cfg = config_of(credentials, provider)
    sample = json.loads(json.dumps(SAMPLE_BODY))
    if cfg.get("org_namespace"):
        sample["organization"]["externalIds"] = [{"namespace": cfg["org_namespace"], "value": "sample-org-id", "verified": True, "source": "trusted_api"}]
    secrets = {f.name: REDACTED for f in FIELDS[provider] if f.secret}
    ctx = _PreviewContext()
    base = base_url(provider, credentials)
    try:
        facts = _facts(sample, event_type, cfg.get("org_namespace"))
        if provider == "posthog":
            steps = plan_posthog(cfg, secrets, facts, event_type)
        elif provider == "customerio":
            steps = plan_customerio(cfg, secrets, facts, event_type)
        else:
            steps = None
        if steps is not None:
            for s in steps:
                ctx.call(s["step"], s["method"], base + s["path"], s["json"], _headers(s.get("auth")), False)
        elif provider == "hubspot" and cfg.get("participation_object_type") and cfg.get("participation_id_property"):
            _run_hubspot(ctx, base, cfg, {"access_token": REDACTED}, facts, event_type)
        elif provider == "attio" and cfg.get("participation_list"):
            _run_attio(ctx, base, cfg, {"access_token": REDACTED}, facts, event_type)
    except (Skip, _Stop, KeyError):
        return []
    for c in ctx.calls:
        c["body_json"] = json.dumps(c["body"], indent=2, sort_keys=True)
    return ctx.calls
