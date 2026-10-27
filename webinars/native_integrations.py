"""Native outbound adapters (PostHog, HubSpot, Customer.io, Attio, Segment, Amplitude, GA4, Mixpanel,
RudderStack, Brevo) on the existing outbox.

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
# Self-contained provider modules (``webinars/native_providers/*``), filled by ``_register``
# at the end of this file. Empty while this module is still importing.
REGISTRY = {}
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
    "segment": {"us": "api.segment.io", "eu": "events.eu1.segmentapis.com"},
    "amplitude": {"us": "api2.amplitude.com", "eu": "api.eu.amplitude.com"},
    # Only the documented global collection host; no regional GA host is assumed.
    "ga4": {"global": "www.google-analytics.com"},
    # Ingestion API residency hosts (unverified in this pass, see docs/native-integrations.md).
    "mixpanel": {"us": "api.mixpanel.com", "eu": "api-eu.mixpanel.com", "in": "api-in.mixpanel.com"},
    # The only host in the create-contact / create-event references (fetched 2026-10-25).
    "brevo": {"global": "api.brevo.com"},
}

# Static vendor hosts. RudderStack is not here: its data plane is a per-customer host (see
# ``dataplane_host_problem``), so it is pinned per destination by ``allowed_hosts``.
VENDOR_HOSTS = frozenset(h for regions in HOSTS.values() for h in regions.values())

PROVIDER_CHOICES = [
    ("", "Generic signed webhook"),
    ("posthog", "PostHog"),
    ("hubspot", "HubSpot"),
    ("customerio", "Customer.io"),
    ("attio", "Attio"),
    ("segment", "Segment"),
    ("amplitude", "Amplitude"),
    ("ga4", "Google Analytics 4"),
    ("mixpanel", "Mixpanel"),
    ("rudderstack", "RudderStack"),
    ("brevo", "Brevo"),
]
# RudderStack validates, but stays unsendable (``native_transport_unavailable``) until delivery
# pins each destination's own host via ``allowed_hosts`` and sets ``NATIVE_DESTINATION_HOSTS``.
PROVIDER_LABELS = dict(PROVIDER_CHOICES)

_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_NAMESPACE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_HS_OBJECT = re.compile(r"^2-[0-9]{1,15}$")
_CIO_OBJECT = re.compile(r"^[0-9]{1,6}$")
_ATTIO_LIST = re.compile(r"^([a-z][a-z0-9_]{0,99}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_SECRET = re.compile(r"^[\x21-\x7e]{6,512}$")
_GA4_MEASUREMENT = re.compile(r"^G-[A-Z0-9]{4,20}$")
# Query-string-safe secret (GA4 api_secret travels in the URL).
_URL_SECRET = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
# Basic-auth username: printable, no colon (it would split user and password).
_BASIC_USER = re.compile(r"^[\x21-\x39\x3b-\x7e]{6,512}$")
_MIXPANEL_PROJECT = re.compile(r"^[0-9]{1,20}$")
# Bare lowercase DNS name: no scheme, port, path, userinfo, wildcard, IP or trailing dot.
_HOSTNAME = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Brevo contact attributes: canonical profile field -> fact key. Nothing else (no custom fields,
# phone or free-form PII) can be mapped.
BREVO_ATTRIBUTE_SOURCES = {"firstName": "first_name", "lastName": "last_name", "company": "company_name", "jobTitle": "job_title"}
_BREVO_PAIR = r"(?:firstName|lastName|company|jobTitle)=[A-Z][A-Z0-9_]{0,49}"
_BREVO_MAPPING = re.compile(rf"^{_BREVO_PAIR}(?:,{_BREVO_PAIR}){{0,3}}$")
# Identifier/channel/blocklist names that must never be written as plain attributes.
BREVO_RESERVED_ATTRIBUTES = frozenset(
    {"EMAIL", "SMS", "WHATSAPP", "LANDLINE_NUMBER", "EXT_ID", "CONTACT_ID", "EMAIL_ID", "EMAILBLACKLISTED", "SMSBLACKLISTED"}
)


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
    "segment": (
        Field("region", "Region", "Segment workspace region.", choices=(("us", "US (api.segment.io)"), ("eu", "EU (events.eu1.segmentapis.com)"))),
        Field("write_key", "Source write key", "Write key of an HTTP API source. Profile traits are sent only when the Profile data group is allowed.", secret=True, pattern=_SECRET),
        PROCESSING_ACK,
    ),
    "amplitude": (
        Field("region", "Region", "Amplitude data center of your project.", choices=(("us", "US (api2.amplitude.com)"), ("eu", "EU (api.eu.amplitude.com)"))),
        Field("api_key", "Project API key", "HTTP V2 API key of the project. Only IDs and webinar properties are sent, no profile.", secret=True, pattern=_SECRET),
        PROCESSING_ACK,
    ),
    "ga4": (
        Field("region", "Collection host", "Measurement Protocol host.", choices=(("global", "www.google-analytics.com"),)),
        Field("measurement_id", "Measurement ID", "Web data stream measurement ID, e.g. G-ABC123XYZ.", pattern=_GA4_MEASUREMENT),
        Field(
            "api_secret",
            "Measurement Protocol API secret",
            "Created under the data stream. Sent in the request URL, so it is never shown in previews or errors.",
            secret=True,
            pattern=_URL_SECRET,
        ),
        PROCESSING_ACK,
    ),
    "mixpanel": (
        Field("region", "Data residency", "Mixpanel data residency of your project.", choices=(("us", "US (api.mixpanel.com)"), ("eu", "EU (api-eu.mixpanel.com)"), ("in", "India (api-in.mixpanel.com)"))),
        Field("project_id", "Project ID", "Numeric project ID (sent as project_id on /import).", pattern=_MIXPANEL_PROJECT),
        Field("service_account_username", "Service account username", "Service account with access to this project.", secret=True, pattern=_BASIC_USER),
        Field("service_account_secret", "Service account secret", "Only IDs and webinar properties are sent, no profile.", secret=True, pattern=_SECRET),
        PROCESSING_ACK,
    ),
    "rudderstack": (
        Field("region", "Data plane", "Your own data plane host below.", choices=(("dataplane", "Operator-allowlisted data plane"),)),
        Field(
            "dataplane_host",
            "Data plane host",
            "Bare host name only, e.g. example.dataplane.rudderstack.com. Your operator must list this exact host in WEBHOOK_HOST_ALLOWLIST.",
            pattern=_HOSTNAME,
        ),
        Field("write_key", "Source write key", "Write key of an HTTP source. Profile traits are sent only when the Profile data group is allowed.", secret=True, pattern=_SECRET),
        PROCESSING_ACK,
    ),
    "brevo": (
        Field("region", "API host", "Brevo API host.", choices=(("global", "api.brevo.com"),)),
        Field("api_key", "API key", "Brevo API v3 key, sent in the api-key header only to api.brevo.com.", secret=True, pattern=_SECRET),
        Field(
            "contact_sync",
            "Contacts",
            "Events are always keyed by the registrant's email. Updating contacts also writes the mapped attributes "
            "below (only when the Profile data group is allowed). Lists, subscriptions and blocklist flags are never changed.",
            choices=(("events", "Send events only"), ("upsert", "Create or update the contact by email, then send events")),
        ),
        Field(
            "email_merge_ack",
            "Email match confirmation",
            "Required: every Brevo call identifies the contact by email, so events attach to (and, when updating "
            "contacts, attributes overwrite) any existing contact with that email. Only registrations whose identity the "
            "platform verified (browser capability or new person) are sent; this is not proof of mailbox ownership.",
            choices=EMAIL_MERGE_ACK.choices,
        ),
        Field(
            "contact_attributes",
            "Contact attribute mapping",
            "Comma-separated pairs, e.g. firstName=FIRSTNAME,lastName=LASTNAME. Sources: firstName, lastName, company, "
            "jobTitle only. Targets must be existing uppercase Brevo contact attributes. Leave empty to send email only.",
            required=False,
            pattern=_BREVO_MAPPING,
        ),
        PROCESSING_ACK,
    ),
}

# Analytics tools: the platform's own analytics consent purpose must gate delivery.
ANALYTICS_PROVIDERS = {"posthog", "amplitude", "ga4", "mixpanel", "rudderstack"}
# Adapters that also refuse a message whose snapshot carries consents without an analytics grant
# (defence in depth on top of delivery's required-consent check).
ANALYTICS_GUARDED = {"mixpanel", "rudderstack"}
# Providers whose host is per-destination: delivery must pin ``allowed_hosts(...)``.
NEEDS_DESTINATION_HOSTS = {"rudderstack"}
# GA4 forbids PII: the snapshot must not carry the profile, and needs consents + attribution
# (the analytics-consent-gated GA client ID).
GA4_FORBIDDEN_GROUPS = ("profile",)
GA4_REQUIRED_GROUPS = ("consents", "attribution")

# Per provider: the custom-ID field that custom_id matching needs.
CUSTOM_ID_FIELD = {"hubspot": "contact_id_property", "attio": "person_match_attribute"}
# Per provider: the organization target setting that must be set together with ``org_namespace``.
ORG_TARGETS = {"posthog": "group_type", "hubspot": "company_id_property", "customerio": "object_type_id", "attio": "company_match_attribute"}

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


def _exact_allowlist_entries():
    """Operator WEBHOOK_HOST_ALLOWLIST entries written as plain hosts (wildcard/suffix entries excluded)."""
    from webinars.ssrf import _setting_list

    out = set()
    for raw in _setting_list("WEBHOOK_HOST_ALLOWLIST", []):
        entry = str(raw).strip().lower().rstrip(".")
        if entry and not entry.startswith(("*", ".")):
            out.add(entry)
    return out


def dataplane_host_problem(host):
    """Why ``host`` may not be a RudderStack data plane, else None. No DNS.

    The host is user-entered, so it must be a bare DNS name *and* an exact, non-wildcard entry of
    the operator allowlist: an operator entry like ``rudderstack.com`` or ``*.example.com`` does
    not admit arbitrary sub-domains here. Static vendor hosts are refused (a write key must not go
    to another vendor). ``validate_destination_url`` then applies the usual https/internal-name policy.
    """
    if not isinstance(host, str) or not _HOSTNAME.match(host):
        return "format"
    if host in VENDOR_HOSTS:
        return "vendor_host"
    if host not in _exact_allowlist_entries():
        return "not_allowlisted"
    try:
        validate_destination_url(f"https://{host}")
    except UnsafeDestination:
        return "unsafe"
    return None


# Generic name for provider modules with a per-destination host (same policy as RudderStack).
def destination_host_problem(host):
    return dataplane_host_problem(host)


def _host(provider, credentials):
    credentials = credentials or {}
    module = REGISTRY.get(provider)
    if module is not None and hasattr(module, "host_of"):
        # Per-destination host (operator-allowlisted, see ``destination_host_problem``) or None.
        host = module.host_of(credentials)
        return host if isinstance(host, str) and host else None
    if provider == "rudderstack":
        if credentials.get(NATIVE_PREFIX + "region") != "dataplane":
            return None
        host = credentials.get(NATIVE_PREFIX + "dataplane_host", "")
        return host if dataplane_host_problem(host) is None else None
    return HOSTS.get(provider, {}).get(credentials.get(NATIVE_PREFIX + "region", ""))


def base_url(provider, credentials):
    host = _host(provider, credentials)
    return f"https://{host}" if host else None


def allowed_hosts(provider, credentials):
    """The only host(s) this destination may send to: pass as ``allowed_hosts`` to the transport."""
    host = _host(provider, credentials)
    return frozenset({host}) if host else frozenset()


def destination_hosts_supported():
    """True once delivery pins ``allowed_hosts(provider, credentials)`` per destination."""
    import webinars.delivery

    return getattr(webinars.delivery, "NATIVE_DESTINATION_HOSTS", False) is True


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
    if provider == "rudderstack":
        host = (credentials or {}).get(NATIVE_PREFIX + "dataplane_host", "")
        problem = dataplane_host_problem(host) if host else None
        if problem == "format":
            if "native-dataplane_host" not in errors:
                add("native-dataplane_host", "Enter a bare lowercase host name: no https://, port, path or wildcard.")
        elif problem == "vendor_host":
            add("native-dataplane_host", "That host belongs to another provider; enter your RudderStack data plane host.")
        elif problem == "not_allowlisted":
            add("provider", f"Ask your operator to add exactly {host} (not a wildcard or parent domain) to WEBHOOK_HOST_ALLOWLIST before enabling.")
        elif problem == "unsafe":
            add("native-dataplane_host", "That host is not allowed as a destination.")
    cfg = config_of(credentials or {}, provider)
    if provider in ORG_TARGETS and bool(cfg.get("org_namespace")) != bool(cfg.get(ORG_TARGETS[provider])):
        add("native-org_namespace", "Set both the organization ID namespace and the organization target, or neither.")
    bad_events = [e for e in (event_types or []) if e not in NATIVE_EVENT_TYPES]
    if bad_events:
        add("event_types", "Native integrations only support: " + ", ".join(NATIVE_EVENT_TYPES) + ".")
    groups = set(allowed_groups or [])
    missing = [g for g in REQUIRED_GROUPS if g not in groups]
    if missing:
        add("allowed_groups", "Native integrations need these data groups: " + ", ".join(missing) + ".")
    if provider in ANALYTICS_PROVIDERS and "analytics" not in (required_consents or []):
        # Product guardrail from the platform's own consent model (analytics purpose), not a CRM rule.
        add("required_consents", f"{PROVIDER_LABELS[provider]} is analytics: require Analytics consent.")
    if provider == "ga4":
        forbidden = [g for g in GA4_FORBIDDEN_GROUPS if g in groups]
        if forbidden:
            add("allowed_groups", "Google Analytics must not receive personal data: remove the " + ", ".join(forbidden) + " group.")
        needed = [g for g in GA4_REQUIRED_GROUPS if g not in groups]
        if needed:
            add("allowed_groups", "Google Analytics needs these data groups for the consent-gated GA client ID: " + ", ".join(needed) + ".")
    if provider in CUSTOM_ID_FIELD:
        match = cfg.get("person_match")
        if match == "email" and cfg.get("email_merge_ack") != CONFIRMED:
            add("native-email_merge_ack", "Matching by email updates existing contacts: confirm this to enable.")
        if match == "custom_id" and not cfg.get(CUSTOM_ID_FIELD[provider]):
            add(f"native-{CUSTOM_ID_FIELD[provider]}", "Required when matching by custom ID.")
    if provider == "brevo":
        mapping = cfg.get("contact_attributes")
        if mapping and _BREVO_MAPPING.match(mapping) and brevo_attribute_map(mapping) is None:
            add("native-contact_attributes", "Map each source and each Brevo attribute once; identifier and blocklist attributes (EMAIL, SMS, EXT_ID…) can't be mapped.")
    if provider in NEEDS_REQUEST_TRANSPORT and not transport_supports_requests():
        add(
            "provider",
            f"{PROVIDER_LABELS[provider]} needs the response-reading transport (ssrf.request_json_bounded), which is missing; it can't be enabled.",
        )
    if provider in NEEDS_DESTINATION_HOSTS and not destination_hosts_supported():
        add("provider", f"{PROVIDER_LABELS[provider]} needs per-destination host pinning in delivery, which this installation lacks; it can't be enabled.")
    module = REGISTRY.get(provider)
    if module is not None and hasattr(module, "validate_extra"):
        module.validate_extra(cfg, credentials or {}, add, event_types=event_types, allowed_groups=allowed_groups, required_consents=required_consents)
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


def plan_segment(cfg, secrets, facts, event_type):
    """HTTP API ``POST /v1/identify`` (only with a profile) then ``POST /v1/track``.

    Basic auth ``writeKey:`` (empty password). ``userId`` = platform person id; ``messageId`` is
    deterministic per delivery and step, so a retry resends the same id (Segment dedupe on it
    is not verified here). Segment answers 200 for most malformed bodies, so 2xx is not proof
    the event was accepted.
    """
    steps = []
    traits = _clean({"email": facts["email"], "firstName": facts["first_name"], "lastName": facts["last_name"], "title": facts["job_title"]})
    if facts["has_profile"] and traits:
        steps.append(
            {
                "step": "identify",
                "method": "POST",
                "path": "/v1/identify",
                "json": _clean(
                    {"userId": facts["person_id"], "messageId": _stable_uuid(facts["delivery_id"], "segment-identify"), "timestamp": facts["occurred_at"], "traits": traits}
                ),
            }
        )
    steps.append(
        {
            "step": "track",
            "method": "POST",
            "path": "/v1/track",
            "json": _clean(
                {
                    "userId": facts["person_id"],
                    "event": _event_name(event_type, facts),
                    "properties": _clean(dict(facts["properties"])),
                    "messageId": _stable_uuid(facts["delivery_id"], "segment-track"),
                    "timestamp": facts["occurred_at"],
                }
            ),
        }
    )
    token = base64.b64encode(f"{secrets['write_key']}:".encode()).decode()
    for s in steps:
        s["auth"] = f"Basic {token}"
    return steps


def plan_amplitude(cfg, secrets, facts, event_type):
    """HTTP V2 ``POST /2/httpapi`` with ``api_key`` in the body and one event.

    No profile data. ``insert_id`` is deterministic per delivery; Amplitude documents dedupe on
    ``device_id`` + ``insert_id`` and we send no device id (none is captured; we do not invent
    one), so dedupe of a retried event is NOT assumed.
    """
    event = {
        "user_id": facts["person_id"],
        "event_type": _event_name(event_type, facts),
        "insert_id": _stable_uuid(facts["delivery_id"], "amplitude-event"),
        "event_properties": _clean(dict(facts["properties"])),
    }
    ts = unix_seconds(facts["occurred_at"])
    if ts is not None:
        event["time"] = ts * 1000  # milliseconds since epoch
    return [{"step": "event", "method": "POST", "path": "/2/httpapi", "json": {"api_key": secrets["api_key"], "events": [event]}, "auth": None}]


def _analytics_guard(body):
    """Skip when the snapshot carries consents but no explicit analytics grant.

    Without the consents group, delivery's required-consent check (``validate`` forces
    Analytics) is the gate; this never treats a missing record as a grant.
    """
    if "consents" in body and not _consent_granted_in(body, "analytics"):
        raise Skip("native_analytics_consent_missing")


def _require_delivery_id(facts):
    # Deterministic ids derive from the delivery id; without it every message would share one id.
    if not facts["delivery_id"]:
        raise _Stop(Outcome("dead", "native_delivery_id_missing"))


def _encode(payload):
    """The exact request body ``NativeContext.call`` sends (compact, UTF-8)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# Vendor payload limits from the primary docs fetched 2026-10-25 (docs/native-integrations.md).
# "KB"/"MB" are read as decimal, the stricter reading. Checked on the serialized body before any call.
MIXPANEL_MAX_EVENT_BYTES = 1_000_000  # each event < 1 MB
MIXPANEL_MAX_PROPERTIES = 254  # < 255 property keys
RUDDERSTACK_MAX_CALL_BYTES = 32_000  # max 32 KB per call
_SCALARS = (str, int, float, bool)


def _require_flat(step, mapping):
    # Only scalar values are sent: Mixpanel caps nesting/arrays and RudderStack's nesting limit
    # was not found in its docs, so nested or array values are refused rather than guessed at.
    if any(not isinstance(v, _SCALARS) for v in mapping.values()):
        raise _Stop(Outcome("dead", f"{step}: payload_unsupported"))


def _require_size(step, payload, max_bytes):
    if len(_encode(payload)) > max_bytes:
        raise _Stop(Outcome("dead", f"{step}: payload_too_large"))


def plan_mixpanel(cfg, secrets, facts, event_type):
    """Ingestion ``POST /import?strict=1&project_id=…`` with a one-event JSON array.

    Service-account Basic auth. ``distinct_id`` = platform person id; ``time`` in milliseconds
    from ``occurredAt``; ``$insert_id`` deterministic per delivery (uuid5: 36 chars, hex and
    hyphens), so a retry resends the same (event, time, distinct_id, $insert_id), Mixpanel's
    documented dedupe tuple; best effort, not exactly-once. ``ip`` = "0" so Mixpanel does not
    geolocate from our server's request IP. No profile data (no /engage call).
    """
    _require_delivery_id(facts)
    ts = unix_seconds(facts["occurred_at"])
    if ts is None:
        raise Skip("native_no_timestamp")  # /import needs a time; we do not invent one
    if not cfg.get("project_id"):
        raise _Stop(Outcome("dead", "native_config_incomplete"))
    props = _clean(dict(facts["properties"]))
    props.update(
        {"time": ts * 1000, "distinct_id": facts["person_id"], "$insert_id": _stable_uuid(facts["delivery_id"], "mixpanel-event"), "ip": 0}
    )
    _require_flat("import", props)
    if len(props) > MIXPANEL_MAX_PROPERTIES:
        raise _Stop(Outcome("dead", "import: payload_unsupported"))
    events = [{"event": _event_name(event_type, facts), "properties": props}]
    # The body is the one-event array: at most 2 bytes more than the event, so this is stricter.
    if len(_encode(events)) >= MIXPANEL_MAX_EVENT_BYTES:
        raise _Stop(Outcome("dead", "import: payload_too_large"))
    token = base64.b64encode(f"{secrets['service_account_username']}:{secrets['service_account_secret']}".encode()).decode()
    query = urlencode({"strict": "1", "project_id": cfg["project_id"]})
    return [{"step": "import", "method": "POST", "path": "/import?" + query, "json": events, "auth": f"Basic {token}"}]


def plan_rudderstack(cfg, secrets, facts, event_type):
    """HTTP API ``POST /v1/identify`` (only with a profile) then ``POST /v1/track``.

    Same Segment-compatible spec as ``plan_segment``: Basic ``writeKey:`` (the documented curl
    ``-u <write_key>:``), ``userId`` = person id, deterministic ``messageId``. RudderStack dedupe
    on ``messageId`` is not assumed. The timestamp is sent only if it parses as ISO-8601. Every
    step is checked against the per-call size limit before the first call, so an oversize track
    never follows a sent identify.
    """
    _require_delivery_id(facts)
    steps = plan_segment(cfg, {"write_key": secrets["write_key"]}, facts, event_type)
    for s in steps:
        s["json"]["messageId"] = _stable_uuid(facts["delivery_id"], f"rudderstack-{s['step']}")
        if unix_seconds(s["json"].get("timestamp")) is None:
            s["json"].pop("timestamp", None)
        for key in ("traits", "properties"):
            _require_flat(s["step"], s["json"].get(key) or {})
        _require_size(s["step"], s["json"], RUDDERSTACK_MAX_CALL_BYTES)
    return steps


# Brevo limits from the create-event reference fetched 2026-10-25 (decimal KB, the stricter reading).
BREVO_MAX_PROPERTIES_BYTES = 50_000  # event_properties: 50 KB
_BREVO_NAME = re.compile(r"^[A-Za-z0-9_-]{1,255}$")  # event_name and event_properties keys
# The approved, self-contained snapshot sent as event_properties: explicit (group, path) -> key.
# Only scalar lifecycle ids, statuses and timestamps. Never profile/consent/attribution data or
# any URL (joinUrl, waitingRoomUrl, managedRoomUrl, recordingUrl, heroImageUrl, redirectUrl), so
# no private link reaches Brevo whatever the destination's link setting. A group the destination
# may not receive is already absent from the re-filtered body, so its keys are simply not sent.
BREVO_EVENT_PATHS = (
    ("identity", ("personId",), "person_id"),
    ("registration", ("id",), "registration_id"),
    ("registration", ("status",), "registration_status"),
    ("registration", ("attendance",), "registration_attendance"),
    ("registration", ("formVersion",), "registration_form_version"),
    ("registration", ("createdAt",), "registration_created_at"),
    ("registration", ("updatedAt",), "registration_updated_at"),
    ("event", ("id",), "event_id"),
    ("event", ("slug",), "event_slug"),
    ("event", ("title",), "event_title"),
    ("event", ("revision",), "event_revision"),
    ("event", ("occurrence", "id"), "occurrence_id"),
    ("event", ("occurrence", "startsAt"), "occurrence_starts_at"),
    ("event", ("occurrence", "endsAt"), "occurrence_ends_at"),
    ("event", ("occurrence", "timezone"), "occurrence_timezone"),
    ("event", ("occurrence", "status"), "occurrence_status"),
    ("event", ("occurrence", "cancelledAt"), "occurrence_cancelled_at"),
    ("event", ("occurrence", "recordingPublishedAt"), "occurrence_recording_published_at"),
    ("delivery_metadata", ("deliveryId",), "delivery_id"),
    ("delivery_metadata", ("createdAt",), "delivery_created_at"),
)


def brevo_attribute_map(value):
    """``{fact key: BREVO_ATTRIBUTE}`` from ``firstName=FIRSTNAME,…``; ``{}`` when empty, None when invalid."""
    if not value:
        return {}
    if not _BREVO_MAPPING.match(value):
        return None
    pairs = [p.split("=", 1) for p in value.split(",")]
    sources, targets = [s for s, _ in pairs], [t for _, t in pairs]
    if len(set(sources)) != len(sources) or len(set(targets)) != len(targets) or set(targets) & BREVO_RESERVED_ATTRIBUTES:
        return None
    return {BREVO_ATTRIBUTE_SOURCES[s]: t for s, t in pairs}


def _brevo_event_properties(body, event_type):
    props = {"webinar_event_type": event_type, "occurred_at": body.get("occurredAt"), "schema_version": body.get("schemaVersion") or SCHEMA_VERSION}
    for group, path, key in BREVO_EVENT_PATHS:
        cur = body.get(group)
        for p in path:
            cur = cur.get(p) if isinstance(cur, dict) else None
        props[key] = cur
    return {k: v for k, v in props.items() if isinstance(v, _SCALARS) and v != ""}


def plan_brevo(cfg, secrets, facts, event_type, body):
    """``POST /v3/contacts`` (optional, email-keyed upsert) then ``POST /v3/events``. ``api-key`` header.

    Contact: only in ``upsert`` mode, with the email-match confirmation, when the profile group is
    present and has an email. Body is only ``email``, the explicitly mapped ``attributes`` and
    ``updateEnabled: true``; never ``ext_id`` (no cross-merge on our person id), ``listIds``,
    blocklist flags, SMS/WhatsApp or ``forceMerge``. Event: ``identifiers.email_id`` only,
    ``event_date`` from ``occurredAt`` when it parses, flat ``event_properties`` from
    ``BREVO_EVENT_PATHS`` including the delivery id (Brevo documents no idempotency key, so a
    retry can duplicate the event; consumers dedupe on ``delivery_id``). Every step is validated
    before the first call.
    """
    if cfg.get("email_merge_ack") != CONFIRMED or cfg.get("contact_sync") not in ("events", "upsert"):
        raise _Stop(Outcome("dead", "native_match_strategy_unconfirmed"))
    _require_delivery_id(facts)
    attribute_map = brevo_attribute_map(cfg.get("contact_attributes"))
    if attribute_map is None:
        raise _Stop(Outcome("dead", "native_config_invalid"))
    identity = body.get("identity") if isinstance(body.get("identity"), dict) else {}
    email = facts["email"] or identity.get("email")
    if not isinstance(email, str) or not email:
        raise Skip("native_no_email")
    _event_name(event_type, facts)  # unsupported events / unknown attendance are skipped like elsewhere
    name = "webinar_" + event_type.replace(".", "_")
    props = _brevo_event_properties(body, event_type)
    if not _BREVO_NAME.match(name) or not all(_BREVO_NAME.match(k) for k in props):
        raise _Stop(Outcome("dead", "event: payload_unsupported"))
    _require_size("event", props, BREVO_MAX_PROPERTIES_BYTES)
    headers = {"api-key": secrets["api_key"]}
    steps = []
    if cfg["contact_sync"] == "upsert" and facts["has_profile"] and facts["email"]:
        attributes = _clean({target: facts[source] for source, target in attribute_map.items()})
        _require_flat("contact", attributes)
        contact = {"email": facts["email"], "updateEnabled": True}
        if attributes:
            contact["attributes"] = attributes
        steps.append({"step": "contact", "method": "POST", "path": "/v3/contacts", "json": contact, "auth": None, "headers": headers})
    event = {"event_name": name, "identifiers": {"email_id": email}, "event_properties": props}
    if unix_seconds(facts["occurred_at"]) is not None:
        event["event_date"] = facts["occurred_at"]
    steps.append({"step": "event", "method": "POST", "path": "/v3/events", "json": event, "auth": None, "headers": headers})
    return steps


GA4_EVENT_NAMES = {
    "webinar registered": "webinar_registered",
    "webinar registration updated": "webinar_registration_updated",
    "webinar checked in": "webinar_checked_in",
    "webinar attended": "webinar_attended",
    "webinar no-show": "webinar_no_show",
}
# Only pseudonymous webinar ids and statuses; never person id, email, names or custom fields.
GA4_PARAMS = ("webinar_event_type", "webinar_event_id", "webinar_occurrence_id", "webinar_registration_status", "webinar_attendance")
GA4_PARAM_MAX = 100
# ``_ga`` cookie value ``GA1.<n>.<random>.<timestamp>`` -> client id ``<random>.<timestamp>``.
_GA_COOKIE = re.compile(r"^GA\d+\.\d+\.(\d{1,20}\.\d{1,20})$")
_GA_CLIENT_ID = re.compile(r"^\d{1,20}\.\d{1,20}$")


def ga_client_id(body):
    """The captured GA client id from the snapshot's attribution, else None (never invented).

    ``attribution.clean_attribution`` keeps ``gaClientId`` only with analytics consent.
    """
    attribution = body.get("attribution") if isinstance(body.get("attribution"), dict) else {}
    for touch in ("registrationTouch", "firstTouch"):
        t = attribution.get(touch) if isinstance(attribution.get(touch), dict) else {}
        ids = t.get("identifiers") if isinstance(t.get("identifiers"), dict) else {}
        value = ids.get("gaClientId")
        if not isinstance(value, str):
            continue
        m = _GA_COOKIE.match(value)
        if m:
            return m.group(1)
        if _GA_CLIENT_ID.match(value):
            return value
    return None


def _consent_granted_in(body, purpose):
    consents = body.get("consents") if isinstance(body.get("consents"), dict) else {}
    entry = consents.get(purpose)
    return isinstance(entry, dict) and entry.get("granted") is True


def ga4_inputs(body):
    """``(client_id, ads_granted)`` from the snapshot. Raises ``Skip`` without analytics consent or a client id."""
    if not _consent_granted_in(body, "analytics"):
        raise Skip("native_analytics_consent_missing")
    client_id = ga_client_id(body)
    if not client_id:
        raise Skip("native_ga_client_id_missing")
    return client_id, _consent_granted_in(body, "advertising")


def plan_ga4(cfg, secrets, facts, event_type, client_id, ads_granted):
    """Measurement Protocol ``POST /mp/collect?measurement_id=…&api_secret=…``.

    The URL carries the API secret: it must never be logged or previewed (``redact_url``).
    GA answers 2xx even for invalid payloads, so 2xx is not proof the event was accepted.
    """
    params = {}
    for key in GA4_PARAMS:
        value = facts["properties"].get(key)
        if isinstance(value, str) and value:
            params[key] = value[:GA4_PARAM_MAX]
    ad = "GRANTED" if ads_granted else "DENIED"  # explicit advertising grant only
    payload = {
        "client_id": client_id,
        "consent": {"ad_user_data": ad, "ad_personalization": ad},
        "events": [{"name": GA4_EVENT_NAMES[_event_name(event_type, facts)], "params": params}],
    }
    ts = unix_seconds(facts["occurred_at"])
    if ts is not None:
        payload["timestamp_micros"] = ts * 1_000_000
    if not cfg.get("measurement_id"):
        raise _Stop(Outcome("dead", "native_config_incomplete"))
    query = urlencode({"measurement_id": cfg["measurement_id"], "api_secret": secrets["api_secret"]})
    return [{"step": "collect", "method": "POST", "path": "/mp/collect?" + query, "json": payload, "auth": None}]


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

    def call(self, step, method, url, payload, headers, read, *, encode=None):
        """One vendor call. ``payload`` is JSON-encoded (``_encode``) unless ``encode`` is given
        (e.g. ``urlencode`` for an OAuth token request); GET sends no body."""
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
        body = b"" if method == "GET" else (encode(payload) if encode else _encode(payload))
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


def _step_headers(step):
    # Optional vendor credential header (Brevo ``api-key``) on top of the constant headers.
    return {**_headers(step.get("auth")), **(step.get("headers") or {})}


def _run_simple(ctx, base, steps):
    for s in steps:
        ctx.call(s["step"], s["method"], base + s["path"], s["json"], _step_headers(s), False)


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
        elif provider == "segment":
            _run_simple(ctx, base, plan_segment(cfg, secrets, facts, event_type))
        elif provider == "amplitude":
            _run_simple(ctx, base, plan_amplitude(cfg, secrets, facts, event_type))
        elif provider == "ga4":
            client_id, ads = ga4_inputs(body)
            _run_simple(ctx, base, plan_ga4(cfg, secrets, facts, event_type, client_id, ads))
        elif provider == "mixpanel":
            _analytics_guard(body)
            _run_simple(ctx, base, plan_mixpanel(cfg, secrets, facts, event_type))
        elif provider == "rudderstack":
            if not destination_hosts_supported():
                return Outcome("dead", "native_transport_unavailable")
            _analytics_guard(body)
            _run_simple(ctx, base, plan_rudderstack(cfg, secrets, facts, event_type))
        elif provider == "brevo":
            _run_simple(ctx, base, plan_brevo(cfg, secrets, facts, event_type, body))
        elif provider in REGISTRY:
            if provider in NEEDS_DESTINATION_HOSTS and not destination_hosts_supported():
                return Outcome("dead", "native_transport_unavailable")
            if provider in ANALYTICS_PROVIDERS:
                _analytics_guard(body)
            REGISTRY[provider].run(ctx, base, cfg, secrets, facts, event_type, body)
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
    if provider in NEEDS_DESTINATION_HOSTS and not destination_hosts_supported():
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

    def __init__(self, responder=None):
        self.calls = []
        self._responder = responder

    def call(self, step, method, url, payload, headers, read, *, encode=None):
        self.calls.append(
            {
                "step": step,
                "method": method,
                "url": redact_url(url),
                "headers": {k: (REDACTED if k.lower() in SECRET_HEADERS else v) for k, v in headers.items()},
                "body": _redact(payload),
            }
        )
        if not read:
            return None
        if self._responder is not None:
            return self._responder(step, method, url, payload)
        return {"results": [{"id": "<returned id>"}], "data": {"id": {"record_id": "<returned id>"}}}


# Query parameters / body keys that carry credentials in some vendor contracts.
SECRET_KEYS = frozenset({"api_key", "api_secret", "writeKey", "write_key"})
# Request headers that carry credentials (lower-case).
SECRET_HEADERS = frozenset({"authorization", "api-key"})


def redact_url(url):
    """``url`` with secret query values replaced (GA4 ``api_secret``). Safe to render or log."""
    if not isinstance(url, str) or "?" not in url:
        return url
    from urllib.parse import parse_qsl

    base, _, query = url.partition("?")
    pairs = [(k, REDACTED if k in SECRET_KEYS else v) for k, v in parse_qsl(query, keep_blank_values=True)]
    return base + "?" + urlencode(pairs)


def _redact(obj):
    if isinstance(obj, dict):
        return {k: (REDACTED if k in SECRET_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


# Bounded, operator-facing reasons a preview plan was refused. Keys are fixed codes (never
# payload values, provider text or exception messages); anything else maps to ``preview_refused``.
PREVIEW_REFUSALS = {
    "native_match_strategy_unconfirmed": "Choose a contact sync mode and confirm the email merge acknowledgement.",
    "native_config_invalid": "Fix the field mapping: use only the listed sources, unique uppercase targets and no reserved names.",
    "native_config_incomplete": "Fill in every required field for this provider.",
    "native_delivery_id_missing": "The sample has no delivery id; this is a platform defect, report it.",
    "preview_event_skipped": "The sample registration would not be sent with these settings (event type, identity or consent rules).",
    "preview_payload_unsupported": "The planned request is not a flat, size-bounded payload; check names and mapped fields.",
    "preview_refused": "The request plan was refused; check the settings listed above.",
}


def preview(provider, credentials, event_type="registration.created"):
    """Planned requests for a synthetic sample registration, secrets redacted. ``[]`` when not configured
    or when the plan is refused (see ``preview_refusal`` for the reason)."""
    return _preview(provider, credentials, event_type)[0]


def preview_refusal(provider, credentials, event_type="registration.created"):
    """``(code, guidance)`` from ``PREVIEW_REFUSALS`` when the sample plan is refused, else ``None``.

    ``None`` also when the provider is not configured enough to plan (``validate`` reports that).
    """
    code = _preview(provider, credentials, event_type)[1]
    return (code, PREVIEW_REFUSALS[code]) if code else None


def _refusal_code(exc):
    if isinstance(exc, Skip):
        return "preview_event_skipped"
    if isinstance(exc, KeyError):
        return "native_config_incomplete"
    error = exc.outcome.error or ""
    if error in PREVIEW_REFUSALS:
        return error
    if error.endswith((": payload_unsupported", ": payload_too_large")):
        return "preview_payload_unsupported"
    return "preview_refused"


def _preview(provider, credentials, event_type):
    if provider not in FIELDS or not base_url(provider, credentials):
        return [], None
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
        elif provider == "segment":
            steps = plan_segment(cfg, secrets, facts, event_type)
        elif provider == "amplitude":
            steps = plan_amplitude(cfg, secrets, facts, event_type)
        elif provider == "ga4":
            # Synthetic, clearly fake client id; real sends use only the captured one.
            steps = plan_ga4(cfg, secrets, facts, event_type, "1000000000.1000000000", False)
        elif provider == "mixpanel":
            steps = plan_mixpanel(cfg, secrets, facts, event_type)
        elif provider == "rudderstack":
            steps = plan_rudderstack(cfg, secrets, facts, event_type)
        elif provider == "brevo":
            steps = plan_brevo(cfg, secrets, facts, event_type, sample)
        else:
            steps = None
        if provider in REGISTRY:
            module = REGISTRY[provider]
            ctx = _PreviewContext(getattr(module, "preview_response", None))
            module.run(ctx, base, cfg, secrets, facts, event_type, sample)
        elif steps is not None:
            for s in steps:
                ctx.call(s["step"], s["method"], base + s["path"], s["json"], _step_headers(s), False)
        elif provider == "hubspot" and cfg.get("participation_object_type") and cfg.get("participation_id_property"):
            _run_hubspot(ctx, base, cfg, {"access_token": REDACTED}, facts, event_type)
        elif provider == "attio" and cfg.get("participation_list"):
            _run_attio(ctx, base, cfg, {"access_token": REDACTED}, facts, event_type)
    except (Skip, _Stop, KeyError) as exc:
        return [], _refusal_code(exc)
    for c in ctx.calls:
        c["body_json"] = json.dumps(c["body"], indent=2, sort_keys=True)
    return ctx.calls, None


# ---------------------------------------------------------------------------
# Provider modules (webinars/native_providers/): one self-contained module per vendor
# ---------------------------------------------------------------------------
#
# A module declares ``PROVIDER``, ``LABEL``, ``FIELDS`` (``Field`` tuple; first field is the
# region/host choice unless ``host_of`` is defined), static ``HOSTS`` (``{region: host}``) or a
# ``host_of(credentials)`` for an operator-allowlisted per-destination host, and
# ``run(ctx, base, cfg, secrets, facts, event_type, body)`` which performs the vendor calls
# through ``ctx.call`` and raises ``Skip`` / ``_Stop`` like the built-in adapters. Optional:
# ``ANALYTICS`` (requires Analytics consent), ``NEEDS_REQUEST_TRANSPORT``, ``PER_DESTINATION_HOST``,
# ``SECRET_HEADERS``, ``CUSTOM_ID_FIELD``, ``ORG_TARGET``, ``validate_extra(...)`` and
# ``preview_response(step, method, url, payload)`` (fake read responses for the redacted preview).


def _register(module):
    global VENDOR_HOSTS, SECRET_HEADERS
    provider = module.PROVIDER
    if provider in FIELDS:
        raise RuntimeError(f"native provider {provider!r} registered twice")
    REGISTRY[provider] = module
    FIELDS[provider] = tuple(module.FIELDS)
    hosts = dict(getattr(module, "HOSTS", {}) or {})
    if hosts:
        HOSTS[provider] = hosts
        VENDOR_HOSTS = frozenset(VENDOR_HOSTS | set(hosts.values()))
    PROVIDER_CHOICES.append((provider, module.LABEL))
    PROVIDER_LABELS[provider] = module.LABEL
    if getattr(module, "ANALYTICS", False):
        ANALYTICS_PROVIDERS.add(provider)
    if getattr(module, "NEEDS_REQUEST_TRANSPORT", False):
        NEEDS_REQUEST_TRANSPORT.add(provider)
    if getattr(module, "PER_DESTINATION_HOST", False) or hasattr(module, "host_of"):
        NEEDS_DESTINATION_HOSTS.add(provider)
    if getattr(module, "CUSTOM_ID_FIELD", None):
        CUSTOM_ID_FIELD[provider] = module.CUSTOM_ID_FIELD
    if getattr(module, "ORG_TARGET", None):
        ORG_TARGETS[provider] = module.ORG_TARGET
    extra_headers = {h.lower() for h in getattr(module, "SECRET_HEADERS", ())}
    if extra_headers:
        SECRET_HEADERS = frozenset(SECRET_HEADERS | extra_headers)


def _load_provider_modules():
    from webinars import native_providers

    for module in native_providers.modules():
        _register(module)


_load_provider_modules()
