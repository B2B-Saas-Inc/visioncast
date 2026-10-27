"""Pipedrive native adapter: search-then-write person (and organization), plus one Activity per
lifecycle event with a dedupe marker.

Request shapes follow the Pipedrive API v2 OpenAPI specification downloaded on 2026-10-27 from
https://developers.pipedrive.com/docs/api/v1/openapi-v2.yaml (servers: ``https://api.pipedrive.com/api/v2``)
and the authentication guide (``x-api-token`` header). The v1 OpenAPI file fetched the same day no
longer lists persons, organizations or activities, so v1 is not used. Live behaviour is unverified.

* Person: ``GET /persons/search`` (``term``, ``fields=custom_fields`` or ``email``, ``exact_match``,
  ``limit=1``) then ``PATCH /persons/{id}`` or ``POST /persons``. Email mode needs the email merge
  confirmation; custom-ID mode stores our person id in a 40-character custom field key.
* Organization (optional): the same search on ``/organizations/search`` by the verified org id in
  an organization custom field; created when missing, never renamed when found; linked via ``org_id``.
* Activity: ``GET /activities?person_id=…`` (first page only) is scanned for the marker
  ``webinar-registration:<registration id>:<event type>`` in ``note``; when absent, ``POST /activities``
  creates one (primary participant = the person). This makes a retry converge instead of
  duplicating, within the scanned page.
* Never sent: ``marketing_status`` (no consent changes), ``job_title``/``notes``/contact-sync
  fields (403 without contact sync), labels, deals, leads, phones, URLs.
"""
import datetime
import re
from urllib.parse import quote, urlencode

from webinars.native_integrations import (
    EMAIL_MERGE_ACK,
    ORG_NAMESPACE,
    PERSON_MATCH,
    PROCESSING_ACK,
    Field,
    Outcome,
    Skip,
    _clean,
    _event_name,
    _first_id,
    _headers,
    _person_match,
    _require_flat,
    _require_size,
    _SECRET,
    _Stop,
)

PROVIDER = "pipedrive"
LABEL = "Pipedrive"
HOSTS = {"global": "api.pipedrive.com"}
API = "/api/v2"
NEEDS_REQUEST_TRANSPORT = True
SECRET_HEADERS = ("x-api-token",)
CUSTOM_ID_FIELD = "person_id_field"
ORG_TARGET = "org_id_field"
DEFAULT_ACTIVITY_TYPE = "meeting"
ACTIVITY_PAGE = 500  # verified: GET /activities limit max 500
SEARCH_LIMIT = 1  # verified: search limit max 100
# Pipedrive documents no request-size limit on the pages read; this bound is ours (unverified).
MAX_BODY_BYTES = 100_000
MARKER_PREFIX = "webinar-registration:"
SUBJECTS = {
    "webinar registered": "Webinar registered",
    "webinar registration updated": "Webinar registration updated",
    "webinar checked in": "Webinar checked in",
    "webinar attended": "Webinar attended",
    "webinar no-show": "Webinar no-show",
}
DONE_EVENTS = ("attendance.checked_in", "attendance.recorded")
SUBJECT_MAX = 255

# Verified: custom fields are "randomly generated 40-character hashes" (hex in every example).
_FIELD_KEY = re.compile(r"^[0-9a-f]{40}$")
_ACTIVITY_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
# Search terms: our ids are UUID-like; emails are plain addresses. Anything else is refused.
_TERM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TERM_EMAIL = re.compile(r"^[^\s@/?#&=]{1,64}@[^\s@/?#&=]{1,189}$")

FIELDS = (
    Field("region", "Region", "Pipedrive API host.", choices=(("global", "api.pipedrive.com"),)),
    Field("api_token", "API token", "Personal API token, sent in the x-api-token header only to api.pipedrive.com.", secret=True, pattern=_SECRET),
    PERSON_MATCH,
    EMAIL_MERGE_ACK,
    Field(
        "person_id_field",
        "Person ID custom field key",
        "40-character key of a person text custom field that stores our person ID (searchable types only: varchar, text, "
        "varchar_auto). Required when matching by custom ID; when set in email mode it is written too.",
        required=False,
        pattern=_FIELD_KEY,
    ),
    Field(
        "org_id_field",
        "Organization ID custom field key",
        "40-character key of an organization text custom field that stores the verified organization ID. Leave empty to skip organizations.",
        required=False,
        pattern=_FIELD_KEY,
    ),
    ORG_NAMESPACE,
    Field(
        "activity_type",
        "Activity type",
        "Key of the activity type to create, e.g. meeting (the default) or a custom key. Must exist in the account.",
        required=False,
        pattern=_ACTIVITY_TYPE,
    ),
    PROCESSING_ACK,
)


def _num(value):
    """Pipedrive ids are integers; keep the string when it is not (preview placeholders)."""
    return int(value) if isinstance(value, str) and value.isdigit() else value


def _term(value, pattern, step):
    if not isinstance(value, str) or not pattern.match(value):
        raise _Stop(Outcome("dead", f"{step}: payload_unsupported"))
    return value


def _search(ctx, base, headers, path, step, term, fields):
    query = urlencode({"term": term, "fields": fields, "exact_match": "true", "limit": SEARCH_LIMIT})
    data = ctx.call(step, "GET", f"{base}{API}/{path}/search?{query}", None, headers, True)
    return _first_id(data, "data", "items", 0, "item", "id")


def _write(ctx, base, headers, step, method, url, payload, record_id=None):
    """POST/PATCH ``payload``; returns the record id (from the response for a create)."""
    _require_size(step, payload, MAX_BODY_BYTES)
    data = ctx.call(step, method, url, payload, headers, True)
    if record_id:
        return record_id  # an update of a known record: 2xx is enough
    record_id = _first_id(data, "data", "id")
    if not record_id:
        raise _Stop(Outcome("retry", f"{step}: response_missing_id"))
    return record_id


def _due(starts_at):
    """``(YYYY-MM-DD, HH:MM:SS)`` in UTC from the occurrence start, else ``(None, None)``."""
    if not isinstance(starts_at, str) or not starts_at:
        return None, None
    try:
        dt = datetime.datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")


def marker(facts, event_type):
    return f"{MARKER_PREFIX}{facts['registration_id']}:{event_type}"


def activity_payload(cfg, facts, event_type, person_pd_id, org_pd_id):
    subject = SUBJECTS[_event_name(event_type, facts)]
    title = facts["properties"].get("webinar_event_title")
    if isinstance(title, str) and title:
        subject = f"{subject}: {title}"[:SUBJECT_MAX]
    due_date, due_time = _due(facts["properties"].get("webinar_starts_at"))
    activity = _clean(
        {
            "subject": subject,
            "type": cfg.get("activity_type") or DEFAULT_ACTIVITY_TYPE,
            "due_date": due_date,
            "due_time": due_time,
            "note": marker(facts, event_type),
            "org_id": _num(org_pd_id) if org_pd_id else None,
        }
    )
    activity["done"] = event_type in DONE_EVENTS
    activity["participants"] = [{"person_id": _num(person_pd_id), "primary": True}]
    return activity


def run(ctx, base, cfg, secrets, facts, event_type, body):
    _event_name(event_type, facts)  # unsupported events / unknown attendance are skipped before any call
    mode = _person_match(cfg)
    if mode == "custom_id" and not cfg.get("person_id_field"):
        raise _Stop(Outcome("dead", "native_config_incomplete"))
    if mode == "email" and not facts["email"]:
        raise Skip("native_no_email")
    headers = {**_headers(None), "x-api-token": secrets["api_token"]}
    _require_flat("person", {k: facts[k] for k in ("first_name", "last_name", "email", "company_name") if facts[k] is not None})
    identity = body.get("identity") if isinstance(body.get("identity"), dict) else {}
    email = facts["email"] or (identity.get("email") if isinstance(identity.get("email"), str) else None) or None
    name = " ".join(v for v in (facts["first_name"], facts["last_name"]) if v) or email
    if not name:
        raise Skip("native_person_name_missing")  # Pipedrive requires a name on create; none is invented
    custom = {}
    if cfg.get("person_id_field"):
        custom[cfg["person_id_field"]] = facts["person_id"]
    # Plan the search term before any call so a refused term never follows a write.
    if mode == "custom_id":
        term, fields = _term(facts["person_id"], _TERM_ID, "person_search"), "custom_fields"
    else:
        term, fields = _term(facts["email"], _TERM_EMAIL, "person_search"), "email"

    org_pd_id = None
    if cfg.get("org_id_field") and facts["org_key"]:
        org_term = _term(facts["org_key"], _TERM_ID, "organization_search")
        org_pd_id = _search(ctx, base, headers, "organizations", "organization_search", org_term, "custom_fields")
        if not org_pd_id:
            organization = {"name": facts["company_name"] or facts["org_key"], "custom_fields": {cfg["org_id_field"]: facts["org_key"]}}
            org_pd_id = _write(ctx, base, headers, "organization", "POST", f"{base}{API}/organizations", organization)

    person_pd_id = _search(ctx, base, headers, "persons", "person_search", term, fields)
    person = {"name": name}
    if custom:
        person["custom_fields"] = custom
    if org_pd_id:
        person["org_id"] = _num(org_pd_id)
    if person_pd_id:
        # Update: emails are not rewritten (PATCH replaces the whole list and would drop other addresses).
        _write(ctx, base, headers, "person", "PATCH", f"{base}{API}/persons/{quote(person_pd_id, safe='')}", person, record_id=person_pd_id)
    else:
        if email:
            person["emails"] = [{"value": email, "primary": True, "label": "work"}]
        person_pd_id = _write(ctx, base, headers, "person", "POST", f"{base}{API}/persons", person)

    query = urlencode({"person_id": person_pd_id, "limit": ACTIVITY_PAGE, "sort_by": "add_time", "sort_direction": "desc"})
    data = ctx.call("activities", "GET", f"{base}{API}/activities?{query}", None, headers, True)
    existing = data.get("data") if isinstance(data, dict) else None
    wanted = marker(facts, event_type)
    for item in existing if isinstance(existing, list) else []:
        if isinstance(item, dict) and isinstance(item.get("note"), str) and wanted in item["note"]:
            return  # already recorded by an earlier attempt: converge, do not duplicate
    _write(ctx, base, headers, "activity", "POST", f"{base}{API}/activities", activity_payload(cfg, facts, event_type, person_pd_id, org_pd_id))


def preview_response(step, method, url, payload):
    """Plausible read responses for the redacted preview: nothing found, every write returns an id."""
    if step.endswith("_search"):
        return {"success": True, "data": {"items": []}, "additional_data": {"next_cursor": None}}
    if step == "activities":
        return {"success": True, "data": [], "additional_data": {"next_cursor": None}}
    return {"success": True, "data": {"id": "<returned id>"}}
