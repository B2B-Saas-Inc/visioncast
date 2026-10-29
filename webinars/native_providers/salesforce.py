"""Salesforce native adapter: one atomic Composite request of External-ID upserts.

Contract points marked "verified" were read on 2026-10-27 from the REST API Developer Guide
(developer.salesforce.com/docs/platform/api-rest/guide/: resources-composite-composite-post,
requests-composite, resources-composite-subrequest-result, resources-sobject-upsert-patch and
dome-upsert); everything else is an assumption (docs/native-integrations.md lists both).

* Host: the destination's own My Domain host (``instance_host``), typed by the marketer; it
  must be a bare name under ``.my.salesforce.com`` (sandboxes included), never a static vendor
  host. ``host_of`` pins every call to it, token request included.
* Auth: OAuth 2.0 client-credentials token from ``POST /services/oauth2/token`` (form body).
  The token is cached in memory only (``hosting.base``), never stored, and the response's
  ``instance_url`` must be the configured host or the message is dead-lettered.
* Writes: ``POST /services/data/<version>/composite`` with ``allOrNone: true`` and PATCH
  subrequests on ``/sobjects/<Object>/<ExternalIdField>/<value>`` (verified: upsert by external
  ID; 25 subrequests max; top-level 200 even when a subrequest fails; with allOrNone the other
  subrequests answer 400). Person (Lead or Contact), optional Account by verified org id, and a
  custom participation object keyed by our registration id. A retry re-sends the same keys, so
  it targets the same records; that no duplicate results depends on the External ID fields
  being Unique in the org (unverified).
* Only ``custom_id`` matching: Salesforce upserts only on External ID fields, and the standard
  ``Email`` field cannot be one, so email merge is not offered.
"""
import re
from urllib.parse import quote, urlencode, urlsplit

from webinars.hosting.base import cached_token, forget_token, store_token
from webinars.native_integrations import (
    NATIVE_PREFIX,
    ORG_NAMESPACE,
    PROCESSING_ACK,
    REDACTED,
    USER_AGENT,
    VENDOR_HOSTS,
    Field,
    Outcome,
    Skip,
    _event_name,
    _headers,
    _HOSTNAME,
    implicit_vendor_hosts,
    _require_flat,
    _require_size,
    _SECRET,
    _Stop,
    _clean,
    vendor_host_problem,
)

PROVIDER = "salesforce"
LABEL = "Salesforce"
API_VERSION = "v62.0"
NEEDS_REQUEST_TRANSPORT = True
CUSTOM_ID_FIELD = "person_external_id_field"
ORG_TARGET = "account_external_id_field"
# Client-credentials responses carry no expires_in (assumption); cache well under the shortest
# configurable org session timeout (15 min) and drop the token on a 401.
TOKEN_TTL_SECONDS = 900
# The composite body is checked before the token call. Salesforce documents no composite byte
# limit on the pages read; this bound is ours (unverified) and keeps bodies small.
MAX_COMPOSITE_BYTES = 200_000
MAX_SUBREQUESTS = 25  # verified: "up to 25 subrequests in a single call"
PERSON_OBJECTS = ("Lead", "Contact")

_CUSTOM_API_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,76}__c$")
_RELATIONSHIP_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,76}__r$")
# Values that go into an upsert URL path (person, registration and organization ids).
_PATH_VALUE = re.compile(r"^[\x21-\x7e]{1,255}$")
# Participation sources: the keys of ``facts["properties"]`` (ids, statuses, timestamps only).
PARTICIPATION_SOURCES = (
    "webinar_event_type",
    "webinar_event_id",
    "webinar_event_title",
    "webinar_occurrence_id",
    "webinar_starts_at",
    "webinar_registration_id",
    "webinar_registration_status",
    "webinar_attendance",
    "webinar_schema_version",
    "webinar_delivery_id",
)
_PAIR = r"webinar_[a-z_]{1,40}=[A-Za-z][A-Za-z0-9_]{0,76}__c"
_FIELD_MAP = re.compile(rf"^{_PAIR}(?:,{_PAIR}){{0,9}}$")

FIELDS = (
    Field("region", "Host", "Your org's own My Domain host, entered below.", choices=(("mydomain", "Your My Domain host"),)),
    Field(
        "instance_host",
        "My Domain host",
        "Bare host name only, e.g. acme.my.salesforce.com (a sandbox is just another host, e.g. "
        "acme--dev.sandbox.my.salesforce.com). It must end in .my.salesforce.com; the token request and every write go only there.",
        pattern=_HOSTNAME,
    ),
    Field(
        "client_id",
        "Consumer key",
        "Consumer key of the External Client App (or Connected App) with the client credentials flow enabled.",
        secret=True,
        pattern=_SECRET,
    ),
    Field("client_secret", "Consumer secret", "Consumer secret of that app. Sent only in the token request to your host.", secret=True, pattern=_SECRET),
    Field(
        "person_match",
        "Match existing people by",
        "Salesforce upserts only by an External ID field, and the standard Email field cannot be one, so people are "
        "matched on our person ID in the field below. An existing record with the same email but no person ID is not "
        "merged; a duplicate rule may then reject the write (dead-lettered).",
        choices=(("custom_id", "Our person ID in an External ID field"),),
    ),
    Field(
        "person_object",
        "Person object",
        "Lead needs LastName and Company, Contact needs LastName: a registration without them is skipped, never "
        "written with placeholders.",
        choices=(("Lead", "Lead"), ("Contact", "Contact")),
    ),
    Field(
        "person_external_id_field",
        "Person External ID field",
        "Custom field on that object (External ID, ideally also Unique) that stores our person ID, e.g. Webinar_Person_Id__c.",
        required=False,
        pattern=_CUSTOM_API_NAME,
    ),
    Field(
        "account_external_id_field",
        "Account External ID field",
        "Custom External ID field on Account that stores the organization ID (Contact mode only; Leads are never "
        "linked to accounts). Leave empty to skip accounts.",
        required=False,
        pattern=_CUSTOM_API_NAME,
    ),
    ORG_NAMESPACE,
    Field("participation_object", "Participation object", "Custom object for webinar participation, e.g. Webinar_Participation__c.", pattern=_CUSTOM_API_NAME),
    Field(
        "participation_external_id_field",
        "Participation External ID field",
        "External ID field on that object that stores our registration ID, e.g. Webinar_Registration_Id__c.",
        pattern=_CUSTOM_API_NAME,
    ),
    Field(
        "person_lookup_relationship",
        "Person lookup relationship",
        "Relationship name of the participation object's lookup to the person object, e.g. Contact__r or Lead__r. "
        "It is set by the person's External ID, never by a record ID.",
        pattern=_RELATIONSHIP_NAME,
    ),
    Field(
        "participation_field_map",
        "Participation field mapping",
        "Comma-separated pairs, e.g. webinar_event_title=Event_Title__c,webinar_attendance=Attendance__c. Sources: "
        + ", ".join(PARTICIPATION_SOURCES)
        + ". Targets are custom fields on the participation object; each source and target once. Leave empty to write only the IDs.",
        required=False,
        pattern=_FIELD_MAP,
    ),
    PROCESSING_ACK,
)


HOST_SUFFIXES = (".my.salesforce.com",)


def host_problem(host):
    return vendor_host_problem(host, HOST_SUFFIXES)


def host_of(credentials):
    """The configured My Domain host when it passes the per-destination host policy, else None."""
    if credentials.get(NATIVE_PREFIX + "region") != "mydomain":
        return None
    host = credentials.get(NATIVE_PREFIX + "instance_host", "")
    return host if isinstance(host, str) and host and host_problem(host) is None else None


def participation_field_map(value):
    """``{source: Field__c}`` from the mapping setting; ``{}`` when empty, None when invalid."""
    if not value:
        return {}
    if not _FIELD_MAP.match(value):
        return None
    pairs = [p.split("=", 1) for p in value.split(",")]
    sources, targets = [s for s, _ in pairs], [t for _, t in pairs]
    if any(s not in PARTICIPATION_SOURCES for s in sources) or len(set(sources)) != len(sources) or len(set(targets)) != len(targets):
        return None
    return dict(pairs)


def validate_extra(cfg, credentials, add, *, event_types=(), allowed_groups=(), required_consents=()):
    host = cfg.get("instance_host", "")
    problem = host_problem(host) if host else None
    if problem == "format":
        add("native-instance_host", "Enter a bare lowercase host name: no https://, port, path or wildcard.")
    elif problem == "vendor_host":
        add("native-instance_host", "That host belongs to another provider; enter your own Salesforce My Domain host.")
    elif problem == "not_allowlisted":
        if implicit_vendor_hosts():
            add("native-instance_host", "Enter your org's My Domain host, which ends in .my.salesforce.com.")
        else:
            add("provider", f"Ask your operator to add exactly {host} (not a wildcard or parent domain) to WEBHOOK_HOST_ALLOWLIST before enabling.")
    elif problem == "unsafe":
        add("native-instance_host", "That host is not allowed as a destination.")
    mapping = cfg.get("participation_field_map")
    if mapping and _FIELD_MAP.match(mapping) and _field_map_problem(cfg):
        add("native-participation_field_map", "Map each source and each field once, using only the listed sources; the ID fields and the lookup can't be targets.")
    if cfg.get("person_object") == "Lead" and cfg.get("account_external_id_field"):
        add("native-account_external_id_field", "Leads are never linked to accounts: choose Contact or clear this field.")
    if "profile" not in set(allowed_groups or []):
        add("allowed_groups", "Salesforce needs the profile data group: Lead and Contact records require a last name (and Lead a company).")


def _field_map_problem(cfg):
    mapping = participation_field_map(cfg.get("participation_field_map"))
    if mapping is None:
        return True
    reserved = {cfg.get("participation_external_id_field"), cfg.get("person_lookup_relationship")}
    return bool(set(mapping.values()) & reserved)


def _path(value, step):
    if not isinstance(value, str) or not _PATH_VALUE.match(value):
        raise _Stop(Outcome("dead", f"{step}: payload_unsupported"))
    return quote(value, safe="")


def _subrequest(ref, sobject, ext_field, value, body):
    # Verified: PATCH /services/data/vXX.X/sobjects/<sObject>/<fieldName>/<fieldValue> upserts by external id.
    url = f"/services/data/{API_VERSION}/sobjects/{quote(sobject, safe='')}/{quote(ext_field, safe='')}/{_path(value, ref)}"
    return {"method": "PATCH", "url": url, "referenceId": ref, "body": body}


def _cache_key(host, secrets):
    return {"host": host, "client_id": secrets["client_id"], "client_secret": secrets["client_secret"]}


def _token(ctx, base, host, secrets):
    """``(access_token, from_cache)``. The token never leaves this process or reaches logs."""
    key = _cache_key(host, secrets)
    use_cache = REDACTED not in key.values()  # previews plan with redacted secrets: never cache them
    if use_cache:
        cached = cached_token(PROVIDER, key)
        if cached:
            return cached, True
    form = {"grant_type": "client_credentials", "client_id": secrets["client_id"], "client_secret": secrets["client_secret"]}
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": USER_AGENT}
    data = ctx.call("token", "POST", base + "/services/oauth2/token", form, headers, True, encode=lambda p: urlencode(p).encode())
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise _Stop(Outcome("retry", "token: response_missing_token"))
    instance = data.get("instance_url")
    parts = urlsplit(instance) if isinstance(instance, str) else None
    # Credentials never follow a vendor-supplied URL: the org must be the configured host.
    if parts is None or parts.scheme != "https" or (parts.hostname or "").lower() != host or parts.port not in (None, 443):
        raise _Stop(Outcome("dead", "token: instance_mismatch"))
    if use_cache:
        store_token(PROVIDER, key, token, TOKEN_TTL_SECONDS)
    return token, False


def _halted(entry):
    # With allOrNone, the subrequests that did not cause the failure answer 400 PROCESSING_HALTED;
    # only the error code is read (a fixed vendor enum), never the message.
    body = entry.get("body")
    return isinstance(body, list) and any(isinstance(e, dict) and e.get("errorCode") == "PROCESSING_HALTED" for e in body)


def _check_composite(data, refs):
    results = data.get("compositeResponse") if isinstance(data, dict) else None
    if not isinstance(results, list) or len(results) != len(refs):
        raise _Stop(Outcome("retry", "composite: response_unparsable"))
    failures = []
    for entry in results:
        if not isinstance(entry, dict) or not isinstance(entry.get("httpStatusCode"), int):
            raise _Stop(Outcome("retry", "composite: response_unparsable"))
        status = entry["httpStatusCode"]
        if 200 <= status < 300:
            continue
        step = entry.get("referenceId") if entry.get("referenceId") in refs else "composite"
        failures.append((status, step, _halted(entry)))
    if not failures:
        return
    status, step, _ = next((f for f in failures if not f[2]), failures[0])  # the root cause, not a halted sibling
    if status in (408, 425, 429) or status >= 500:
        raise _Stop(Outcome("retry", f"{step}: http_status: {status}", status))
    # 4xx (schema, duplicate rule, permissions) and 300 (several records match the external id): retrying cannot succeed.
    raise _Stop(Outcome("dead", f"{step}: http_status: {status}", status))


def plan(cfg, facts, event_type):
    """The composite body for this message (pure; used by ``run`` and tests). Raises Skip/_Stop."""
    _event_name(event_type, facts)  # unsupported events / unknown attendance are skipped like elsewhere
    if cfg.get("person_match") != "custom_id":
        raise _Stop(Outcome("dead", "native_match_strategy_unconfirmed"))
    obj = cfg.get("person_object")
    pext, pobj, rext, rel = (cfg.get(k) for k in ("person_external_id_field", "participation_object", "participation_external_id_field", "person_lookup_relationship"))
    if obj not in PERSON_OBJECTS or not all((pext, pobj, rext, rel)):
        raise _Stop(Outcome("dead", "native_config_incomplete"))
    if _field_map_problem(cfg):
        raise _Stop(Outcome("dead", "native_config_invalid"))
    mapping = participation_field_map(cfg.get("participation_field_map"))
    if not facts["has_profile"] or not facts["last_name"] or (obj == "Lead" and not facts["company_name"]):
        # Lead requires LastName and Company, Contact LastName: no placeholders are invented.
        raise Skip("native_lead_fields_missing" if obj == "Lead" else "native_contact_fields_missing")
    requests = []
    person = _clean({pext: facts["person_id"], "Email": facts["email"], "FirstName": facts["first_name"], "LastName": facts["last_name"], "Title": facts["job_title"]})
    if obj == "Lead":
        person["Company"] = facts["company_name"]
    aext, org = cfg.get("account_external_id_field"), facts["org_key"]
    if obj == "Contact" and aext and org:
        # Account Name is required on create; the verified org id stands in when no company name is known.
        requests.append(_subrequest("account", "Account", aext, org, {aext: org, "Name": facts["company_name"] or org}))
        person["Account"] = {aext: org}  # relationship set by external id (assumed for the standard Account relationship)
    requests.append(_subrequest("person", obj, pext, facts["person_id"], person))
    participation = {rext: facts["registration_id"]}
    for source, target in mapping.items():
        value = facts["properties"].get(source)
        if value not in (None, ""):
            participation[target] = value
    _require_flat("participation", participation)
    participation[rel] = {pext: facts["person_id"]}  # verified pattern: Relationship__r: {ExtId__c: value}
    requests.append(_subrequest("participation", pobj, rext, facts["registration_id"], participation))
    if len(requests) > MAX_SUBREQUESTS:
        raise _Stop(Outcome("dead", "composite: payload_unsupported"))
    payload = {"allOrNone": True, "collateSubrequests": False, "compositeRequest": requests}
    _require_size("composite", payload, MAX_COMPOSITE_BYTES)
    return payload


def run(ctx, base, cfg, secrets, facts, event_type, body):
    payload = plan(cfg, facts, event_type)  # everything is checked before the token call
    host = (urlsplit(base).hostname or "").lower()
    if not host or host in VENDOR_HOSTS:
        raise _Stop(Outcome("dead", "native_config_incomplete"))
    token, from_cache = _token(ctx, base, host, secrets)
    try:
        data = ctx.call("composite", "POST", base + f"/services/data/{API_VERSION}/composite", payload, _headers(f"Bearer {token}"), True)
    except _Stop as stop:
        if from_cache and stop.outcome.status == 401:
            forget_token(PROVIDER, _cache_key(host, secrets))  # stale cached token: fetch a fresh one next attempt
            raise _Stop(Outcome("retry", "composite: token_rejected", 401)) from None
        raise
    finally:
        del token
    _check_composite(data, [r["referenceId"] for r in payload["compositeRequest"]])


def preview_response(step, method, url, payload):
    """Plausible read responses for the redacted preview (no network)."""
    if step == "token":
        return {"access_token": REDACTED, "instance_url": f"https://{urlsplit(url).hostname}", "token_type": "Bearer"}
    return {
        "compositeResponse": [
            {"referenceId": r["referenceId"], "httpStatusCode": 201, "httpHeaders": {}, "body": {"id": "<returned id>", "success": True, "errors": [], "created": True}}
            for r in payload.get("compositeRequest", [])
        ]
    }
