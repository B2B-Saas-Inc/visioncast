"""Attendance imports over the management API (docs/hosting.md, "Attendance import").

Scopes: ``attendance_write`` for everything that writes; ``attendance_write`` or
``config_read`` for reads. A provider pull additionally needs an enabled hosting account
linked to the session (credentials are never exposed). Responses carry masked emails,
display names and ids only: never full addresses, join links or provider payloads.

Applying an import and marking no-shows are two steps, like hosting execute: the first
``POST`` returns 428 with a preview and a single-use ``confirmToken`` bound to the key,
the exact body and the import's state; repeat with header ``Webinar-Confirm-Token``.
The MCP tools reuse this logic with ``attendance_confirmed`` after their own gate.
"""
import uuid

from django.core.exceptions import ValidationError

from webinars import attendance_import as pipeline
from webinars import confirmation
from webinars.models import ApiKey, AttendanceImport, AttendanceImportRow, Occurrence, Registration

from .management import CONFIRM_HEADER, _ConfirmationRequired, _endpoint, _Invalid, _parse

WRITE = (ApiKey.Scope.ATTENDANCE_WRITE,)
READ = WRITE + (ApiKey.Scope.CONFIG_READ,)
APPLY_ACTION = "attendance_import.apply"
NO_SHOWS_ACTION = "attendance_import.no_shows"
SOURCES = ("provider", "file")

_JSON_TO_ROW = {"externalId": "external_id", "email": "email", "name": "name", "joinedAt": "joined_at",
                "leftAt": "left_at", "durationSeconds": "duration_seconds"}

ROW_INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "email": {"type": "string", "maxLength": 254},
        "name": {"type": "string", "maxLength": 300},
        "externalId": {"type": "string", "maxLength": 255, "description": "Provider participant/registrant id."},
        "joinedAt": {"type": "string", "maxLength": 40, "description": "ISO-8601 (UTC assumed when naive)."},
        "leftAt": {"type": "string", "maxLength": 40},
        "durationSeconds": {"type": "integer", "minimum": 0},
    },
}
CREATE_INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["source"],
    "properties": {
        "source": {"type": "string", "enum": list(SOURCES),
                   "description": "provider: read-only pull from the linked hosting provider (Zoom, Riverside). "
                                  "file: the rows given here."},
        "rows": {"type": "array", "maxItems": pipeline.MAX_ROWS, "items": ROW_INPUT_SCHEMA,
                 "description": "Required with source=file; refused with source=provider."},
    },
}
RESOLVE_INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "registrationId": {"type": "string", "format": "uuid",
                           "description": "An active registration of this occurrence; attendance is recorded at once."},
        "ignore": {"type": "boolean", "description": "true: leave the row out (no attendance recorded)."},
    },
}
EMPTY_INPUT_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}}
_N_STR = {"type": ["string", "null"]}
ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "match": {"type": "string", "enum": list(AttendanceImportRow.Match.values)},
        "externalId": _N_STR, "emailMasked": {**_N_STR, "description": "j***@example.com; never the full address."},
        "name": _N_STR, "joinedAt": _N_STR, "leftAt": _N_STR, "durationSeconds": {"type": ["integer", "null"]},
        "registrationId": _N_STR, "appliedAt": _N_STR,
    },
}
IMPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "format": "uuid"}, "occurrenceId": {"type": "string", "format": "uuid"},
        "source": {"type": "string", "enum": list(AttendanceImport.Source.values)},
        "state": {"type": "string", "enum": list(AttendanceImport.State.values)},
        "complete": {"type": "boolean", "description": "False when the provider list was cut short (page cap)."},
        "counts": {"type": "object", "properties": {k: {"type": "integer"} for k in
                                                     ("rows", "matched", "unmatched", "ambiguous", "ignored", "applied")}},
        "noShows": {"type": "object", "properties": {"markedAt": _N_STR, "marked": {"type": "integer"},
                                                      "refused": {"type": "integer"}}},
        "lastError": _N_STR, "operationId": _N_STR, "createdAt": _N_STR, "updatedAt": _N_STR, "appliedAt": _N_STR,
        "rows": {"type": "array", "items": ROW_SCHEMA, "description": "Detail responses only."},
    },
}


def _validation(exc, status=400):
    code = "conflict" if status == 409 else "validation_error"
    if hasattr(exc, "error_dict"):
        fields = {k: "; ".join(m for e in v for m in e.messages) for k, v in exc.error_dict.items()}
        return _Invalid(fields, "Request validation failed." if status == 400 else "; ".join(fields.values()),
                        status=status, code=code)
    return _Invalid(None, "; ".join(exc.messages) or "Invalid request.", status=status, code=code)


def _uuid(value, field):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise _Invalid({field: "Must be a UUID."})


def _occurrence(pk):
    return Occurrence.objects.select_related("event").get(pk=pk)


def import_target(occ, import_id):
    """The import ``import_id`` of ``occ``; unknown and other-occurrence ids get the same 404."""
    imp = AttendanceImport.objects.select_related("occurrence").filter(pk=_uuid(import_id, "importId"),
                                                                        occurrence_id=occ.pk).first()
    if imp is None:
        raise _Invalid(None, "Not found.", status=404, code="not_found")
    return imp


def row_target(imp, row_id):
    row = AttendanceImportRow.objects.filter(pk=_uuid(row_id, "rowId"), attendance_import_id=imp.pk).first()
    if row is None:
        raise _Invalid(None, "Not found.", status=404, code="not_found")
    return row


def rows_from_json(items):
    """camelCase JSON rows -> normalised internal rows (ValidationError -> 400)."""
    if not isinstance(items, list):
        raise _Invalid({"rows": "Must be a list of row objects."})
    converted = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise _Invalid({"rows": f"Row {index}: must be an object."})
        unknown = sorted(k for k in item if k not in _JSON_TO_ROW)
        if unknown:
            raise _Invalid({"rows": f"Row {index}: unknown field(s) {', '.join(unknown)}."})
        converted.append({_JSON_TO_ROW[k]: v for k, v in item.items()})
    try:
        return pipeline.normalise_rows(converted)
    except ValidationError as exc:
        raise _validation(exc)


def create_import(occ, body, *, user=None, api_key=None):
    """Shared by REST and MCP: ``{"source": "provider"}`` or ``{"source": "file", "rows": [...]}``."""
    if not isinstance(body, dict):
        raise _Invalid(None, "Request body must be a JSON object.")
    rows = body.get("rows")
    values = _parse({k: v for k, v in body.items() if k != "rows"}, {"source": ("source", "str")}, required=("source",))
    source = values["source"]
    if source not in SOURCES:
        raise _Invalid({"source": f"Must be one of: {', '.join(SOURCES)}."})
    if source == "provider":
        if "rows" in body:
            raise _Invalid({"rows": "Not accepted with source=provider."})
        try:
            return pipeline.pull_from_provider(occ, user=user, api_key=api_key)
        except ValidationError as exc:
            raise _validation(exc, status=409)
    if rows is None:
        raise _Invalid({"rows": "This field is required with source=file."})
    try:
        return pipeline.create_file_import(occ, rows_from_json(rows), user=user, api_key=api_key)
    except ValidationError as exc:
        raise _validation(exc, status=409 if "source" in getattr(exc, "error_dict", {}) else 400)


def create_preview(occ, body):
    """Side-effect-free preview of ``create_import`` (MCP dry run)."""
    if not isinstance(body, dict):
        raise _Invalid(None, "Request body must be a JSON object.")
    rows = body.get("rows")
    values = _parse({k: v for k, v in body.items() if k != "rows"}, {"source": ("source", "str")}, required=("source",))
    if values["source"] == "provider":
        if "rows" in body:
            raise _Invalid({"rows": "Not accepted with source=provider."})
        return pipeline.pull_preview(occ)
    if values["source"] != "file":
        raise _Invalid({"source": f"Must be one of: {', '.join(SOURCES)}."})
    if rows is None:
        raise _Invalid({"rows": "This field is required with source=file."})
    return pipeline.file_preview(occ, rows_from_json(rows))


def _confirm(request, key, action, material, state, preview):
    """428 + token on the first call; consume on the second. MCP passes ``attendance_confirmed``
    with the state its own gate was issued for."""
    if getattr(request, "attendance_confirmed", False):
        expected = getattr(request, "attendance_expected_state", None)
        if expected is not None and expected != state:
            raise _Invalid(None, "The import changed since the preview; preview again.", status=409, code="conflict")
        return
    headers = getattr(request, "headers", None) or {}
    token = headers.get(CONFIRM_HEADER, "")
    if not token:
        raise _ConfirmationRequired(preview, confirmation.issue(action, key, material, state))
    try:
        confirmation.consume(action, key, material, token, state)
    except confirmation.ConfirmationError as exc:
        raise _Invalid(None, exc.message, status=400, code=exc.code)


@_endpoint(WRITE, ("GET", "POST"), read_scopes=READ)
def occurrence_attendance_imports(request, key, body, pk):
    occ = _occurrence(pk)
    if request.method == "GET":
        pipeline.recover_stale_imports()
        imports = AttendanceImport.objects.filter(occurrence_id=occ.pk).order_by("-created_at")[:100]
        return 200, {"occurrenceId": str(occ.pk), "results": [pipeline.serialize_import(i) for i in imports]}
    imp = create_import(occ, body, api_key=key)
    return 201, pipeline.serialize_import(imp, rows=True)


@_endpoint(WRITE, ("GET",), read_scopes=READ)
def attendance_import_detail(request, key, body, pk, import_id):
    occ = _occurrence(pk)
    pipeline.recover_stale_imports()
    return 200, pipeline.serialize_import(import_target(occ, import_id), rows=True)


@_endpoint(WRITE, ("POST",))
def attendance_import_apply(request, key, body, pk, import_id):
    occ = _occurrence(pk)
    _parse(body, {})
    imp = import_target(occ, import_id)
    preview = pipeline.apply_preview(imp)
    if preview["problems"]:
        raise _Invalid({"state": "; ".join(preview["problems"])}, "This import cannot be applied.", status=409, code="conflict")
    state = pipeline.import_state(imp)
    _confirm(request, key, APPLY_ACTION, {"occurrenceId": str(occ.pk), "importId": str(imp.pk)}, state, preview)
    try:
        imp = pipeline.apply_import(imp, api_key=key, expected_state=state)
    except ValidationError as exc:
        raise _validation(exc, status=409)
    return 200, pipeline.serialize_import(imp, rows=True)


@_endpoint(WRITE, ("POST",))
def attendance_import_row_resolve(request, key, body, pk, import_id, row_id):
    occ = _occurrence(pk)
    values = _parse(body, {"registrationId": ("registration", "str"), "ignore": ("ignore", "bool")})
    imp = import_target(occ, import_id)
    row = row_target(imp, row_id)
    ignore = bool(values.get("ignore"))
    registration = None
    if ignore and "registrationId" in values:
        raise _Invalid({"registrationId": "Not accepted with ignore=true."})
    if not ignore:
        if "registrationId" not in values:
            raise _Invalid({"registrationId": "Give a registrationId or ignore=true."})
        registration = Registration.objects.filter(pk=_uuid(values["registrationId"], "registrationId"),
                                                   occurrence_id=occ.pk).first()
        if registration is None:
            raise _Invalid(None, "Not found.", status=404, code="not_found")
    try:
        row = pipeline.resolve_row(imp, row, registration=registration, ignore=ignore, api_key=key)
    except ValidationError as exc:
        raise _validation(exc, status=409)
    return 200, {"importId": str(imp.pk), "row": pipeline.serialize_row(row)}


@_endpoint(WRITE, ("POST",))
def attendance_import_no_shows(request, key, body, pk, import_id):
    occ = _occurrence(pk)
    _parse(body, {})
    imp = import_target(occ, import_id)
    preview = pipeline.no_show_preview(imp)
    if preview["problems"]:
        raise _Invalid({"state": "; ".join(preview["problems"])}, "No-shows cannot be marked now.", status=409, code="conflict")
    state = pipeline.import_state(imp)
    _confirm(request, key, NO_SHOWS_ACTION, {"occurrenceId": str(occ.pk), "importId": str(imp.pk)}, state, preview)
    try:
        imp = pipeline.mark_no_shows(imp, api_key=key, expected_state=state)
    except ValidationError as exc:
        raise _validation(exc, status=409)
    return 200, pipeline.serialize_import(imp)
