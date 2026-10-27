"""Mailchimp Marketing API native adapter: audience member upsert, optional tags and a member event.

Contract (Marketing API 3.0, official references fetched 2026-10-27, see docs/native-integrations.md):

* ``PUT /3.0/lists/{audience}/members/{md5(lower(email))}`` adds or updates the member
  (``email_address``, ``status_if_new``, ``merge_fields``). ``status_if_new`` is only
  ``transactional`` (default: a non-subscribed contact who "can't receive marketing emails")
  or ``pending`` (opt-in confirmation, needs an explicit operator acknowledgement). Never
  ``subscribed``, and ``status`` is never sent, so an existing member's marketing status is
  never changed. No ``interests``, ``tags``, ``marketing_permissions``, ``ip_*`` or
  ``timestamp_*`` fields.
* ``POST …/members/{hash}/tags`` with ``{"tags": [{"name", "status": "active"}]}`` (204).
* ``POST …/members/{hash}/events`` with ``name`` (2-30 chars), string ``properties`` and
  ``occurred_at`` (204). Not idempotent: ``webinar_delivery_id`` lets consumers dedupe.
* Auth: HTTP Basic ``anystring:<api key>`` (the quick-start's ``--user "anystring:${apikey}"``)
  to ``<dc>.api.mailchimp.com``; the data center must equal the API key's suffix after its
  last ``-`` and be an exact operator allowlist entry.
"""
import base64
import hashlib
import re

from webinars import native_integrations as ni

PROVIDER = "mailchimp"
LABEL = "Mailchimp"
# The member upsert is a PUT (method-aware transport) and its response is read.
NEEDS_REQUEST_TRANSPORT = True

DATACENTER = re.compile(r"^[a-z]{2,3}[0-9]{1,3}$")
_AUDIENCE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TAG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:-]{0,99}$")
MERGE_SOURCES = ni.BREVO_ATTRIBUTE_SOURCES  # firstName, lastName, company, jobTitle -> fact keys
_PAIR = r"(?:firstName|lastName|company|jobTitle)=[A-Z][A-Z0-9_]{0,49}"
MERGE_MAP = re.compile(rf"^{_PAIR}(?:,{_PAIR}){{0,3}}$")
RESERVED_MERGE_TAGS = frozenset({"EMAIL"})
STATUS_IF_NEW = ("transactional", "pending")
DEFAULT_STATUS_IF_NEW = "transactional"
EVENT_NAME_MAX = 30
TAG_SETTINGS = {"registered": "tag_registered", "attended": "tag_attended", "no_show": "tag_no_show"}

FIELDS = (
    ni.Field("region", "API host", "<data center>.api.mailchimp.com, from the data center below.", choices=(("datacenter", "Data center from your API key"),)),
    ni.Field(
        "datacenter",
        "Data center",
        "The suffix after the last dash of your API key, e.g. us19. Calls go only to <data center>.api.mailchimp.com, which your operator must list exactly in WEBHOOK_HOST_ALLOWLIST.",
        pattern=DATACENTER,
    ),
    ni.Field("api_key", "API key", "Marketing API key, sent only as HTTP Basic auth to the data center host.", secret=True, pattern=ni._SECRET),
    ni.Field("audience_id", "Audience ID", "The audience (list) ID the member is added to or updated in.", pattern=_AUDIENCE),
    ni.Field(
        "email_merge_ack",
        "Email match confirmation",
        "Required: every call is keyed by the MD5 of the registrant's email, so merge fields overwrite any existing "
        "member with that email. Only registrations whose identity the platform verified (browser capability or new "
        "person) are sent; this is not proof of mailbox ownership.",
        choices=ni.EMAIL_MERGE_ACK.choices,
    ),
    ni.Field(
        "status_if_new",
        "Status for new members",
        "Transactional members can't receive marketing emails. Pending asks Mailchimp to send its opt-in confirmation; "
        "the member becomes subscribed only if they confirm. Existing members keep their status; subscribed is never set.",
        required=False,
        choices=(("transactional", "Transactional (default, not marketable)"), ("pending", "Pending (opt-in confirmation sent by Mailchimp)")),
    ),
    ni.Field(
        "optin_ack",
        "Opt-in email confirmation",
        "Required when new members are pending: confirm that Mailchimp may send registrants its opt-in confirmation email.",
        required=False,
        choices=(("", "Not confirmed"), (ni.CONFIRMED, "Confirmed by the operator")),
    ),
    ni.Field(
        "merge_field_map",
        "Merge field mapping",
        "Comma-separated pairs, e.g. firstName=FNAME,lastName=LNAME. Sources: firstName, lastName, company, jobTitle only. "
        "Targets are uppercase merge tags that already exist in the audience (never EMAIL). Leave empty to send the email only.",
        required=False,
        pattern=MERGE_MAP,
    ),
    ni.Field("tag_registered", "Tag: registered", "Tag name applied on registration.created. Leave empty for none.", required=False, pattern=_TAG_NAME),
    ni.Field("tag_attended", "Tag: attended", "Tag name applied on check-in and recorded attendance. Leave empty for none.", required=False, pattern=_TAG_NAME),
    ni.Field("tag_no_show", "Tag: no-show", "Tag name applied on a recorded no-show. Leave empty for none.", required=False, pattern=_TAG_NAME),
    ni.PROCESSING_ACK,
)


def host_of(credentials):
    """``<dc>.api.mailchimp.com`` when the data center is well-formed and the host is an exact
    operator allowlist entry; else None (every call is then refused)."""
    if credentials.get(ni.NATIVE_PREFIX + "region") != "datacenter":
        return None
    dc = credentials.get(ni.NATIVE_PREFIX + "datacenter", "")
    if not isinstance(dc, str) or not DATACENTER.match(dc):
        return None
    host = f"{dc}.api.mailchimp.com"
    return host if ni.destination_host_problem(host) is None else None


def key_matches_datacenter(credentials):
    """True when the stored key's suffix (after its last ``-``) equals the configured data center.
    Compares only; the key is never rendered."""
    key = credentials.get(ni.NATIVE_PREFIX + "api_key", "")
    dc = credentials.get(ni.NATIVE_PREFIX + "datacenter", "")
    return bool(key) and bool(dc) and key.rpartition("-")[2] == dc


def merge_field_map(value):
    """``{fact key: MERGE_TAG}`` from ``firstName=FNAME,…``; ``{}`` when empty, None when invalid."""
    if not value:
        return {}
    if not MERGE_MAP.match(value):
        return None
    pairs = [p.split("=", 1) for p in value.split(",")]
    sources, targets = [s for s, _ in pairs], [t for _, t in pairs]
    if len(set(sources)) != len(sources) or len(set(targets)) != len(targets) or set(targets) & RESERVED_MERGE_TAGS:
        return None
    return {MERGE_SOURCES[s]: t for s, t in pairs}


def validate_extra(cfg, credentials, add, **kw):
    dc = cfg.get("datacenter", "")
    if dc and DATACENTER.match(dc):
        host = f"{dc}.api.mailchimp.com"
        problem = ni.destination_host_problem(host)
        if problem == "not_allowlisted":
            add("provider", f"Ask your operator to add exactly {host} (not a wildcard or parent domain) to WEBHOOK_HOST_ALLOWLIST before enabling.")
        elif problem is not None:
            add("native-datacenter", "That data center host is not allowed as a destination.")
        if credentials.get(ni.NATIVE_PREFIX + "api_key") and not key_matches_datacenter(credentials):
            add("native-datacenter", "The data center must be the suffix after the last dash of the stored API key.")
    status = cfg.get("status_if_new") or DEFAULT_STATUS_IF_NEW
    if status == "pending" and cfg.get("optin_ack") != ni.CONFIRMED:
        add("native-optin_ack", "Pending members get Mailchimp's opt-in confirmation email: confirm this to enable.")
    mapping = cfg.get("merge_field_map")
    if mapping and MERGE_MAP.match(mapping) and merge_field_map(mapping) is None:
        add("native-merge_field_map", "Map each source and each merge tag once; EMAIL can't be mapped.")


def subscriber_hash(email):
    return hashlib.md5(email.strip().lower().encode("utf-8")).hexdigest()  # noqa: S324 - Mailchimp's documented member key


def _tag_for(event_type, facts):
    if event_type == "registration.created":
        return "registered"
    if event_type == "attendance.checked_in":
        return "attended"
    if event_type == "attendance.recorded":
        return facts["attendance"]
    return None


def run(ctx, base, cfg, secrets, facts, event_type, body):
    if cfg.get("email_merge_ack") != ni.CONFIRMED:
        raise ni._Stop(ni.Outcome("dead", "native_match_strategy_unconfirmed"))
    status = cfg.get("status_if_new") or DEFAULT_STATUS_IF_NEW
    if status not in STATUS_IF_NEW or (status == "pending" and cfg.get("optin_ack") != ni.CONFIRMED):
        raise ni._Stop(ni.Outcome("dead", "native_match_strategy_unconfirmed"))
    mapping = merge_field_map(cfg.get("merge_field_map"))
    audience = cfg.get("audience_id", "")
    if mapping is None or not _AUDIENCE.match(audience):
        raise ni._Stop(ni.Outcome("dead", "native_config_invalid"))
    if secrets["api_key"] != ni.REDACTED and not key_matches_datacenter({ni.NATIVE_PREFIX + "api_key": secrets["api_key"], ni.NATIVE_PREFIX + "datacenter": cfg.get("datacenter", "")}):
        raise ni._Stop(ni.Outcome("dead", "native_config_invalid"))
    ni._require_delivery_id(facts)
    identity = body.get("identity") if isinstance(body.get("identity"), dict) else {}
    email = facts["email"] or identity.get("email")
    if not isinstance(email, str) or not email:
        raise ni.Skip("native_no_email")
    name = ni.GA4_EVENT_NAMES.get(ni._event_name(event_type, facts))
    if not name or not 2 <= len(name) <= EVENT_NAME_MAX:
        raise ni._Stop(ni.Outcome("dead", "event: payload_unsupported"))
    tag_key = _tag_for(event_type, facts)
    tag = cfg.get(TAG_SETTINGS[tag_key]) if tag_key else None
    if tag and not _TAG_NAME.match(tag):
        raise ni._Stop(ni.Outcome("dead", "native_config_invalid"))
    member = {"email_address": email, "status_if_new": status}
    if facts["has_profile"]:
        merge_fields = ni._clean({target: facts[source] for source, target in mapping.items()})
        ni._require_flat("member", merge_fields)
        if merge_fields:
            member["merge_fields"] = merge_fields
    # Event properties are string-only per the reference; only scalar lifecycle facts, never profile data or URLs.
    properties = {k: str(v) for k, v in facts["properties"].items() if isinstance(v, ni._SCALARS) and v != ""}
    event = {"name": name, "properties": properties}
    if ni.unix_seconds(facts["occurred_at"]) is not None:
        event["occurred_at"] = facts["occurred_at"]
    token = base64.b64encode(f"anystring:{secrets['api_key']}".encode("utf-8")).decode("ascii")
    headers = ni._headers(f"Basic {token}")
    member_url = f"{base}/3.0/lists/{audience}/members/{subscriber_hash(email)}"
    data = ctx.call("member", "PUT", member_url, member, headers, True)
    if not ni._first_id(data, "id"):
        raise ni._Stop(ni.Outcome("retry", "member: response_missing_id"))
    if tag:
        ctx.call("tags", "POST", member_url + "/tags", {"tags": [{"name": tag, "status": "active"}]}, headers, False)
    # Last, so a failure before it never leaves a duplicate event behind on retry.
    ctx.call("event", "POST", member_url + "/events", event, headers, False)


def preview_response(step, method, url, payload):
    return {"id": "<returned id>"}
