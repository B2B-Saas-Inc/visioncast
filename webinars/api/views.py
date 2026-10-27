"""Public API, proxy-target API, same-origin direct proxy, ICS and health views.

CSRF: every view here is ``csrf_exempt`` because Django's cookie/form CSRF token does
not fit a JSON API called by a GTM-injected script or a server-side proxy. Instead:

* Proxy-target and registration endpoints (``/api/v1/...``) require a scoped API key in
  a request header, which a cross-site browser cannot attach (no ambient authority).
* Same-origin direct proxy endpoints (``/webinar-api/...``) carry ambient cookie
  authority, so they enforce: exact ``Origin`` match against
  ``settings.WEBINAR_ALLOWED_ORIGINS`` (required on POST, checked on GET if present),
  ``Sec-Fetch-Site: same-origin`` when the header is present, ``Content-Type:
  application/json`` on POST, and a custom header on POST — either
  ``X-Webinar-Request: 1`` or the SDK's double-submit token ``X-Webinar-CSRF`` equal to
  the ``__Host-webinar_csrf`` cookie. Custom headers / JSON content type force a CORS
  preflight cross-site, which we never grant.

``/webinar-api/calendar.ics`` is public (same data as ``/api/v1/occurrences/<id>/calendar.ics``)
and reads no cookie, so it has no origin check. ``/webinar-api/my-registration`` reads
the capability cookie, so on top of the GET origin checks it requires the POST custom
header (``X-Webinar-Request: 1`` or the double-submit token) and is rate limited. Its
proxy-target twin ``/api/v1/my-registration`` shares that limit only when
``RATE_LIMIT_TRUST_X_FORWARDED_FOR`` is on.

Responses that may relate to a person are ``Cache-Control: no-store``. Nothing in
this module logs or echoes submitted personal data.
"""
import copy
import hmac
import json
import re
import secrets
import uuid

from django.conf import settings
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from webinars import __version__
from webinars import calendar_links, identity, ratelimit
from webinars.api import queries
from webinars.api.auth import api_key_from_request, has_scope
from webinars.audit import audit
from webinars.ics import build_ics
from webinars.models import ApiKey, Event, Occurrence, Registration
from webinars.registration import build_response_body, error_body, register

MAX_BODY_BYTES = 64 * 1024
DEFAULT_LIMIT = queries.DEFAULT_LIMIT
MAX_LIMIT = queries.MAX_LIMIT
CSRF_COOKIE = "__Host-webinar_csrf"
CSRF_HEADER = "X-Webinar-CSRF"
REGISTER_SCOPES = (ApiKey.Scope.REGISTER, ApiKey.Scope.PROXY, ApiKey.Scope.TRUSTED)
DIRECT_CALENDAR_PATH = "/webinar-api/calendar.ics"
_BACKEND_ICS_RE = re.compile(r"^/api/v1/occurrences/([0-9a-fA-F-]{36})/calendar\.ics$")
# Per client address, per process (webinars.ratelimit); edge limits still apply.
MY_REGISTRATION_LIMIT = (60, 60)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json(body, status=200, *, no_store=False):
    response = JsonResponse(body, status=status, json_dumps_params={"ensure_ascii": False})
    if no_store:
        response["Cache-Control"] = "no-store"
    return response


def _err(status, code, message, fields=None, *, no_store=True):
    return _json(error_body(code, message, fields), status=status, no_store=no_store)


def _method_not_allowed(allowed):
    response = _err(405, "method_not_allowed", "Method not allowed.")
    response["Allow"] = ", ".join(allowed)
    return response


def _base_url():
    return (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")


def _iso(dt):
    return dt.astimezone(timezone.get_fixed_timezone(0)).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _calendar_path(occ):
    return calendar_links.ics_path(occ)


def _require_key(request, scopes):
    """Return (api_key, error_response)."""
    key = api_key_from_request(request)
    if key is None:
        return None, _err(401, "unauthorized", "A valid API key is required.")
    if not has_scope(key, *scopes):
        return None, _err(403, "forbidden", "This API key lacks the required scope.")
    return key, None


def _read_json(request):
    """Return (payload, error_response) enforcing JSON content type and 64 KB limit."""
    content_type = (request.META.get("CONTENT_TYPE") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return None, _err(415, "unsupported_media_type", "Content-Type must be application/json.")
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY_BYTES:
        return None, _err(413, "payload_too_large", "Request body is too large.")
    body = request.body
    if len(body) > MAX_BODY_BYTES:
        return None, _err(413, "payload_too_large", "Request body is too large.")
    if not body:
        return {}, None
    try:
        return json.loads(body.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, _err(400, "validation_error", "Request body is not valid JSON.")


# ---------------------------------------------------------------------------
# Serialization (public projection only: never join/waiting-room links)
# ---------------------------------------------------------------------------


def serialize_occurrence(occ):
    data = _serialize_occurrence(occ)
    # Capacity and price keys appear only when configured, so free unlimited sessions keep
    # the original shape. ``remaining`` is null unless the organiser opted in.
    if occ.capacity is not None:
        from webinars import capacity

        data["availability"] = capacity.public_availability(occ)
    if occ.ticket_price_minor is not None or occ.event.ticket_price_minor is not None:
        from webinars import payments

        data["ticket"] = payments.public_ticket(occ)
    return data


def _serialize_occurrence(occ):
    base = _base_url()
    return {
        "id": str(occ.pk),
        "startsAt": _iso(occ.starts_at),
        "endsAt": _iso(occ.ends_at),
        "timezone": occ.timezone,
        "status": occ.status,
        "registrationOpen": bool(
            occ.registration_open and occ.status == Occurrence.Status.SCHEDULED and occ.starts_at > timezone.now()
        ),
        "recordingUrl": occ.recording_url if occ.recording_published else None,
        "calendarUrl": base + _calendar_path(occ),
        # ICS plus Google/Outlook links built from public fields only (webinars.calendar_links).
        "calendarLinks": calendar_links.calendar_links(occ),
    }


def serialize_event(event):
    return {
        "slug": event.slug,
        "title": event.public_title,
        "summary": event.summary,
        "heroImageUrl": event.hero_url or None,
        "aida": {
            "attention": event.attention,
            "interest": event.interest,
            "decision": event.decision,
            "action": event.action,
        },
        "learningOutcomes": list(event.learning_outcomes or []),
        "hosts": [
            {"name": h.name, "slug": h.slug, "title": h.title, "imageUrl": h.image_url or None}
            for h in event.hosts.all()
        ],
        "topics": [{"name": t.name, "slug": t.slug} for t in event.topics.all()],
        "formVersion": event.form_version.version,
        "cta": event.cta,
        "occurrences": [serialize_occurrence(o) for o in event.occurrences.all()],
    }


_event_queryset = queries.event_queryset  # backwards-compatible alias


def list_public_events(*, q="", topic="", host="", when="", limit=None, offset=None):
    """Public catalogue page shared by ``GET /api/v1/events`` and the MCP ``list_events`` tool.

    Raises ``queries.QueryError`` for invalid filters or pagination.
    """
    qs = queries.public_events(q=q, topic=topic, host=host, when=when)
    limit, offset = queries.parse_page(limit, offset)
    count, rows = queries.page(qs, limit, offset)
    return {"count": count, "limit": limit, "offset": offset, "results": [serialize_event(e) for e in rows]}


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------


@csrf_exempt
def events_list(request):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    try:
        body = list_public_events(
            q=request.GET.get("q"),
            topic=request.GET.get("topic"),
            host=request.GET.get("host"),
            when=request.GET.get("when"),
            limit=request.GET.get("limit"),
            offset=request.GET.get("offset"),
        )
    except queries.QueryError as exc:
        return _err(400, "validation_error", exc.message, exc.fields, no_store=False)
    return _json(body)


@csrf_exempt
def event_detail(request, slug):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    event = queries.public_event(slug)
    if event is None:
        return _err(404, "not_found", "Event not found.", no_store=False)
    return _json(serialize_event(event))


def _ics_response(pk):
    occ = (
        Occurrence.objects.select_related("event")
        .filter(pk=pk, event__visibility__in=[Event.Visibility.PUBLIC, Event.Visibility.UNLISTED])
        .first()
    )
    if occ is None:
        return _err(404, "not_found", "Occurrence not found.", no_store=False)
    response = HttpResponse(build_ics(occ), content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{occ.event.slug}.ics"'
    return response


@csrf_exempt
def occurrence_ics(request, pk):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    return _ics_response(pk)


def _occurrence_id_query(request):
    """The single ``occurrenceId`` UUID query parameter (same rule as the Node proxy), or None."""
    if list(request.GET.keys()) != ["occurrenceId"] or len(request.GET.getlist("occurrenceId")) != 1:
        return None
    try:
        return uuid.UUID(request.GET["occurrenceId"])
    except ValueError:
        return None


@csrf_exempt
def direct_calendar(request):
    """``GET /webinar-api/calendar.ics?occurrenceId=<uuid>``: public ICS for direct mode.

    Same output and visibility rule as ``occurrence_ics``; no cookie or key is read.
    """
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    occ_id = _occurrence_id_query(request)
    if occ_id is None:
        return _err(400, "validation_error", "Invalid query.", {"occurrenceId": "Exactly one UUID is required."})
    return _ics_response(occ_id)


@csrf_exempt
def form_schema(request, version):
    """Public canonical form schema (labels, types, options). No PII, no key required."""
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    from webinars.forms_schema import public_schema

    form = queries.public_form_version(version)
    if form is None:
        return _err(404, "not_found", "Form version not found.", no_store=False)
    return _json(public_schema(form))


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return _json({"status": "unavailable", "version": __version__}, status=503, no_store=True)
    return _json({"status": "ok", "version": __version__}, no_store=True)


# ---------------------------------------------------------------------------
# Shared profile / registration / reset logic
# ---------------------------------------------------------------------------


def _profile(request):
    occ_id = request.GET.get("occurrenceId") or ""
    form_version = request.GET.get("formVersion") or ""
    fields = {}
    try:
        uuid.UUID(occ_id)
    except ValueError:
        fields["occurrenceId"] = "Must be a UUID."
    if not form_version:
        fields["formVersion"] = "Required."
    if fields:
        return _err(400, "validation_error", "Invalid query.", fields)
    occ = (
        Occurrence.objects.select_related("event__form_version")
        .filter(pk=occ_id, event__visibility__in=[Event.Visibility.PUBLIC, Event.Visibility.UNLISTED])
        .first()
    )
    if occ is None:
        return _err(404, "not_found", "Occurrence not found.")
    form = occ.event.form_version
    if form_version != form.version:
        return _err(400, "validation_error", "The form version does not match this event.", {"formVersion": "Mismatch."})
    cap = identity.resolve_capability(request)
    person = cap.person if cap is not None else None
    # Only missing keys are disclosed — never values.
    return _json(
        {"recognized": person is not None, "missingFields": identity.missing_fields(person, form), "formVersion": form.version},
        no_store=True,
    )


def _direct_ics_url(body):
    """Copy of a success body with ``calendar.icsUrl`` pointed at ``DIRECT_CALENDAR_PATH``.

    Same rewrite as the Node proxy; an unexpected shape drops the link rather than
    passing a broken or foreign one. Never mutates ``body`` (it may be a stored replay).
    """
    calendar = body.get("calendar") if isinstance(body, dict) else None
    if not isinstance(calendar, dict) or "icsUrl" not in calendar:
        return body
    body = copy.deepcopy(body)
    match = _BACKEND_ICS_RE.match(str(body["calendar"]["icsUrl"]))
    if match is None:
        del body["calendar"]["icsUrl"]
        return body
    try:
        occ_id = uuid.UUID(match.group(1))
    except ValueError:
        del body["calendar"]["icsUrl"]
        return body
    body["calendar"]["icsUrl"] = f"{DIRECT_CALENDAR_PATH}?occurrenceId={occ_id}"
    return body


def _direct_ics_rewrite_enabled():
    # On by default; WEBINAR_DIRECT_ICS_REWRITE=false restores the /api/v1/ link for edges
    # that route /api/v1/occurrences/ and rely on it (docs/continuation-api.md).
    return bool(getattr(settings, "WEBINAR_DIRECT_ICS_REWRITE", True))


def _registration(request, api_key, trusted, *, direct=False):
    payload, error = _read_json(request)
    if error is not None:
        return error
    capability = identity.resolve_capability(request)
    # Paid holds only (and only for mailbox-verified browsers, registration._paid_hold).
    # Behind the proxy every browser shares one address and one key, so the bucket is the
    # verified person the capability cookie proves, never a forwarded header the caller
    # could forge. Direct browsers are bounded per client address; server callers per key.
    if direct or api_key is None:
        rate_key = ("paid_hold", "ip:" + ratelimit.client_ip(request))
    elif capability is not None:
        rate_key = ("paid_hold", f"person:{capability.person_id}")
    else:
        rate_key = ("paid_hold_key", f"key:{api_key.pk}")
    result = register(payload=payload, capability=capability, api_key=api_key, trusted=trusted, rate_key=rate_key)
    body = result.body
    if direct and 200 <= result.status_code < 300 and _direct_ics_rewrite_enabled():
        body = _direct_ics_url(body)
    response = _json(body, status=result.status_code, no_store=True)
    if result.set_token:
        identity.set_capability_cookie(response, result.set_token)
    if result.clear_cookie:
        identity.clear_capability_cookie(response)
    return response


def _reset(request, api_key):
    cap = identity.resolve_capability(request)
    if cap is not None:
        identity.revoke_capability(cap, "user_reset")
        audit("capability.revoked", api_key=api_key, obj=cap, reason="user_reset")
    response = _json({"reset": True}, no_store=True)
    identity.clear_capability_cookie(response)
    return response


def _my_registration_within_limit(request):
    """One ``MY_REGISTRATION_LIMIT`` bucket per client address, shared by the direct and proxy routes."""
    limit, window = MY_REGISTRATION_LIMIT
    return ratelimit.hit_key("my_registration", ratelimit.client_ip(request), limit=limit, window=window)


def _my_registration(request, *, direct):
    """Registration status for the browser's capability, for one public/unlisted occurrence.

    Only the capability cookie identifies the person (never an email, person id or
    registration id from the request). The body never contains profile, consent,
    attendance or link data: just the status plus the event's public thank-you/calendar
    block (``registration.build_response_body``) when an active registration exists.
    Unrecognised browsers and recognised people without a registration get the same
    shape, so the occurrence's registrations are not enumerable.
    """
    occ_id = _occurrence_id_query(request)
    if occ_id is None:
        return _err(400, "validation_error", "Invalid query.", {"occurrenceId": "Exactly one UUID is required."})
    occ = (
        Occurrence.objects.select_related("event")
        .filter(pk=occ_id, event__visibility__in=[Event.Visibility.PUBLIC, Event.Visibility.UNLISTED])
        .first()
    )
    if occ is None:
        return _err(404, "not_found", "Occurrence not found.")
    cap = identity.resolve_capability(request)
    status = None
    if cap is not None:
        status = (
            Registration.objects.filter(occurrence=occ, person_id=cap.person_id).values_list("status", flat=True).first()
        )
    body = {"occurrenceId": str(occ.pk), "recognized": cap is not None, "registered": status == Registration.Status.REGISTERED,
            "status": status}
    if status == Registration.Status.REGISTERED:
        public = build_response_body(occ)
        body["thankYou"] = public["thankYou"]
        body["calendar"] = public["calendar"]
        if direct and _direct_ics_rewrite_enabled():
            body = _direct_ics_url(body)
    return _json(body, no_store=True)


# ---------------------------------------------------------------------------
# Proxy-target endpoints (API key)
# ---------------------------------------------------------------------------


@csrf_exempt
def api_profile(request):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    _key, error = _require_key(request, (ApiKey.Scope.PROXY,))
    if error is not None:
        return error
    return _profile(request)


@csrf_exempt
def api_registrations(request):
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    key, error = _require_key(request, REGISTER_SCOPES)
    if error is not None:
        return error
    return _registration(request, key, trusted=has_scope(key, ApiKey.Scope.TRUSTED))


@csrf_exempt
def api_reset(request):
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    key, error = _require_key(request, (ApiKey.Scope.PROXY,))
    if error is not None:
        return error
    return _reset(request, key)


@csrf_exempt
def api_my_registration(request):
    """Proxy-target twin of ``direct_my_registration`` (proxy key; cookie forwarded by the proxy)."""
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    _key, error = _require_key(request, (ApiKey.Scope.PROXY,))
    if error is not None:
        return error
    # Every proxied request comes from the proxy's own address, so a per-address limit
    # is only meaningful when the X-Forwarded-For client address it sets is trusted.
    # Otherwise this would be one shared budget for all browsers: rely on edge limits.
    if getattr(settings, "RATE_LIMIT_TRUST_X_FORWARDED_FOR", False) and not _my_registration_within_limit(request):
        return _err(429, "rate_limited", "Too many requests.")
    return _my_registration(request, direct=False)


# ---------------------------------------------------------------------------
# Same-origin direct proxy endpoints (cookie authority; origin/header CSRF defence)
# ---------------------------------------------------------------------------


def _allowed_origins():
    origins = getattr(settings, "WEBINAR_ALLOWED_ORIGINS", []) or []
    if isinstance(origins, str):
        origins = [o.strip() for o in origins.split(",")]
    return {o.rstrip("/") for o in origins if o}


def _check_same_origin(request, *, post, verify_header=None):
    """``verify_header`` (default: ``post``) requires the custom/double-submit header."""
    origin = request.headers.get("Origin")
    allowed = _allowed_origins()
    if post and not origin:
        return _err(403, "origin_rejected", "Origin header is required.")
    if origin is not None and origin not in allowed:
        return _err(403, "origin_rejected", "Origin is not allowed.")
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None and site != "same-origin":
        return _err(403, "origin_rejected", "Cross-site requests are not allowed.")
    if verify_header is None:
        verify_header = post
    if verify_header:
        csrf_cookie = request.COOKIES.get(CSRF_COOKIE, "")
        csrf_header = request.headers.get(CSRF_HEADER, "")
        custom_ok = request.headers.get("X-Webinar-Request") == "1"
        token_ok = bool(csrf_cookie) and bool(csrf_header) and hmac.compare_digest(csrf_cookie, csrf_header)
        if not (custom_ok or token_ok):
            return _err(403, "forbidden", "Missing request verification header.")
    return None


def _ensure_csrf_cookie(request, response):
    """Issue the SDK's double-submit token cookie (readable by JS; not an identity)."""
    if not request.COOKIES.get(CSRF_COOKIE):
        response.set_cookie(
            CSRF_COOKIE, secrets.token_urlsafe(32), path="/", secure=True, httponly=False, samesite="Strict"
        )
    return response


@csrf_exempt
def direct_profile(request):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    error = _check_same_origin(request, post=False)
    if error is not None:
        return error
    return _ensure_csrf_cookie(request, _profile(request))


@csrf_exempt
def direct_registrations(request):
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    error = _check_same_origin(request, post=True)
    if error is not None:
        return error
    # Never trusted: browser-supplied organization IDs stay unverified.
    return _registration(request, None, trusted=False, direct=True)


@csrf_exempt
def direct_reset(request):
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    error = _check_same_origin(request, post=True)
    if error is not None:
        return error
    return _reset(request, None)


@csrf_exempt
def direct_my_registration(request):
    """``GET /webinar-api/my-registration?occurrenceId=<uuid>`` (capability cookie)."""
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    error = _check_same_origin(request, post=False, verify_header=True)
    if error is not None:
        return error
    if not _my_registration_within_limit(request):
        return _err(429, "rate_limited", "Too many requests.")
    return _my_registration(request, direct=True)
