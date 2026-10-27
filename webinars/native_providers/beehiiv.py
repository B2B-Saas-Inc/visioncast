"""beehiiv native adapter: a newsletter subscription (free tier only) plus optional lifecycle tags.

beehiiv's only model for a person is the subscription itself, so **this adapter subscribes
registrants to the configured publication**. It says so in the operator-facing settings and
needs two explicit acknowledgements before it can be enabled (``subscribe_ack`` and
``email_merge_ack``), on top of the shared data-processing confirmation.

Contract (official references fetched 2026-10-27 with WebFetch, see docs/native-integrations.md):

* ``POST /v2/publications/{publication_id}/subscriptions`` (Create Subscription) with ``email``,
  ``reactivate_existing``, ``send_welcome_email``, optional ``custom_fields: [{name, value}]``,
  optional ``utm_source``/``utm_medium``/``utm_campaign`` and, only when the operator chose one,
  ``double_opt_override`` (``on``/``off``). Never ``tier`` (free only), ``premium_tiers``,
  ``premium_tier_ids``, ``stripe_customer_id``, ``referring_site``, ``referral_code``,
  ``automation_ids``, ``newsletter_list_ids``, ``complimentary_gift_id``. 200 returns
  ``data.id`` (``sub_`` + UUID). What beehiiv answers when the email already has a subscription is
  NOT documented on that page (UNVERIFIED): a 2xx is treated as converged, a 4xx is dead.
* ``POST …/subscriptions/{id}/tags`` with ``{"tags": ["…"]}`` (Add subscription tag): creates the
  tag on the publication when missing (max 100 unique tags per publication); re-adding an existing
  tag is not documented as idempotent (UNVERIFIED, assumed a no-op).
* Auth: ``Authorization: Bearer <api key>`` to ``api.beehiiv.com`` only. Rate limits are per key per
  fixed 60 s window (30–800 by plan); 429 on excess, no ``Retry-After`` documented.
"""
import re
from urllib.parse import quote

from webinars import native_integrations as ni

PROVIDER = "beehiiv"
LABEL = "beehiiv"
HOSTS = {"global": "api.beehiiv.com"}
API = "/v2/publications"
# The create response is read (``data.id`` is the path key of the tag call).
NEEDS_REQUEST_TRANSPORT = True

# Verified example ids: ``pub_00000000-0000-0000-0000-000000000000`` / ``sub_…`` (prefix + UUID).
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
PUBLICATION_ID = re.compile(rf"^pub_{_UUID}$")
SUBSCRIPTION_ID = re.compile(r"^sub_[A-Za-z0-9_-]{1,100}$")
_TAG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:-]{0,99}$")
_UTM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
# Custom field names are free text in beehiiv; here: no comma/equals (the pair delimiters), no leading
# or trailing space, printable ASCII only.
_FIELD_NAME = r"[A-Za-z0-9_](?:[A-Za-z0-9_ .:-]{0,48}[A-Za-z0-9_])?"
FIELD_SOURCES = {
    "firstName": ("facts", "first_name"),
    "lastName": ("facts", "last_name"),
    "company": ("facts", "company_name"),
    "jobTitle": ("facts", "job_title"),
    "webinar_event_title": ("properties", "webinar_event_title"),
    "webinar_starts_at": ("properties", "webinar_starts_at"),
    "webinar_registration_status": ("properties", "webinar_registration_status"),
    "webinar_attendance": ("properties", "webinar_attendance"),
}
_PAIR = r"(?:" + "|".join(re.escape(s) for s in FIELD_SOURCES) + rf")={_FIELD_NAME}"
FIELD_MAP = re.compile(rf"^{_PAIR}(?:,{_PAIR}){{0,{len(FIELD_SOURCES) - 1}}}$")
UTM_MEDIUM = "webinar"
DOUBLE_OPT = ("not_set", "on", "off")
BOOL_CHOICES = (("false", "No"), ("true", "Yes"))
# beehiiv documents no request-size limit on the pages read; this bound is ours (unverified).
MAX_BODY_BYTES = 20_000
# Lifecycle -> tag setting. ``registration.updated`` applies no tag (its status is in the field map).
TAG_SETTINGS = {"registered": "tag_registered", "attended": "tag_attended", "no_show": "tag_no_show"}

FIELDS = (
    ni.Field("region", "API host", "beehiiv API host.", choices=(("global", "api.beehiiv.com"),)),
    ni.Field("api_key", "API key", "beehiiv API key, sent only as a Bearer token to api.beehiiv.com.", secret=True, pattern=ni._SECRET),
    ni.Field("publication_id", "Publication ID", "The prefixed publication id, e.g. pub_00000000-0000-0000-0000-000000000000.", pattern=PUBLICATION_ID),
    ni.Field(
        "subscribe_ack",
        "Subscription confirmation",
        "Required: registrants are subscribed to this publication; this is the only thing beehiiv can do with them. "
        "Confirm you have a lawful basis to subscribe them and that Required consent reflects your policy.",
        choices=(("", "Not confirmed"), (ni.CONFIRMED, "Confirmed by the operator")),
    ),
    ni.Field(
        "email_merge_ack",
        "Email match confirmation",
        "Required: every call is keyed by the registrant's email, so mapped custom fields and tags apply to any existing "
        "subscription with that email. Only registrations whose identity the platform verified (browser capability or "
        "new person) are sent; this is not proof of mailbox ownership.",
        choices=ni.EMAIL_MERGE_ACK.choices,
    ),
    ni.Field(
        "send_welcome_email",
        "Send beehiiv's welcome email",
        "Default no. Yes asks beehiiv to send the publication's welcome email to each new subscriber.",
        required=False,
        choices=BOOL_CHOICES,
    ),
    ni.Field(
        "double_opt_override",
        "Double opt-in override",
        "Default: use the publication's own double opt-in setting. On: beehiiv sends a confirmation email and the "
        "subscription stays inactive until confirmed. Off: the subscription is activated at once without any confirmation "
        "email (needs the confirmation below). Pick an override only if you have documented why.",
        required=False,
        choices=(("not_set", "Publication default"), ("on", "On (confirmation email, inactive until confirmed)"), ("off", "Off (active immediately, no confirmation)")),
    ),
    ni.Field(
        "optin_ack",
        "Opt-in override confirmation",
        "Required when double opt-in is overridden to Off: confirm that registrants have already given the opt-in that "
        "the publication would otherwise ask for by email.",
        required=False,
        choices=(("", "Not confirmed"), (ni.CONFIRMED, "Confirmed by the operator")),
    ),
    ni.Field(
        "reactivate_existing",
        "Reactivate unsubscribed people",
        "Default no: someone who unsubscribed from this publication is never resubscribed by a webinar registration. "
        "Yes asks beehiiv to reactivate them (beehiiv intends this only for subscribers who knowingly resubscribe).",
        required=False,
        choices=BOOL_CHOICES,
    ),
    ni.Field(
        "custom_field_map",
        "Custom field mapping",
        "Comma-separated pairs of source=<existing beehiiv custom field name>, e.g. firstName=First Name,"
        "webinar_event_title=Last Webinar. Sources: " + ", ".join(FIELD_SOURCES) + ". Phone and other data can't be "
        "mapped. Leave empty to send the email only.",
        required=False,
        pattern=FIELD_MAP,
    ),
    ni.Field(
        "utm_source",
        "UTM source",
        "Optional constant sent as utm_source on new subscriptions (with utm_medium=webinar and utm_campaign=the event slug). "
        "Leave empty to send no UTM fields.",
        required=False,
        pattern=_UTM,
    ),
    ni.Field("tag_registered", "Tag: registered", "Tag name applied on registration.created. Leave empty for none.", required=False, pattern=_TAG_NAME),
    ni.Field("tag_attended", "Tag: attended", "Tag name applied on check-in and recorded attendance. Leave empty for none.", required=False, pattern=_TAG_NAME),
    ni.Field("tag_no_show", "Tag: no-show", "Tag name applied on a recorded no-show. Leave empty for none.", required=False, pattern=_TAG_NAME),
    ni.PROCESSING_ACK,
)


def custom_field_map(value):
    """``{source: beehiiv field name}`` from ``firstName=First Name,…``; ``{}`` when empty, None when invalid."""
    if not value:
        return {}
    if not FIELD_MAP.match(value):
        return None
    pairs = [p.split("=", 1) for p in value.split(",")]
    sources, targets = [s for s, _ in pairs], [t.casefold() for _, t in pairs]
    if len(set(sources)) != len(sources) or len(set(targets)) != len(targets):
        return None
    return dict(pairs)


def _bool(cfg, key):
    """``False``/``True`` from the ``false``/``true`` choice (blank = false); None for anything else."""
    value = cfg.get(key) or "false"
    return {"false": False, "true": True}.get(value)


def validate_extra(cfg, credentials, add, **kw):
    if cfg.get("subscribe_ack") != ni.CONFIRMED:
        add("native-subscribe_ack", "This integration subscribes registrants to the publication: confirm this to enable.")
    if (cfg.get("double_opt_override") or "not_set") == "off" and cfg.get("optin_ack") != ni.CONFIRMED:
        add("native-optin_ack", "Turning double opt-in off activates subscriptions without any confirmation email: confirm this to enable.")
    mapping = cfg.get("custom_field_map")
    if mapping and FIELD_MAP.match(mapping) and custom_field_map(mapping) is None:
        add("native-custom_field_map", "Map each source and each custom field name once.")


def _tag_for(event_type, facts):
    if event_type == "registration.created":
        return "registered"
    if event_type == "attendance.checked_in":
        return "attended"
    if event_type == "attendance.recorded":
        return facts["attendance"]  # attended | no_show (anything else was skipped by _event_name)
    return None


def _event_slug(body):
    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    slug = event.get("slug")
    return slug if isinstance(slug, str) and _UTM.match(slug) else None


def run(ctx, base, cfg, secrets, facts, event_type, body):
    if cfg.get("subscribe_ack") != ni.CONFIRMED or cfg.get("email_merge_ack") != ni.CONFIRMED:
        raise ni._Stop(ni.Outcome("dead", "native_match_strategy_unconfirmed"))
    double_opt = cfg.get("double_opt_override") or "not_set"
    if double_opt not in DOUBLE_OPT or (double_opt == "off" and cfg.get("optin_ack") != ni.CONFIRMED):
        raise ni._Stop(ni.Outcome("dead", "native_match_strategy_unconfirmed"))
    mapping = custom_field_map(cfg.get("custom_field_map"))
    publication = cfg.get("publication_id", "")
    welcome, reactivate = _bool(cfg, "send_welcome_email"), _bool(cfg, "reactivate_existing")
    utm_source = cfg.get("utm_source") or None
    if mapping is None or not PUBLICATION_ID.match(publication) or welcome is None or reactivate is None:
        raise ni._Stop(ni.Outcome("dead", "native_config_invalid"))
    if utm_source and not _UTM.match(utm_source):
        raise ni._Stop(ni.Outcome("dead", "native_config_invalid"))
    identity = body.get("identity") if isinstance(body.get("identity"), dict) else {}
    email = facts["email"] or identity.get("email")
    if not isinstance(email, str) or not email:
        raise ni.Skip("native_no_email")
    ni._event_name(event_type, facts)  # unsupported events / unknown attendance are skipped like elsewhere
    tag_key = _tag_for(event_type, facts)
    tag = cfg.get(TAG_SETTINGS[tag_key]) if tag_key else None
    if tag and not _TAG_NAME.match(tag):
        raise ni._Stop(ni.Outcome("dead", "native_config_invalid"))
    values = {}
    for source, name in mapping.items():
        kind, key = FIELD_SOURCES[source]
        if kind == "facts" and not facts["has_profile"]:
            continue
        value = facts[key] if kind == "facts" else facts["properties"].get(key)
        if value not in (None, ""):
            values[name] = value
    ni._require_flat("subscription", values)  # scalars only, checked before any call
    subscription = {"email": email, "reactivate_existing": reactivate, "send_welcome_email": welcome}
    if values:
        subscription["custom_fields"] = [{"name": name, "value": value} for name, value in values.items()]
    if utm_source:
        subscription["utm_source"] = utm_source
        subscription["utm_medium"] = UTM_MEDIUM
        slug = _event_slug(body)
        if slug:
            subscription["utm_campaign"] = slug
    if double_opt != "not_set":
        subscription["double_opt_override"] = double_opt
    ni._require_size("subscription", subscription, MAX_BODY_BYTES)
    headers = ni._headers(f"Bearer {secrets['api_key']}")
    url = f"{base}{API}/{publication}/subscriptions"
    data = ctx.call("subscription", "POST", url, subscription, headers, True)
    sub_id = ni._first_id(data, "data", "id")
    if not sub_id or not SUBSCRIPTION_ID.match(sub_id):
        raise ni._Stop(ni.Outcome("retry", "subscription: response_missing_id"))
    if tag:
        # Re-adding a tag already on the subscription is assumed a no-op (UNVERIFIED), so a retry never duplicates it.
        ctx.call("tags", "POST", f"{url}/{quote(sub_id, safe='')}/tags", {"tags": [tag]}, headers, True)


def preview_response(step, method, url, payload):
    return {"data": {"id": "sub_00000000-0000-0000-0000-000000000000"}}
