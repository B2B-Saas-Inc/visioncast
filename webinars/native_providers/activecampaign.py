"""ActiveCampaign native adapter: email-keyed contact sync plus optional lifecycle tags.

Contract (API v3, official references fetched 2026-10-27, see docs/native-integrations.md):

* ``POST /api/3/contact/sync`` creates or updates one contact by email (200 updated, 201
  created; "duplicate-email errors do not occur"). Body: ``contact.email``, ``firstName``,
  ``lastName`` and ``fieldValues`` (``[{"field": <custom field id>, "value": ...}]``). Never
  ``phone``, ``lists``, ``tags``, ``status``, ``form``, ``account``/``orgid``, ``ip4`` or any
  other key: the contact is never subscribed to a list or automation.
* ``POST /api/3/contactTags`` applies one existing tag (by numeric id) to the contact returned
  by the sync: 201 applied, 200 already applied (documented; a retry is safe).
* Auth: ``Api-Token`` header only, sent to the account's own API host, which the marketer
  types (``acme.api-us1.com``); it must end in ``.api-us1.com``.

Event tracking (``trackcmp.net``, form-encoded, a different host) is NOT implemented.
"""
import re

from webinars import native_integrations as ni

PROVIDER = "activecampaign"
LABEL = "ActiveCampaign"
# Response bodies are read (the sync returns ``contact.id`` for the tag call).
NEEDS_REQUEST_TRANSPORT = True
SECRET_HEADERS = ("api-token",)

_TAG_ID = re.compile(r"^[1-9][0-9]{0,14}$")
# Custom-field mapping: canonical source -> numeric ActiveCampaign custom field id.
FIELD_SOURCES = {
    "firstName": ("facts", "first_name"),
    "lastName": ("facts", "last_name"),
    "company": ("facts", "company_name"),
    "jobTitle": ("facts", "job_title"),
    "webinar_event_title": ("properties", "webinar_event_title"),
    "webinar_starts_at": ("properties", "webinar_starts_at"),
    "webinar_registration_status": ("properties", "webinar_registration_status"),
    "webinar_attendance": ("properties", "webinar_attendance"),
    "webinar_event_type": ("properties", "webinar_event_type"),
}
_PAIR = r"(?:" + "|".join(re.escape(s) for s in FIELD_SOURCES) + r")=[1-9][0-9]{0,14}"
FIELD_MAP = re.compile(rf"^{_PAIR}(?:,{_PAIR}){{0,{len(FIELD_SOURCES) - 1}}}$")

# Lifecycle -> tag setting. ``registration.updated`` applies no tag (its status is in the field map).
TAG_SETTINGS = {"registered": "tag_registered", "attended": "tag_attended", "no_show": "tag_no_show"}

FIELDS = (
    ni.Field("region", "API host", "Your account's own API host, entered below.", choices=(("account", "Your account API host"),)),
    ni.Field(
        "api_host",
        "Account API host",
        "Bare host name only, e.g. acme.api-us1.com (from Settings > Developer). It must end in .api-us1.com.",
        pattern=ni._HOSTNAME,
    ),
    ni.Field("api_key", "API key", "Developer API key, sent only in the Api-Token header to the host above.", secret=True, pattern=ni._SECRET),
    ni.Field(
        "email_merge_ack",
        "Email match confirmation",
        "Required: every call creates or updates the contact that has the registrant's email, so names and mapped "
        "fields overwrite any existing contact with that email. Only registrations whose identity the platform verified "
        "(browser capability or new person) are sent; this is not proof of mailbox ownership.",
        choices=ni.EMAIL_MERGE_ACK.choices,
    ),
    ni.Field(
        "field_map",
        "Custom field mapping",
        "Comma-separated pairs of source=<numeric custom field id>, e.g. company=12,webinar_event_title=15. Sources: "
        + ", ".join(FIELD_SOURCES)
        + ". Phone and other data can't be mapped. Leave empty to send email and names only.",
        required=False,
        pattern=FIELD_MAP,
    ),
    ni.Field("tag_registered", "Tag ID: registered", "Numeric tag id applied on registration.created. Leave empty for none.", required=False, pattern=_TAG_ID),
    ni.Field("tag_attended", "Tag ID: attended", "Numeric tag id applied on check-in and recorded attendance. Leave empty for none.", required=False, pattern=_TAG_ID),
    ni.Field("tag_no_show", "Tag ID: no-show", "Numeric tag id applied on a recorded no-show. Leave empty for none.", required=False, pattern=_TAG_ID),
    ni.PROCESSING_ACK,
)


HOST_SUFFIXES = (".api-us1.com",)


def host_problem(host):
    return ni.vendor_host_problem(host, HOST_SUFFIXES)


def host_of(credentials):
    """The account API host when it is a bare host under the vendor's domain; else None."""
    if credentials.get(ni.NATIVE_PREFIX + "region") != "account":
        return None
    host = credentials.get(ni.NATIVE_PREFIX + "api_host", "")
    return host if host and host_problem(host) is None else None


def field_map(value):
    """``{source: field id}`` from ``company=12,…``; ``{}`` when empty, None when invalid."""
    if not value:
        return {}
    if not FIELD_MAP.match(value):
        return None
    pairs = [p.split("=", 1) for p in value.split(",")]
    sources, targets = [s for s, _ in pairs], [t for _, t in pairs]
    if len(set(sources)) != len(sources) or len(set(targets)) != len(targets):
        return None
    return dict(pairs)


def validate_extra(cfg, credentials, add, **kw):
    host = cfg.get("api_host", "")
    problem = host_problem(host) if host else None
    if problem == "format":
        add("native-api_host", "Enter a bare lowercase host name: no https://, port, path or wildcard.")
    elif problem == "vendor_host":
        add("native-api_host", "That host belongs to another provider; enter your ActiveCampaign account API host.")
    elif problem == "not_allowlisted":
        if ni.implicit_vendor_hosts():
            add("native-api_host", "Enter your account's API host, which ends in .api-us1.com.")
        else:
            add("provider", f"Ask your operator to add exactly {host} (not a wildcard or parent domain) to WEBHOOK_HOST_ALLOWLIST before enabling.")
    elif problem == "unsafe":
        add("native-api_host", "That host is not allowed as a destination.")
    mapping = cfg.get("field_map")
    if mapping and FIELD_MAP.match(mapping) and field_map(mapping) is None:
        add("native-field_map", "Map each source and each custom field id once.")


def _tag_for(event_type, facts):
    if event_type == "registration.created":
        return "registered"
    if event_type == "attendance.checked_in":
        return "attended"
    if event_type == "attendance.recorded":
        return facts["attendance"]  # attended | no_show (anything else was skipped by _event_name)
    return None


def run(ctx, base, cfg, secrets, facts, event_type, body):
    if cfg.get("email_merge_ack") != ni.CONFIRMED:
        raise ni._Stop(ni.Outcome("dead", "native_match_strategy_unconfirmed"))
    mapping = field_map(cfg.get("field_map"))
    if mapping is None:
        raise ni._Stop(ni.Outcome("dead", "native_config_invalid"))
    identity = body.get("identity") if isinstance(body.get("identity"), dict) else {}
    email = facts["email"] or identity.get("email")
    if not isinstance(email, str) or not email:
        raise ni.Skip("native_no_email")
    ni._event_name(event_type, facts)  # unsupported events / unknown attendance are skipped like elsewhere
    tag_key = _tag_for(event_type, facts)
    tag_id = cfg.get(TAG_SETTINGS[tag_key]) if tag_key else None
    if tag_id and not _TAG_ID.match(tag_id):
        raise ni._Stop(ni.Outcome("dead", "native_config_invalid"))
    contact = {"email": email}
    if facts["has_profile"]:
        contact.update(ni._clean({"firstName": facts["first_name"], "lastName": facts["last_name"]}))
    values = {}
    for source, field_id in mapping.items():
        kind, key = FIELD_SOURCES[source]
        value = facts[key] if kind == "facts" else facts["properties"].get(key)
        if value not in (None, ""):
            values[field_id] = value
    ni._require_flat("contact", values)  # scalars only, checked before any call
    ni._require_flat("contact", contact)
    if values:
        contact["fieldValues"] = [{"field": field_id, "value": value} for field_id, value in values.items()]
    headers = {**ni._headers(None), "Api-Token": secrets["api_key"]}
    data = ctx.call("contact", "POST", base + "/api/3/contact/sync", {"contact": contact}, headers, True)
    contact_id = ni._first_id(data, "contact", "id")
    if not contact_id or not contact_id.isdigit():
        raise ni._Stop(ni.Outcome("retry", "contact: response_missing_id"))
    if tag_id:
        # 201 applied, 200 already applied: both delivered, so a retry never duplicates the tag.
        ctx.call("tag", "POST", base + "/api/3/contactTags", {"contactTag": {"contact": int(contact_id), "tag": int(tag_id)}}, headers, False)


def preview_response(step, method, url, payload):
    return {"contact": {"id": "1"}}
