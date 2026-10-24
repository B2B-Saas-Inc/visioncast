"""Key-authenticated management (write) API. Contract: docs/management-api.md.

Scopes: ``content_write`` (events, hosts, topics, form versions, occurrences,
recordings) and ``attendance_write`` (attendance events only). Keys are sent in a
header (no ambient authority), so the views are ``csrf_exempt`` like the rest of
``webinars.api.views``.

Read-only access (docs/continuation-api.md): ``GET`` on events, occurrences and
waiting rooms also accepts ``config_read`` (``READ_ONLY``). The scope is checked per
method, so it never authorises POST/PATCH/DELETE, and read-only responses carry no
attendee-facing room URL (``_read_only_projection``). ``view.scopes`` stays the write
scope, so in-process callers (``invoke``, MCP) are unchanged.

Every write goes through the same validation and lifecycle code as Django admin:

* events: ``admin.EventForm`` (https hero URL, learning outcomes, slug uniqueness,
  thank-you redirect allowlist via ``Event.clean``), revision bump under
  ``select_for_update`` and an ``EventRevision`` snapshot (``admin.event_snapshot``);
* occurrences: ``admin.OccurrenceForm`` (IANA timezone, DST gap/fold, https links)
  and ``admin.persist_occurrence`` / ``scheduling.reschedule_occurrence``;
* cancel / recording / attendance: ``scheduling.cancel_occurrence``,
  ``publish_recording`` and ``record_attendance`` (reminders, ICS sequence, outbox).

Input is an explicit camelCase allowlist per resource; unknown keys and wrong types
are rejected and nothing is ever ``setattr``-ed from request keys. Responses never
contain join/waiting-room URLs (only ``hasJoinUrl`` flags) or any person data.
"""
import re
from datetime import datetime
from datetime import timezone as dt_timezone
from types import SimpleNamespace

from django import forms
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.db import models as dj_models
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from webinars.admin import EventForm, OccurrenceForm, event_snapshot, persist_occurrence
from webinars.audit import audit
from webinars.models import (
    ApiKey,
    AttendanceEvent,
    Event,
    EventRevision,
    FormVersion,
    Host,
    Occurrence,
    Registration,
    Series,
    Topic,
)
from webinars.normalization import CANONICAL_FIELDS
from webinars.scheduling import (
    MANUAL_ATTENDANCE_KINDS,
    cancel_occurrence,
    has_current_check_in,
    publish_recording,
    record_attendance,
)
from webinars.validation import validate_public_https_url

from .views import _iso, _json, _method_not_allowed, _read_json, _require_key

CONTENT = (ApiKey.Scope.CONTENT_WRITE,)
# Existing redacted, non-privileged read scope; GET only, never a write (see module doc).
READ_ONLY = (ApiKey.Scope.CONFIG_READ,)
CONTENT_READ = CONTENT + READ_ONLY
ATTENDANCE = (ApiKey.Scope.ATTENDANCE_WRITE,)
# Recordings are publication-only; event visibility changes need content_write AND publication.
PUBLISH = (ApiKey.Scope.PUBLICATION,)

_LOCAL_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class _Invalid(Exception):
    def __init__(self, fields, message="Request validation failed.", status=400, code="validation_error"):
        super().__init__(message)
        self.fields = fields
        self.message = message
        self.status = status
        self.code = code


def _conflict(message):
    return _Invalid(None, message, status=409, code="conflict")


class _ConfirmationRequired(_Invalid):
    """428 with a side-effect-free preview and a single-use confirmToken."""

    def __init__(self, preview, token):
        super().__init__(None, "Publication needs confirmation: repeat the same request with header "
                               f"{CONFIRM_HEADER}: <confirmToken>.", status=428, code="confirmation_required")
        self.extra = {"preview": preview, "confirmToken": token}


CONFIRM_HEADER = "Webinar-Confirm-Token"


def _require_publication_scope(request, key):
    from webinars.api.auth import has_scope

    if not getattr(request, "publication_confirmed", False) and not has_scope(key, ApiKey.Scope.PUBLICATION):
        raise _Invalid(None, "Publishing requires the publication scope.", status=403, code="forbidden")


def _require_publication(request, key, action, material, preview, state=None):
    """Every publication path (event visibility, recordings) needs the ``publication``
    scope and a confirmed preview. MCP passes ``publication_confirmed`` only after its
    own gate (``webinars.mcp_config``) consumed a token for the same change."""
    from webinars import confirmation

    if getattr(request, "publication_confirmed", False):
        return
    _require_publication_scope(request, key)
    headers = getattr(request, "headers", None) or {}
    token = headers.get(CONFIRM_HEADER, "")
    if not token:
        raise _ConfirmationRequired(preview, confirmation.issue(action, key, material, state))
    try:
        confirmation.consume(action, key, material, token, state)
    except confirmation.ConfirmationError as exc:
        raise _Invalid(None, exc.message, status=400, code=exc.code)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

# Type tags: "str", "bool", "int", "strlist", "fold", "dict".
EVENT_FIELDS = {
    "internalName": ("internal_name", "str"),
    "slug": ("slug", "str"),
    "publicTitle": ("public_title", "str"),
    "summary": ("summary", "str"),
    "heroImageUrl": ("hero_image_url", "str"),
    "attention": ("attention", "str"),
    "interest": ("interest", "str"),
    "decision": ("decision", "str"),
    "action": ("action", "str"),
    "learningOutcomes": ("learning_outcomes", "strlist"),
    "hosts": ("hosts", "strlist"),
    "topics": ("topics", "strlist"),
    "visibility": ("visibility", "str"),
    "formVersion": ("form_version", "str"),
    "thankYouMode": ("thank_you_mode", "str"),
    "thankYouUrl": ("thank_you_url", "str"),
    "thankYouMessage": ("thank_you_message", "str"),
    "registerLabel": ("register_label", "str"),
    "confirmLabel": ("confirm_label", "str"),
}
EVENT_REQUIRED_ON_CREATE = ("internalName", "slug", "publicTitle", "formVersion")

HOST_FIELDS = {
    "name": ("name", "str"),
    "slug": ("slug", "str"),
    "title": ("title", "str"),
    "bio": ("bio", "str"),
    "imageUrl": ("image_url", "str"),
}
TOPIC_FIELDS = {"name": ("name", "str"), "slug": ("slug", "str")}

FORM_FIELDS = {
    "version": ("version", "str"),
    "description": ("description", "str"),
    "requiredFields": ("required_fields", "strlist"),
    "optionalFields": ("optional_fields", "strlist"),
    "fieldMapping": ("field_mapping", "dict"),
    "customFields": ("custom_fields", "strlist"),
    "defaultPhoneRegion": ("default_phone_region", "str"),
    "smsDisclosureVersion": ("sms_disclosure_version", "str"),
    "smsDisclosureText": ("sms_disclosure_text", "str"),
    "isActive": ("is_active", "bool"),
}
# A published form version is a contract with every embedded form; only these may change.
FORM_MUTABLE = {"description", "isActive"}

OCCURRENCE_FIELDS = {
    "seriesId": ("series", "str_or_null"),
    "timezone": ("timezone", "str"),
    "localStart": ("local_start", "str"),
    "dstFold": ("dst_fold", "fold"),
    "durationMinutes": ("duration_minutes", "int"),
    "registrationOpen": ("registration_open", "bool"),
    "joinUrl": ("join_url", "str"),
    "waitingRoomUrl": ("waiting_room_url", "str"),
}
OCCURRENCE_REQUIRED_ON_CREATE = ("timezone", "localStart")
_OCC_TIME_KEYS = {"timezone", "localStart", "dstFold", "durationMinutes"}
MIN_DURATION_MINUTES = 1
MAX_DURATION_MINUTES = 24 * 60
MIN_LEAD_MINUTES = 0
MAX_LEAD_MINUTES = 240  # mirrors webinars.waiting_room.configure

WAITING_ROOM_FIELDS = {
    "enabled": ("waiting_room_enabled", "bool"),
    "admissionLeadMinutes": ("admission_lead_minutes", "int"),
    "admissionMode": ("admission_mode", "str"),
}


# ---------------------------------------------------------------------------
# JSON Schema for the inputs above (single source for OpenAPI and MCP tools)
# ---------------------------------------------------------------------------

_KIND_SCHEMA = {
    "str": {"type": "string"},
    "str_or_null": {"type": ["string", "null"]},
    "bool": {"type": "boolean"},
    "int": {"type": "integer"},
    "fold": {"type": ["integer", "null"], "enum": [0, 1, None]},
    "strlist": {"type": "array", "items": {"type": "string"}},
    "dict": {"type": "object", "additionalProperties": {"type": "string"}},
}


def _model_field(model, name):
    try:
        return model._meta.get_field(name)
    except FieldDoesNotExist:
        return None


def input_schema(field_map, model=None, *, required=(), extra=None):
    """JSON Schema (draft 2020-12 subset) for an allowlisted camelCase input object.

    Types follow ``_check_type``; ``maxLength`` comes from the model field. ``extra``
    adds per-key keywords (enums, bounds, descriptions) on top.
    """
    props = {}
    for camel, (model_name, kind) in field_map.items():
        schema = dict(_KIND_SCHEMA[kind])
        if kind == "strlist":
            schema["items"] = {"type": "string"}
        field = _model_field(model, model_name) if model is not None else None
        if field is not None and kind in ("str", "str_or_null") and not field.is_relation:
            if getattr(field, "max_length", None):
                schema["maxLength"] = field.max_length
            if isinstance(field, dj_models.URLField):
                schema["format"] = "uri"
        schema.update((extra or {}).get(camel, {}))
        props[camel] = schema
    out = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = list(required)
    return out


def event_input_schema(*, create=False):
    return input_schema(
        EVENT_FIELDS,
        Event,
        required=EVENT_REQUIRED_ON_CREATE if create else (),
        extra={
            "visibility": {
                "enum": list(Event.Visibility.values),
                "description": "Create accepts only draft; publish/unpublish with an update.",
            },
            "thankYouMode": {"enum": list(Event.ThankYouMode.values)},
            "formVersion": {"maxLength": 64, "description": "An existing form version."},
            "hosts": {"description": "Host slugs."},
            "topics": {"description": "Topic slugs."},
            "heroImageUrl": {"description": "https image URL. Rejected while the event has an uploaded cover (coverImageId)."},
            "registerLabel": {"description": "Submit button text; blank uses “Register”. Plain text, no < > `."},
            "confirmLabel": {"description": "Button text when a recognised visitor has nothing to fill in; blank uses “Confirm my RSVP”."},
        },
    )


def occurrence_input_schema(*, create=False):
    return input_schema(
        OCCURRENCE_FIELDS,
        Occurrence,
        required=OCCURRENCE_REQUIRED_ON_CREATE if create else (),
        extra={
            "seriesId": {"description": "Numeric id of a series of this event, or null to detach."},
            "timezone": {"description": "IANA timezone, e.g. Europe/London"},
            "localStart": {
                "pattern": _LOCAL_START_RE.pattern,
                "description": "Wall clock YYYY-MM-DDTHH:MM[:SS] in that timezone, no offset.",
            },
            "dstFold": {"description": "Disambiguates a repeated wall-clock hour (0 first, 1 second)."},
            "durationMinutes": {"minimum": MIN_DURATION_MINUTES, "maximum": MAX_DURATION_MINUTES},
            "joinUrl": {"writeOnly": True, "description": "Write-only provider link; never returned."},
            "waitingRoomUrl": {"writeOnly": True, "description": "Write-only provider link; never returned."},
        },
    )


def waiting_room_input_schema():
    return input_schema(
        WAITING_ROOM_FIELDS,
        Occurrence,
        extra={
            "admissionLeadMinutes": {"minimum": MIN_LEAD_MINUTES, "maximum": MAX_LEAD_MINUTES},
            "admissionMode": {"enum": list(Occurrence.Admission.values)},
        },
    )


def attendance_input_schema():
    return input_schema(
        {"kind": ("kind", "str")}, required=("kind",), extra={"kind": {"enum": [str(k) for k in MANUAL_ATTENDANCE_KINDS]}}
    )


def _check_type(key, value, kind):
    if kind == "str":
        ok = isinstance(value, str)
    elif kind == "str_or_null":
        ok = value is None or isinstance(value, str)
    elif kind == "bool":
        ok = isinstance(value, bool)
    elif kind == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "fold":
        ok = value is None or (isinstance(value, int) and not isinstance(value, bool) and value in (0, 1))
    elif kind == "strlist":
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    elif kind == "dict":
        ok = isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
    else:  # pragma: no cover - programming error
        ok = False
    if not ok:
        return f"Invalid type for {key}."
    return None


def _parse(body, allowed, *, required=(), mutable=None):
    """Return ``{camelKey: value}`` for an allowlisted, type-checked JSON object."""
    if not isinstance(body, dict):
        raise _Invalid(None, "Request body must be a JSON object.")
    errors = {}
    for key in body:
        if key not in allowed:
            errors[key] = "Unknown field."
        elif mutable is not None and key not in mutable:
            errors[key] = "This field cannot be changed."
    for key in required:
        if key not in body:
            errors[key] = "This field is required."
    for key, value in body.items():
        if key in allowed and key not in errors:
            problem = _check_type(key, value, allowed[key][1])
            if problem:
                errors[key] = problem
    if errors:
        raise _Invalid(errors)
    return dict(body)


def _form_errors(form, field_map):
    reverse = {model: camel for camel, (model, _kind) in field_map.items()}
    fields = {}
    for name, messages in form.errors.items():
        key = reverse.get(name, "_" if name == "__all__" else name)
        fields[key] = str(messages[0]) if messages else "Invalid value."
    return _Invalid(fields)


def _parse_local_start(value):
    if not _LOCAL_START_RE.match(value):
        raise _Invalid({"localStart": "Use a wall-clock time YYYY-MM-DDTHH:MM[:SS] without an offset."})
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise _Invalid({"localStart": "Invalid date or time."})


def _validate_slug_refs(model, slugs, key):
    unique = list(dict.fromkeys(slugs))
    found = dict(model.objects.filter(slug__in=unique).values_list("slug", "pk"))
    missing = [s for s in unique if s not in found]
    if missing:
        raise _Invalid({key: f"Unknown slug(s): {', '.join(missing)}."})
    return [found[s] for s in unique]


# ---------------------------------------------------------------------------
# Projections (management view: no private links, no person data)
# ---------------------------------------------------------------------------


def _local(dt):
    if dt is None:
        return None
    naive = timezone.make_naive(dt, dt_timezone.utc) if timezone.is_aware(dt) else dt
    return naive.strftime("%Y-%m-%dT%H:%M:%S")


def serialize_occurrence(occ):
    return {
        "id": str(occ.pk),
        "eventId": str(occ.event_id),
        "seriesId": str(occ.series_id) if occ.series_id else None,
        "timezone": occ.timezone,
        "localStart": _local(occ.local_start),
        "dstFold": occ.dst_fold,
        "durationMinutes": occ.duration_minutes,
        "startsAt": _iso(occ.starts_at),
        "endsAt": _iso(occ.ends_at),
        "status": occ.status,
        "registrationOpen": occ.registration_open,
        "hasJoinUrl": bool(occ.join_url),
        "hasWaitingRoomUrl": bool(occ.waiting_room_url),
        "recordingUrl": occ.recording_url if occ.recording_published else None,
        "recordingPublishedAt": _iso(occ.recording_published_at),
        "icsSequence": occ.ics_sequence,
        "scheduleGeneration": occ.schedule_generation,
        "cancelledAt": _iso(occ.cancelled_at),
        "waitingRoom": serialize_waiting_room(occ),
    }


def serialize_waiting_room(occ):
    from webinars.waiting_room import room_state, room_url

    return {
        "enabled": occ.waiting_room_enabled,
        # The attendee-facing room URL is meant to be shared; the join link never is.
        "url": room_url(occ) if occ.waiting_room_enabled else None,
        "admissionMode": occ.admission_mode,
        "admissionLeadMinutes": occ.admission_lead_minutes,
        "state": room_state(occ).state if occ.waiting_room_enabled else None,
        "attendanceFinalizedAt": _iso(occ.attendance_finalized_at),
    }


def serialize_event(event):
    data = event_snapshot(event)
    data.update(
        {
            "id": str(event.pk),
            "createdAt": _iso(event.created_at),
            "updatedAt": _iso(event.updated_at),
            "occurrences": [serialize_occurrence(o) for o in event.occurrences.order_by("starts_at")],
        }
    )
    return data


def serialize_host(host):
    return {"slug": host.slug, "name": host.name, "title": host.title, "bio": host.bio, "imageUrl": host.image_url}


def serialize_topic(topic):
    return {"slug": topic.slug, "name": topic.name}


def serialize_form(fv):
    return {
        "version": fv.version,
        "description": fv.description,
        "requiredFields": list(fv.required_fields or []),
        "optionalFields": list(fv.optional_fields or []),
        "fieldMapping": dict(fv.field_mapping or {}),
        "customFields": list(fv.custom_fields or []),
        "defaultPhoneRegion": fv.default_phone_region,
        "smsDisclosureVersion": fv.sms_disclosure_version,
        "smsDisclosureText": fv.sms_disclosure_text,
        "isActive": fv.is_active,
    }


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _run(func, request, key, body, args, kwargs):
    """Run endpoint logic and map errors. Returns ``(status, payload_or_error_body)``."""
    from webinars.registration import error_body

    try:
        return func(request, key, body, *args, **kwargs)
    except _Invalid as exc:
        body = error_body(exc.code, exc.message, exc.fields)
        body.update(getattr(exc, "extra", None) or {})
        return exc.status, body
    except ValidationError as exc:
        return 400, error_body("validation_error", "; ".join(exc.messages) or "Invalid request.")
    except (Event.DoesNotExist, Occurrence.DoesNotExist, Registration.DoesNotExist,
            Host.DoesNotExist, Topic.DoesNotExist, FormVersion.DoesNotExist):
        return 404, error_body("not_found", "Not found.")
    except IntegrityError:
        return 409, error_body("conflict", "The change conflicts with existing data.")


def _read_only_projection(payload):
    """Drop attendee-facing room URLs (every ``url`` key; fails closed) for read-only keys.

    Provider join/waiting-room links are never serialized for anyone (``hasJoinUrl`` flags only).
    """
    if isinstance(payload, dict):
        return {k: (None if k == "url" else _read_only_projection(v)) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_read_only_projection(v) for v in payload]
    return payload


def _endpoint(scopes, methods, read_scopes=None):
    """Authenticate (401/403), enforce methods (405), parse JSON and map errors.

    ``read_scopes`` (if given) are accepted for GET only; every other method needs ``scopes``.
    """

    def decorator(func):
        @csrf_exempt
        def view(request, *args, **kwargs):
            from webinars.api.auth import has_scope

            reading = read_scopes is not None and request.method == "GET" and "GET" in methods
            key, error = _require_key(request, read_scopes if reading else scopes)
            if error:
                return error
            if request.method not in methods:
                return _method_not_allowed(methods)
            body = None
            if request.method in ("POST", "PATCH"):
                body, error = _read_json(request)
                if error:
                    return error
            status, payload = _run(func, request, key, body, args, kwargs)
            if status == 204:
                response = HttpResponse(status=204)
                response["Cache-Control"] = "no-store"
                return response
            if reading and not has_scope(key, *scopes):
                payload = _read_only_projection(payload)
            return _json(payload, status=status, no_store=True)

        view.logic = func
        view.scopes = scopes
        view.read_scopes = read_scopes
        view.methods = methods
        return view

    return decorator


def invoke(view, method, key, body=None, **kwargs):
    """Call a management endpoint's logic in-process (used by the MCP server).

    Applies the same scope check, method allowlist, input parsing and error mapping
    as HTTP. Returns ``(status, payload)``; payload is ``None`` for 204.
    """
    from webinars.api.auth import has_scope
    from webinars.registration import error_body

    if not has_scope(key, *view.scopes):
        return 403, error_body("forbidden", "This API key lacks the required scope.")
    if method not in view.methods:
        return 405, error_body("method_not_allowed", "Method not allowed.")
    if method in ("POST", "PATCH") and body is None:
        body = {}
    return _run(view.logic, SimpleNamespace(method=method), key, body, (), kwargs)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _event_form_data(event):
    return {
        "internal_name": event.internal_name,
        "slug": event.slug,
        "public_title": event.public_title,
        "summary": event.summary,
        "hero_image_url": event.hero_image_url,
        "attention": event.attention,
        "interest": event.interest,
        "decision": event.decision,
        "action": event.action,
        "learning_outcomes": list(event.learning_outcomes or []),
        "hosts": list(event.hosts.values_list("pk", flat=True)),
        "topics": list(event.topics.values_list("pk", flat=True)),
        "visibility": event.visibility,
        "form_version": event.form_version_id,
        "thank_you_mode": event.thank_you_mode,
        "thank_you_url": event.thank_you_url,
        "thank_you_message": event.thank_you_message,
        "register_label": event.register_label,
        "confirm_label": event.confirm_label,
    }


def _apply_event_input(data, values):
    for camel, value in values.items():
        model_name = EVENT_FIELDS[camel][0]
        if camel == "hosts":
            value = _validate_slug_refs(Host, value, "hosts")
        elif camel == "topics":
            value = _validate_slug_refs(Topic, value, "topics")
        elif camel == "formVersion":
            pk = FormVersion.objects.filter(version=value).values_list("pk", flat=True).first()
            if pk is None:
                raise _Invalid({"formVersion": "Unknown form version."})
            value = pk
        data[model_name] = value
    return data


def _save_revision(event, key, created, changed):
    EventRevision.objects.get_or_create(
        event=event, revision=event.revision, defaults={"snapshot": event_snapshot(event), "created_by": None}
    )
    audit(
        "event.created" if created else "event.revised",
        api_key=key,
        obj=event,
        revision=event.revision,
        fields=sorted(changed),
    )


@_endpoint(CONTENT, ("GET", "POST"), read_scopes=CONTENT_READ)
def events(request, key, body):
    if request.method == "GET":
        qs = Event.objects.select_related("form_version").order_by("-created_at")[:200]
        return 200, {"results": [serialize_event(e) for e in qs]}
    values = _parse(body, EVENT_FIELDS, required=EVENT_REQUIRED_ON_CREATE)
    if values.get("visibility", Event.Visibility.DRAFT) != Event.Visibility.DRAFT:
        raise _Invalid({"visibility": "Events are created as draft; publish with PATCH."})
    data = {"visibility": Event.Visibility.DRAFT, "thank_you_mode": Event.ThankYouMode.MESSAGE,
            "learning_outcomes": [], "hosts": [], "topics": []}
    _apply_event_input(data, values)
    form = EventForm(data=data)
    if not form.is_valid():
        raise _form_errors(form, EVENT_FIELDS)
    with transaction.atomic():
        event = form.save(commit=False)
        event.revision = 1
        event.save()
        form.save_m2m()
        _save_revision(event, key, True, form.changed_data)
    return 201, serialize_event(event)


@_endpoint(CONTENT, ("GET", "PATCH", "DELETE"), read_scopes=CONTENT_READ)
def event_detail(request, key, body, pk):
    if request.method == "GET":
        return 200, serialize_event(Event.objects.get(pk=pk))
    if request.method == "DELETE":
        with transaction.atomic():
            event = Event.objects.select_for_update().get(pk=pk)
            if event.visibility != Event.Visibility.DRAFT:
                raise _conflict("Only draft events can be deleted; unpublish with PATCH visibility=draft first.")
            if Registration.objects.filter(occurrence__event=event).exists():
                raise _conflict("Events with registrations cannot be deleted; set visibility instead.")
            audit("event.deleted", api_key=key, obj=event, revision=event.revision)
            event.delete()
        return 204, None
    values = _parse(body, EVENT_FIELDS)
    with transaction.atomic():
        event = Event.objects.select_for_update().get(pk=pk)
        current = event.visibility
        target = values.get("visibility", current)
        if target != current:
            if target not in Event.Visibility.values:
                raise _Invalid({"visibility": f"Must be one of: {', '.join(Event.Visibility.values)}."})
            _require_publication_scope(request, key)
        state = event.revision
        before = event_snapshot(event)
        data = _apply_event_input(_event_form_data(event), values)
        form = EventForm(data=data, instance=event)
        if not form.is_valid():
            raise _form_errors(form, EVENT_FIELDS)
        if target != current:
            from webinars.services import publish_problems

            # Readiness of the *patched* candidate (same rule as MCP set_event_visibility and
            # the studio): publishing an event that is not ready is refused, never previewed.
            problems = publish_problems(form.instance) if target != Event.Visibility.DRAFT else []
            if problems:
                raise _Invalid({"visibility": " ".join(problems)}, "The event is not ready to publish.",
                               status=409, code="not_ready")
            preview = {"eventId": str(event.pk), "from": current, "to": target, "revision": state, "problems": [],
                       "otherFields": sorted(k for k in values if k != "visibility")}
            _require_publication(request, key, "rest.event.patch", {"eventId": str(event.pk), "body": values},
                                 preview, state=state)
        after = event_snapshot(form.instance)
        after["hosts"] = sorted(h.slug for h in form.cleaned_data["hosts"])
        after["topics"] = sorted(t.slug for t in form.cleaned_data["topics"])
        if after != before:
            event = form.save(commit=False)
            event.revision += 1
            event.save()
            form.save_m2m()
            _save_revision(event, key, False, form.changed_data)
    return 200, serialize_event(Event.objects.get(pk=pk))


# ---------------------------------------------------------------------------
# Hosts / topics / form versions
# ---------------------------------------------------------------------------

HostForm = forms.modelform_factory(Host, fields=("name", "slug", "title", "bio", "image_url"))
TopicForm = forms.modelform_factory(Topic, fields=("name", "slug"))


def _save_simple(form_class, field_map, values, key, action, instance=None, extra_check=None):
    data = {}
    if instance is not None:
        data = {model: getattr(instance, model) for _camel, (model, _kind) in field_map.items()}
    for camel, value in values.items():
        data[field_map[camel][0]] = value
    if extra_check:
        extra_check(data)
    form = form_class(data=data, instance=instance)
    if not form.is_valid():
        raise _form_errors(form, field_map)
    if instance is not None and not form.has_changed():
        return instance
    with transaction.atomic():
        obj = form.save()
        audit(action, api_key=key, obj=obj, created=instance is None, fields=sorted(form.changed_data))
    return obj


def _check_host(data):
    if data.get("image_url"):
        try:
            validate_public_https_url(data["image_url"])
        except ValidationError as exc:
            raise _Invalid({"imageUrl": exc.messages[0]})


@_endpoint(CONTENT, ("POST",))
def hosts(request, key, body):
    values = _parse(body, HOST_FIELDS, required=("name", "slug"))
    return 201, serialize_host(_save_simple(HostForm, HOST_FIELDS, values, key, "host.saved", extra_check=_check_host))


@_endpoint(CONTENT, ("PATCH",))
def host_detail(request, key, body, slug):
    values = _parse(body, HOST_FIELDS)
    host = Host.objects.get(slug=slug)
    return 200, serialize_host(
        _save_simple(HostForm, HOST_FIELDS, values, key, "host.saved", instance=host, extra_check=_check_host)
    )


@_endpoint(CONTENT, ("POST",))
def topics(request, key, body):
    values = _parse(body, TOPIC_FIELDS, required=("name", "slug"))
    return 201, serialize_topic(_save_simple(TopicForm, TOPIC_FIELDS, values, key, "topic.saved"))


@_endpoint(CONTENT, ("PATCH",))
def topic_detail(request, key, body, slug):
    values = _parse(body, TOPIC_FIELDS)
    topic = Topic.objects.get(slug=slug)
    return 200, serialize_topic(_save_simple(TopicForm, TOPIC_FIELDS, values, key, "topic.saved", instance=topic))


def _check_form_schema(values):
    errors = {}
    canonical = set(CANONICAL_FIELDS)
    for camel in ("requiredFields", "optionalFields"):
        bad = [f for f in values.get(camel, []) if f not in canonical]
        if bad:
            errors[camel] = f"Not canonical field key(s): {', '.join(bad)}."
    overlap = set(values.get("requiredFields", [])) & set(values.get("optionalFields", []))
    if overlap and "optionalFields" not in errors:
        errors["optionalFields"] = "A field cannot be both required and optional."
    for alias, target in values.get("fieldMapping", {}).items():
        if not _KEY_RE.match(alias) or target not in canonical or alias in canonical:
            errors["fieldMapping"] = "Aliases must be simple keys mapping to canonical fields."
            break
    if any(not _KEY_RE.match(c) for c in values.get("customFields", [])):
        errors["customFields"] = "Custom field keys must be simple identifiers."
    region = values.get("defaultPhoneRegion", "")
    if region and not re.fullmatch(r"[A-Z]{2}", region):
        errors["defaultPhoneRegion"] = "Use an ISO 3166-1 alpha-2 code in upper case."
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", values.get("version", "")):
        errors["version"] = "Version must be 1-64 characters of letters, digits, '.', '_' or '-'."
    if errors:
        raise _Invalid(errors)


@_endpoint(CONTENT, ("POST",))
def form_versions(request, key, body):
    values = _parse(body, FORM_FIELDS, required=("version", "requiredFields"))
    _check_form_schema(values)
    if FormVersion.objects.filter(version=values["version"]).exists():
        raise _conflict("This form version already exists; form versions are immutable.")
    with transaction.atomic():
        fv = FormVersion.objects.create(
            version=values["version"],
            description=values.get("description", ""),
            required_fields=values["requiredFields"],
            optional_fields=values.get("optionalFields", []),
            field_mapping=values.get("fieldMapping", {}),
            custom_fields=values.get("customFields", []),
            default_phone_region=values.get("defaultPhoneRegion", ""),
            sms_disclosure_version=values.get("smsDisclosureVersion", ""),
            sms_disclosure_text=values.get("smsDisclosureText", ""),
            is_active=values.get("isActive", True),
        )
        audit("formversion.saved", api_key=key, obj=fv, created=True)
    return 201, serialize_form(fv)


@_endpoint(CONTENT, ("PATCH",))
def form_version_detail(request, key, body, version):
    values = _parse(body, FORM_FIELDS, mutable=FORM_MUTABLE)
    if "description" in values and len(values["description"]) > 300:
        raise _Invalid({"description": "At most 300 characters."})
    with transaction.atomic():
        fv = FormVersion.objects.select_for_update().get(version=version)
        changed = []
        if "description" in values and values["description"] != fv.description:
            fv.description = values["description"]
            changed.append("description")
        if "isActive" in values and values["isActive"] != fv.is_active:
            fv.is_active = values["isActive"]
            changed.append("is_active")
        if changed:
            fv.save(update_fields=changed + ["updated_at"])
            audit("formversion.saved", api_key=key, obj=fv, created=False, fields=changed)
    return 200, serialize_form(fv)


# ---------------------------------------------------------------------------
# Occurrences
# ---------------------------------------------------------------------------


def _occurrence_form_data(occ):
    return {
        "event": occ.event_id,
        "series": occ.series_id or "",
        "timezone": occ.timezone,
        "local_start": timezone.make_naive(occ.local_start, dt_timezone.utc)
        if timezone.is_aware(occ.local_start) else occ.local_start,
        "dst_fold": "" if occ.dst_fold is None else occ.dst_fold,
        "duration_minutes": occ.duration_minutes,
        "registration_open": occ.registration_open,
        "join_url": occ.join_url,
        "waiting_room_url": occ.waiting_room_url,
        "recording_url": occ.recording_url,
    }


def _apply_occurrence_input(data, values, event):
    for camel, value in values.items():
        model_name = OCCURRENCE_FIELDS[camel][0]
        if camel == "localStart":
            value = _parse_local_start(value)
        elif camel == "seriesId":
            if value is None:
                value = ""
            else:
                series = Series.objects.filter(pk=value, event=event).first() if _is_int(value) else None
                if series is None:
                    raise _Invalid({"seriesId": "Unknown series for this event."})
                value = series.pk
        elif camel == "dstFold":
            value = "" if value is None else value
        elif camel == "durationMinutes" and not MIN_DURATION_MINUTES <= value <= MAX_DURATION_MINUTES:
            raise _Invalid({"durationMinutes": f"Must be {MIN_DURATION_MINUTES}-{MAX_DURATION_MINUTES} minutes."})
        data[model_name] = value
    return data


def _is_int(value):
    return isinstance(value, str) and value.isdigit() and len(value) < 19


def _run_occurrence_form(data, instance=None):
    form = OccurrenceForm(data=data, instance=instance)
    if not form.is_valid():
        raise _form_errors(form, OCCURRENCE_FIELDS)
    return form


@_endpoint(CONTENT, ("POST",))
def event_occurrences(request, key, body, pk):
    event = Event.objects.get(pk=pk)
    values = _parse(body, OCCURRENCE_FIELDS, required=OCCURRENCE_REQUIRED_ON_CREATE)
    data = _apply_occurrence_input(
        {"event": event.pk, "series": "", "dst_fold": "", "duration_minutes": 60, "registration_open": True,
         "join_url": "", "waiting_room_url": "", "recording_url": ""},
        values,
        event,
    )
    form = _run_occurrence_form(data)
    with transaction.atomic():
        occ = form.instance
        persist_occurrence(SimpleNamespace(user=None), form, occ, change=False)
        audit("occurrence.api_write", api_key=key, obj=occ, created=True)
    return 201, serialize_occurrence(Occurrence.objects.get(pk=occ.pk))


@_endpoint(CONTENT, ("GET", "PATCH"), read_scopes=CONTENT_READ)
def occurrence_detail(request, key, body, pk):
    if request.method == "GET":
        return 200, serialize_occurrence(Occurrence.objects.get(pk=pk))
    values = _parse(body, OCCURRENCE_FIELDS)
    with transaction.atomic():
        occ = Occurrence.objects.select_for_update().get(pk=pk)
        if occ.status == Occurrence.Status.CANCELLED and values:
            raise _conflict("Cancelled occurrences cannot be changed.")
        data = _apply_occurrence_input(_occurrence_form_data(occ), values, occ.event)
        form = _run_occurrence_form(data, instance=occ)
        if form.has_changed():
            # Time changes go through scheduling.reschedule_occurrence (supersedes and
            # recreates reminders, bumps ICS sequence, notifies registrants).
            persist_occurrence(SimpleNamespace(user=None), form, occ, change=True)
            audit("occurrence.api_write", api_key=key, obj=occ, created=False, fields=sorted(form.changed_data))
    return 200, serialize_occurrence(Occurrence.objects.get(pk=pk))


@_endpoint(CONTENT, ("POST",))
def occurrence_cancel(request, key, body, pk):
    _parse(body, {})
    occ = Occurrence.objects.get(pk=pk)
    with transaction.atomic():
        already = Occurrence.objects.select_for_update().get(pk=pk).status == Occurrence.Status.CANCELLED
        cancel_occurrence(occ)
        if not already:
            audit("occurrence.api_write", api_key=key, obj=occ, action_name="cancel")
    return 200, serialize_occurrence(Occurrence.objects.get(pk=pk))


@_endpoint(PUBLISH, ("POST",))
def occurrence_recording(request, key, body, pk):
    values = _parse(body, {"recordingUrl": ("recording_url", "str")}, required=("recordingUrl",))
    try:
        validate_public_https_url(values["recordingUrl"])
    except ValidationError as exc:
        raise _Invalid({"recordingUrl": exc.messages[0]})
    with transaction.atomic():
        occ = Occurrence.objects.select_for_update().get(pk=pk)
        if occ.status == Occurrence.Status.CANCELLED:
            raise _conflict("Recordings cannot be published for cancelled occurrences.")
        preview = {"sessionId": str(occ.pk), "alreadyPublished": occ.recording_published,
                   "effect": "The recording becomes public and recording.published is queued for destinations."}
        _require_publication(request, key, "rest.occurrence.recording", {"sessionId": str(occ.pk), "body": values},
                             preview, state=_iso(occ.recording_published_at))
        publish_recording(occ, values["recordingUrl"])
        audit("occurrence.api_write", api_key=key, obj=occ, action_name="publish_recording")
    return 200, serialize_occurrence(Occurrence.objects.get(pk=pk))


@_endpoint(CONTENT, ("GET", "PATCH"), read_scopes=CONTENT_READ)
def occurrence_waiting_room(request, key, body, pk):
    """Configure the hosted waiting room and its live admission control."""
    from webinars import waiting_room

    occ = Occurrence.objects.select_related("event").get(pk=pk)
    if request.method == "GET":
        return 200, serialize_waiting_room(occ)
    values = _parse(body, WAITING_ROOM_FIELDS)
    if occ.status == Occurrence.Status.CANCELLED and values:
        raise _conflict("Cancelled occurrences have no waiting room.")
    mode = values.get("admissionMode")
    if mode is not None and mode not in Occurrence.Admission.values:
        raise _Invalid({"admissionMode": f"Must be one of: {', '.join(Occurrence.Admission.values)}."})
    try:
        if "enabled" in values or "admissionLeadMinutes" in values:
            waiting_room.configure(
                occ, enabled=values.get("enabled"), lead_minutes=values.get("admissionLeadMinutes"), api_key=key
            )
        if mode is not None:
            waiting_room.set_admission(occ, mode, api_key=key)
    except ValidationError as exc:
        if hasattr(exc, "error_dict"):
            reverse = {model: camel for camel, (model, _kind) in WAITING_ROOM_FIELDS.items()}
            raise _Invalid({reverse.get(k, k): v[0].messages[0] for k, v in exc.error_dict.items()})
        raise _conflict(" ".join(exc.messages))
    return 200, serialize_waiting_room(Occurrence.objects.get(pk=pk))


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


@_endpoint(ATTENDANCE, ("POST",))
def registration_attendance(request, key, body, pk):
    values = _parse(body, {"kind": ("kind", "str")}, required=("kind",))
    kind = values["kind"]
    if kind not in MANUAL_ATTENDANCE_KINDS:
        raise _Invalid({"kind": f"Must be one of: {', '.join(MANUAL_ATTENDANCE_KINDS)}."})
    reg = Registration.objects.select_related("occurrence").get(pk=pk)
    if reg.status != Registration.Status.REGISTERED:
        raise _conflict("Attendance can only be recorded for active registrations.")
    if reg.occurrence.status == Occurrence.Status.CANCELLED:
        raise _conflict("Attendance cannot be recorded for a cancelled occurrence.")
    if kind in (AttendanceEvent.Kind.ATTENDED, AttendanceEvent.Kind.NO_SHOW) and reg.occurrence.starts_at > timezone.now():
        raise _conflict("Attendance can only be recorded once the occurrence has started.")
    if kind == AttendanceEvent.Kind.NO_SHOW and has_current_check_in(reg, reg.occurrence):
        raise _conflict("This registrant checked in through the waiting room; it cannot be a no-show.")
    with transaction.atomic():
        record_attendance(reg, kind, source="api")
        audit("attendance.api_write", api_key=key, obj=reg, kind=kind)
    return 200, {"registrationId": str(reg.pk), "occurrenceId": str(reg.occurrence_id), "attendance": reg.attendance}
