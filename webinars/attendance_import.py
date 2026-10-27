"""Attendance import pipeline (docs/hosting.md, "Attendance import").

One ingestion path with two sources:

* **provider pull** (``pull_from_provider``): a durable, read-only ``ProvisioningOperation``
  (action ``attendance``) is committed before the call, the adapter's ``fetch_attendance``
  runs outside any lock, and late outcomes are fenced. Never automatic: only a person or
  an API key asks for it. Unsupported providers are refused, never simulated.
* **file / rows** (``create_file_import``): operator-supplied rows (JSON over REST/MCP, CSV
  in the studio) parsed server-side; the raw file is never stored.

Both produce an ``AttendanceImport`` with deduplicated ``AttendanceImportRow``s matched to
active registrations (provider external id first, then normalised email; several
candidates -> ``ambiguous``, never a guess). Nothing is recorded until a person confirms:

* ``apply_import``: every matched/resolved row whose registration is not already
  ``attended`` goes through ``scheduling.record_attendance`` (``attendance.recorded`` fires
  once per registration); already-attended registrations and already-applied rows are
  skipped, so re-applying or re-importing never re-emits.
* ``resolve_row``: a person maps an unmatched/ambiguous row to a registration of the same
  occurrence (attendance is recorded the same way) or ignores it.
* ``mark_no_shows``: a separate confirmable step, only after apply and after the session
  ended; composes with ``waiting_room.finalize_attendance`` (current check-ins are never
  turned into no-shows) and reports the registrations ``record_attendance`` refused.

Stored PII is the minimum for matching and review (email key, masked email, display
name); ``purge_personal_data`` blanks it after retention and on person erasure. Audit
entries carry ids and counts only.
"""
import csv
import io
import re
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from webinars.audit import audit
from webinars.hosting import base, service
from webinars.models import (
    AttendanceEvent,
    AttendanceImport,
    AttendanceImportRow,
    CheckIn,
    HostedSession,
    Occurrence,
    ProviderRegistrant,
    ProvisioningOperation,
    Registration,
)
from webinars.normalization import email_key as _email_key

MAX_ROWS = 5000
MAX_FILE_BYTES = 1024 * 1024
MAX_NAME = 300
MAX_EXTERNAL_ID = 255
STALE_IN_FLIGHT_SECONDS = service.STALE_IN_FLIGHT_SECONDS
ROW_KEYS = ("external_id", "email", "name", "joined_at", "left_at", "duration_seconds")
REVIEWABLE = (AttendanceImportRow.Match.UNMATCHED, AttendanceImportRow.Match.AMBIGUOUS)
COUNTED = (AttendanceImportRow.Match.MATCHED, AttendanceImportRow.Match.RESOLVED)

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z0-9-]{2,24}\.?$")
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9_.:@+/=-]{1,255}$")
_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S",
               "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M")

# CSV header aliases (lower-cased, spaces collapsed). Zoom participant/attendee reports,
# Wistia and StreamYard exports use one of these per column.
_HEADERS = {
    "email": ("email", "user email", "user_email", "email address", "attendee email", "participant email",
              "e-mail", "email (user)"),
    "name": ("name", "user name", "user_name", "attendee name", "participant name", "full name", "display name",
             "name (original name)", "user name (original name)"),
    "first_name": ("first name", "first_name", "firstname"),
    "last_name": ("last name", "last_name", "lastname", "surname"),
    "joined_at": ("joined_at", "join time", "join_time", "joined", "joined at", "first joined", "join date",
                  "joined time", "start time"),
    "left_at": ("left_at", "leave time", "leave_time", "left", "left at", "last left", "leave date", "left time",
                "end time"),
    "duration": ("duration", "duration_seconds", "duration (seconds)", "duration (minutes)", "duration minutes",
                 "time in session (minutes)", "time in session", "minutes attended", "watch time",
                 "watch time (minutes)", "total duration", "duration (min)"),
    "external_id": ("external_id", "id", "participant id", "attendee id", "registrant id", "user id", "user_id",
                    "participant_id"),
    "attended": ("attended", "has attended", "joined?", "attendance"),
}
_FALSE = {"no", "false", "0", "n", "not attended", "did not attend", "absent"}


def _actor(user):
    return user if (user is not None and getattr(user, "is_authenticated", False)) else None


def _iso(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def mask_email(email):
    """``j***@example.com``; never the full address."""
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "—"
    return f"{local[:1]}***@{domain[:200]}"


# ---------------------------------------------------------------------------
# Row normalisation (adapter rows, JSON rows, CSV rows all end up here)
# ---------------------------------------------------------------------------


def parse_datetime(value):
    """Aware UTC datetime from an ISO-8601 or common export string; None when unusable."""
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else value.replace(tzinfo=dt_timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 40:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in _DT_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def parse_duration(value, *, minutes=False):
    """Seconds from an int, a numeric string, ``H:MM:SS`` / ``MM:SS``; None when unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        seconds = value * 60 if minutes else value
        return seconds if 0 <= seconds <= 10 ** 7 else None
    if isinstance(value, float):
        return parse_duration(int(round(value * 60 if minutes else value)))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
            parts = [int(p) for p in parts]
            seconds = parts[-1] + parts[-2] * 60 + (parts[0] * 3600 if len(parts) == 3 else 0)
            return seconds if seconds <= 10 ** 7 else None
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return parse_duration(int(round(number * 60 if minutes else number)))


def _clean_row(item, index):
    """One internal row from a dict with ``ROW_KEYS``; raises ValidationError on wrong types."""
    if not isinstance(item, dict):
        raise ValidationError({"rows": [f"Row {index}: must be an object."]})
    unknown = sorted(k for k in item if k not in ROW_KEYS)
    if unknown:
        raise ValidationError({"rows": [f"Row {index}: unknown field(s) {', '.join(unknown)}."]})
    for key in ("external_id", "email", "name", "joined_at", "left_at"):
        value = item.get(key)
        if value is not None and not isinstance(value, str):
            raise ValidationError({"rows": [f"Row {index}: {key} must be text."]})
    duration = item.get("duration_seconds")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or duration < 0):
        raise ValidationError({"rows": [f"Row {index}: duration_seconds must be a non-negative integer."]})
    email = (item.get("email") or "").strip()
    name = (item.get("name") or "").strip()[:MAX_NAME]
    ext = (item.get("external_id") or "").strip()
    if ext and not _EXTERNAL_ID.match(ext):
        ext = ""
    key = _email_key(email) if _EMAIL.match(email) else ""
    row = {
        "external_id": ext[:MAX_EXTERNAL_ID],
        "email_key": key[:254],
        "email_masked": mask_email(email) if email else "",
        "name": name,
        "joined_at": parse_datetime(item.get("joined_at")),
        "left_at": parse_datetime(item.get("left_at")),
        "duration_seconds": parse_duration(duration),
    }
    if not row["external_id"] and not row["email_key"] and not email:
        return None
    return row


def normalise_rows(items):
    """Validate and normalise rows (adapter output or operator JSON). Drops empty rows."""
    if not isinstance(items, list):
        raise ValidationError({"rows": ["rows must be a list."]})
    if len(items) > MAX_ROWS:
        raise ValidationError({"rows": [f"At most {MAX_ROWS} rows per import."]})
    out = []
    for index, item in enumerate(items, start=1):
        row = _clean_row(item, index)
        if row is not None:
            out.append(row)
    return out


def _norm_header(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower().strip('"'))


def _header_map(headers):
    found = {}
    for position, raw in enumerate(headers):
        name = _norm_header(raw)
        for key, aliases in _HEADERS.items():
            if key not in found and name in aliases:
                found[key] = (position, name)
    return found


def parse_csv(data):
    """Rows from an uploaded CSV (bytes). Tolerates Zoom/Wistia/StreamYard header names and a
    report preamble before the header line. Enforces the size and row limits."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValidationError({"file": ["Upload a CSV file."]})
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError({"file": ["The file is larger than 1 MB."]})
    try:
        text = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError:
        text = bytes(data).decode("latin-1")
    lines = list(csv.reader(io.StringIO(text)))
    header_index = None
    mapping = {}
    for i, line in enumerate(lines[:50]):
        mapping = _header_map(line)
        if "email" in mapping or ("external_id" in mapping and ("name" in mapping or "first_name" in mapping)):
            header_index = i
            break
    if header_index is None:
        raise ValidationError({"file": ["No header row with an email column was found."]})
    body = [line for line in lines[header_index + 1:] if any(cell.strip() for cell in line)]
    if len(body) > MAX_ROWS:
        raise ValidationError({"file": [f"At most {MAX_ROWS} rows per import."]})
    minutes = "duration" in mapping and ("second" not in mapping["duration"][1])

    def cell(line, key):
        if key not in mapping:
            return ""
        position = mapping[key][0]
        return line[position].strip() if position < len(line) else ""

    items = []
    for line in body:
        if "attended" in mapping and cell(line, "attended").lower() in _FALSE:
            continue
        name = cell(line, "name") or " ".join(p for p in (cell(line, "first_name"), cell(line, "last_name")) if p)
        items.append({
            "external_id": cell(line, "external_id"),
            "email": cell(line, "email"),
            "name": name,
            "joined_at": cell(line, "joined_at"),
            "left_at": cell(line, "left_at"),
            "duration_seconds": parse_duration(cell(line, "duration"), minutes=minutes) if "duration" in mapping else None,
        })
    return normalise_rows(items)


# ---------------------------------------------------------------------------
# Matching and deduplication (pure given the occurrence's current registrations)
# ---------------------------------------------------------------------------


def _candidates(occ):
    """``(external_id -> {registration ids}, email_key -> {registration ids})`` for active
    registrations of ``occ``; external ids come from this session's ProviderRegistrants."""
    by_ext, by_email = {}, {}
    session = service.session_for(occ)
    if session is not None:
        pairs = ProviderRegistrant.objects.filter(
            session=session, registration__occurrence_id=occ.pk, registration__status=Registration.Status.REGISTERED,
        ).exclude(external_id="").values_list("external_id", "registration_id")
        for ext, reg_id in pairs:
            by_ext.setdefault(ext, set()).add(reg_id)
    pairs = Registration.objects.filter(
        occurrence_id=occ.pk, status=Registration.Status.REGISTERED, person__email_key__isnull=False,
    ).exclude(person__email_key="").values_list("pk", "person__email_key")
    for reg_id, key in pairs:
        by_email.setdefault(key, set()).add(reg_id)
    return by_ext, by_email


def _merge(target, row):
    target["external_id"] = target["external_id"] or row["external_id"]
    target["email_key"] = target["email_key"] or row["email_key"]
    target["email_masked"] = target["email_masked"] or row["email_masked"]
    target["name"] = target["name"] or row["name"]
    if row["joined_at"] and (target["joined_at"] is None or row["joined_at"] < target["joined_at"]):
        target["joined_at"] = row["joined_at"]
    if row["left_at"] and (target["left_at"] is None or row["left_at"] > target["left_at"]):
        target["left_at"] = row["left_at"]
    if row["duration_seconds"] is not None:
        target["duration_seconds"] = (target["duration_seconds"] or 0) + row["duration_seconds"]


def match_rows(occ, rows):
    """Match normalised rows against ``occ``'s active registrations and merge rows for the
    same person. Returns rows with ``match`` and ``registration_id`` added (no writes)."""
    by_ext, by_email = _candidates(occ)
    merged = {}
    order = []
    for row in rows:
        ext_hits = by_ext.get(row["external_id"]) if row["external_id"] else None
        email_hits = by_email.get(row["email_key"]) if row["email_key"] else None
        candidates = set(ext_hits or ()) or set(email_hits or ())
        if ext_hits and email_hits and set(ext_hits) != set(email_hits):
            candidates = set(ext_hits) | set(email_hits)
        if len(candidates) == 1:
            match, reg_id = AttendanceImportRow.Match.MATCHED, next(iter(candidates))
            key = ("reg", reg_id)
        elif candidates:
            match, reg_id = AttendanceImportRow.Match.AMBIGUOUS, None
            key = ("email", row["email_key"]) if row["email_key"] else ("ext", row["external_id"])
        else:
            match, reg_id = AttendanceImportRow.Match.UNMATCHED, None
            key = ("email", row["email_key"]) if row["email_key"] else ("ext", row["external_id"] or uuid.uuid4().hex)
        if key in merged:
            _merge(merged[key], row)
            continue
        merged[key] = {**row, "match": match, "registration_id": reg_id}
        order.append(key)
    return [merged[k] for k in order]


def match_counts(matched):
    counts = {"rows": len(matched), "matched": 0, "unmatched": 0, "ambiguous": 0, "ignored": 0}
    for row in matched:
        counts[row["match"]] += 1
    return counts


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def _store_rows(imp, occ, rows, now):
    matched = match_rows(occ, rows)
    AttendanceImportRow.objects.bulk_create([
        AttendanceImportRow(
            attendance_import=imp, external_id=r["external_id"], email_key=r["email_key"], email_masked=r["email_masked"],
            display_name=r["name"], joined_at=r["joined_at"], left_at=r["left_at"], duration_seconds=r["duration_seconds"],
            match=r["match"], registration_id=r["registration_id"], created_at=now,
        )
        for r in matched
    ])
    counts = match_counts(matched)
    imp.row_count = counts["rows"]
    imp.matched_count = counts["matched"]
    imp.unmatched_count = counts["unmatched"]
    imp.ambiguous_count = counts["ambiguous"]
    imp.state = AttendanceImport.State.NEEDS_REVIEW
    return counts


def create_file_import(occ, rows, *, user=None, api_key=None):
    """Store operator-supplied (already normalised) rows as a reviewable import."""
    if occ.status == Occurrence.Status.CANCELLED:
        raise ValidationError({"source": ["The session is cancelled."]})
    if not rows:
        raise ValidationError({"rows": ["No usable rows: each row needs an email or an external id."]})
    now = timezone.now()
    with transaction.atomic():
        imp = AttendanceImport.objects.create(
            occurrence=occ, source=AttendanceImport.Source.FILE, state=AttendanceImport.State.PENDING,
            created_by_user=_actor(user), created_by_api_key=api_key, created_at=now,
        )
        counts = _store_rows(imp, occ, rows, now)
        imp.save()
        audit("attendance_import.created", user=user, api_key=api_key, obj=imp, source=imp.source, **counts)
    return imp


def file_preview(occ, rows):
    """Side-effect-free matching preview for operator rows."""
    counts = match_counts(match_rows(occ, rows))
    return {"source": "file", "occurrenceId": str(occ.pk), "counts": counts,
            "effect": "Stores the rows as a reviewable import. Nothing is recorded until the import is applied."}


# ---- provider pull (durable, read-only)


def pull_problems(occ):
    """``(session, problems)``: why a provider pull cannot run now (no writes)."""
    problems = []
    session = service.session_for(occ)
    if session is None or session.state == HostedSession.State.DETACHED:
        problems.append("The session is not linked to a hosting provider.")
        return session, problems
    adapter = service.adapter_for(session)
    if not adapter.capabilities.attendance_import:
        problems.append(f"{adapter.label} does not support attendance import here: import a file instead.")
        return session, problems
    if session.state not in (HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE) or not session.external_id:
        problems.append("The session is not linked to a provider session (attach or create it first).")
    if session.account is None or not session.account.enabled:
        problems.append("The hosting account is disabled.")
    problems.extend(adapter.allowlist_problems())
    if occ.status == Occurrence.Status.CANCELLED:
        problems.append("The session is cancelled.")
    if AttendanceImport.objects.filter(occurrence_id=occ.pk, state=AttendanceImport.State.IN_FLIGHT).exists():
        problems.append("A provider pull is still in flight; wait for it to finish.")
    return session, list(dict.fromkeys(problems))


def pull_preview(occ):
    """What a provider pull would request (credentials redacted); its only write is lost-pull recovery."""
    recover_stale_imports()
    session, problems = pull_problems(occ)
    plans = []
    if session is not None and not problems:
        adapter = service.adapter_for(session)
        try:
            plans = [p.as_preview() for p in adapter.plan("attendance", occ, session.account.config or {}, session.external_id)]
        except base.Unsupported:
            pass
    return {
        "source": "provider", "occurrenceId": str(occ.pk),
        "provider": session.provider if session is not None else None,
        "externalId": session.external_id if session is not None and session.external_id else None,
        "plans": plans, "problems": problems,
        "effect": "Read-only: fetches who attended from the provider into a reviewable import. Nothing is recorded "
                  "until the import is applied.",
    }


def pull_from_provider(occ, *, user=None, api_key=None, transport=None):
    """Durable provider pull. Step A commits the in-flight operation and import, step B calls
    the provider outside any lock, step C records the (fenced) outcome."""
    recover_stale_imports()
    session, problems = pull_problems(occ)
    if problems:
        raise ValidationError({"source": problems})
    adapter = service.adapter_for(session)
    account = session.account
    try:
        secrets = service.load_secrets(account)
    except Exception:  # DecryptionError, bad JSON, missing/rotated master key
        raise ValidationError({"credentials": ["Stored credentials could not be decrypted; set them again."]})
    if adapter.validate_credentials(secrets):
        raise ValidationError({"credentials": ["Some credentials are not set."]})
    external_id = session.external_id
    now = timezone.now()
    with transaction.atomic():
        locked = HostedSession.objects.select_for_update().get(pk=session.pk)
        if (locked.state not in (HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE)
                or locked.account_id != account.pk or locked.external_id != external_id):
            raise ValidationError({"source": ["The session link changed; preview again."]})
        if AttendanceImport.objects.select_for_update().filter(
                occurrence_id=occ.pk, state=AttendanceImport.State.IN_FLIGHT).exists():
            raise ValidationError({"source": ["A provider pull is still in flight; wait for it to finish."]})
        op = ProvisioningOperation.objects.create(
            session=locked, action=ProvisioningOperation.Action.ATTENDANCE, status=ProvisioningOperation.Status.IN_FLIGHT,
            idempotency_key=f"{locked.pk}:attendance:{uuid.uuid4()}", schedule_generation=occ.schedule_generation,
            actor_user=_actor(user), actor_api_key=api_key, created_at=now,
        )
        imp = AttendanceImport.objects.create(
            occurrence=occ, source=locked.provider, state=AttendanceImport.State.IN_FLIGHT, operation=op,
            created_by_user=_actor(user), created_by_api_key=api_key, created_at=now,
        )
        audit("attendance_import.pull_started", user=user, api_key=api_key, obj=imp, provider=locked.provider,
              operation=str(op.pk))
    try:
        outcome = adapter.fetch_attendance(occ, account.config or {}, secrets, external_id,
                                           transport=transport or base.default_transport)
    except base.Unsupported:
        outcome = base.Outcome("failed", "attendance: unsupported")
    except Exception as exc:  # defensive: a pull is read-only, so a retry is always safe
        outcome = base.Outcome("failed", f"attendance: internal_error: {type(exc).__name__}")
    return _record_pull(occ, imp.pk, op.pk, outcome, user=user, api_key=api_key)


def _record_pull(occ, import_id, op_id, outcome, *, user, api_key):
    now = timezone.now()
    rows, complete = [], True
    if outcome.kind == "succeeded":
        data = outcome.data if isinstance(outcome.data, dict) else {}
        complete = bool(data.get("complete", True))
        try:
            rows = normalise_rows(list(data.get("rows") or [])[:MAX_ROWS])
        except ValidationError:
            outcome = base.Outcome("failed", "attendance: response_rows_invalid", outcome.status_code)
        if len(data.get("rows") or []) > MAX_ROWS:
            complete = False
    with transaction.atomic():
        imp = AttendanceImport.objects.select_for_update().get(pk=import_id)
        op = ProvisioningOperation.objects.select_for_update().get(pk=op_id)
        if imp.state != AttendanceImport.State.IN_FLIGHT or op.status != ProvisioningOperation.Status.IN_FLIGHT:
            # Fenced: recovered as lost (or otherwise moved on). Note it; never store the rows.
            op.error = (f"{op.error} [late outcome: {outcome.kind}]".strip())[:300]
            op.status_code = outcome.status_code
            if op.finished_at is None:
                op.finished_at = now
            op.save(update_fields=["error", "status_code", "finished_at"])
            audit("attendance_import.late_outcome", user=user, api_key=api_key, obj=imp, outcome=outcome.kind,
                  status_code=outcome.status_code)
            return imp
        op.status_code = outcome.status_code
        op.finished_at = now
        op.error = (outcome.error or "")[:300]
        counts = {}
        if outcome.kind == "succeeded":
            op.status = ProvisioningOperation.Status.SUCCEEDED
            imp.complete = complete
            imp.last_error = "" if complete else "The provider list was cut short (page cap); rows may be missing."
            counts = _store_rows(imp, occ, rows, now)
        else:
            # A pull is read-only: whatever happened, nothing was changed at the provider and
            # a retry is safe, so the operation is a definitive failure (never "unknown").
            op.status = ProvisioningOperation.Status.FAILED
            imp.state = AttendanceImport.State.FAILED
            imp.last_error = (outcome.error or "attendance: failed")[:300]
        op.save()
        imp.save()
        audit("attendance_import.pull_finished", user=user, api_key=api_key, obj=imp, outcome=outcome.kind,
              status_code=outcome.status_code, complete=complete, **counts)
    return imp


def recover_stale_imports(now=None):
    """Lost in-flight pulls (request timed out, process died) become ``failed``: pulls are
    read-only, so a retry is always safe. Their operations are closed as failed too, so they
    are never mistaken for reconcilable unknown outcomes. Returns the number changed."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=STALE_IN_FLIGHT_SECONDS)
    changed = 0
    stale = list(AttendanceImport.objects.filter(state=AttendanceImport.State.IN_FLIGHT, created_at__lt=cutoff)
                 .values_list("pk", flat=True)[:50])
    for pk in stale:
        with transaction.atomic():
            imp = AttendanceImport.objects.select_for_update().filter(pk=pk, state=AttendanceImport.State.IN_FLIGHT).first()
            if imp is None:
                continue
            imp.state = AttendanceImport.State.FAILED
            imp.last_error = "attendance: no outcome recorded (lost in flight); safe to retry"
            imp.save(update_fields=["state", "last_error", "updated_at"])
            if imp.operation_id:
                ProvisioningOperation.objects.filter(pk=imp.operation_id, status=ProvisioningOperation.Status.IN_FLIGHT).update(
                    status=ProvisioningOperation.Status.FAILED, error=imp.last_error[:300], finished_at=now)
            audit("attendance_import.lost", obj=imp, operation=str(imp.operation_id) if imp.operation_id else None)
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# Apply, review, no-shows
# ---------------------------------------------------------------------------


def import_state(imp):
    """Confirmation fingerprint: any apply, resolve, ignore, no-show pass or outcome changes it."""
    return f"{imp.state}:{imp.updated_at.isoformat()}:{imp.applied_count}:{imp.ignored_count}:{imp.no_shows_marked_at}"


def _check_expected(imp, expected_state):
    if expected_state is not None and import_state(imp) != expected_state:
        raise ValidationError({"state": ["The import changed since the preview; preview again."]})


def _applicable_rows(imp):
    return imp.rows.filter(match__in=COUNTED, applied_at__isnull=True).select_related("registration")


def apply_preview(imp):
    """Counts of what ``apply_import`` would record (no writes)."""
    to_apply = already = 0
    for row in _applicable_rows(imp):
        reg = row.registration
        if reg is None or reg.occurrence_id != imp.occurrence_id or reg.status != Registration.Status.REGISTERED:
            continue
        if reg.attendance == Registration.Attendance.ATTENDED:
            already += 1
        else:
            to_apply += 1
    problems = []
    if imp.state not in (AttendanceImport.State.NEEDS_REVIEW, AttendanceImport.State.APPLIED):
        problems.append(f"This import cannot be applied (state {imp.state}).")
    if imp.occurrence.status == Occurrence.Status.CANCELLED:
        problems.append("The session is cancelled.")
    return {
        "importId": str(imp.pk), "occurrenceId": str(imp.occurrence_id), "source": imp.source, "state": imp.state,
        "complete": imp.complete, "matched": imp.matched_count, "unmatched": imp.unmatched_count,
        "ambiguous": imp.ambiguous_count, "ignored": imp.ignored_count, "toApply": to_apply, "alreadyAttended": already,
        "problems": problems,
        "effect": f"Records 'attended' for {to_apply} registration(s) via record_attendance (attendance.recorded is "
                  f"delivered once per registration); {already} already attended are skipped. Unmatched and ambiguous "
                  "rows stay for review.",
    }


def apply_import(imp, *, user=None, api_key=None, expected_state=None):
    now = timezone.now()
    with transaction.atomic():
        imp = AttendanceImport.objects.select_for_update().select_related("occurrence").get(pk=imp.pk)
        _check_expected(imp, expected_state)
        preview = apply_preview(imp)
        if preview["problems"]:
            raise ValidationError({"state": preview["problems"]})
        applied = skipped = 0
        for row in _applicable_rows(imp):
            reg = row.registration
            if reg is None or reg.occurrence_id != imp.occurrence_id or reg.status != Registration.Status.REGISTERED:
                continue
            if reg.attendance != Registration.Attendance.ATTENDED:
                from webinars.scheduling import record_attendance

                record_attendance(reg, AttendanceEvent.Kind.ATTENDED, user=_actor(user), source=f"import:{imp.source}")
                applied += 1
            else:
                skipped += 1
            row.applied_at = now
            row.save(update_fields=["applied_at", "updated_at"])
        imp.applied_count += applied
        imp.state = AttendanceImport.State.APPLIED
        imp.applied_at = imp.applied_at or now
        imp.save()
        audit("attendance_import.applied", user=user, api_key=api_key, obj=imp, applied=applied, skipped=skipped)
    return imp


def resolve_row(imp, row, *, registration=None, ignore=False, user=None, api_key=None, expected_state=None):
    """Map an unmatched/ambiguous row to a registration of the same occurrence (attendance is
    recorded at once, via ``record_attendance``) or ignore it."""
    now = timezone.now()
    with transaction.atomic():
        imp = AttendanceImport.objects.select_for_update().get(pk=imp.pk)
        row = AttendanceImportRow.objects.select_for_update().filter(pk=row.pk, attendance_import_id=imp.pk).first()
        if row is None:
            raise ValidationError({"rowId": ["Unknown row."]})
        _check_expected(imp, expected_state)
        if imp.state not in (AttendanceImport.State.NEEDS_REVIEW, AttendanceImport.State.APPLIED):
            raise ValidationError({"state": [f"Rows of this import cannot be resolved (state {imp.state})."]})
        if row.match not in REVIEWABLE:
            raise ValidationError({"rowId": [f"This row is already {row.match}."]})
        previous = row.match
        if ignore:
            row.match = AttendanceImportRow.Match.IGNORED
            imp.ignored_count += 1
        else:
            if registration is None or registration.occurrence_id != imp.occurrence_id:
                raise ValidationError({"registrationId": ["Choose a registration of this session."]})
            reg = Registration.objects.select_for_update(of=("self",)).select_related("occurrence").get(pk=registration.pk)
            if reg.status != Registration.Status.REGISTERED:
                raise ValidationError({"registrationId": ["Only active registrations can be marked attended."]})
            row.match = AttendanceImportRow.Match.RESOLVED
            row.registration = reg
            if reg.attendance != Registration.Attendance.ATTENDED:
                from webinars.scheduling import record_attendance

                record_attendance(reg, AttendanceEvent.Kind.ATTENDED, user=_actor(user), source=f"import:{imp.source}")
                imp.applied_count += 1
            row.applied_at = now
            imp.matched_count += 1
        if previous == AttendanceImportRow.Match.UNMATCHED:
            imp.unmatched_count = max(0, imp.unmatched_count - 1)
        else:
            imp.ambiguous_count = max(0, imp.ambiguous_count - 1)
        row.save()
        imp.save()
        audit("attendance_import.row_resolved", user=user, api_key=api_key, obj=imp, row=str(row.pk), ignored=bool(ignore),
              registration=str(row.registration_id) if row.registration_id else None, previous=previous)
    return row


def _remaining(occ):
    return Registration.objects.filter(occurrence_id=occ.pk, status=Registration.Status.REGISTERED,
                                       attendance=Registration.Attendance.UNKNOWN)


def no_show_preview(imp, now=None):
    """What ``mark_no_shows`` would do (no writes)."""
    from webinars import waiting_room

    now = now or timezone.now()
    occ = imp.occurrence
    state = waiting_room.room_state(occ, now).state
    remaining = _remaining(occ)
    checked = CheckIn.objects.filter(occurrence=occ, admission_generation=occ.admission_generation,
                                     registration__in=remaining).count()
    problems = []
    if imp.state != AttendanceImport.State.APPLIED:
        problems.append("Apply the import before marking no-shows.")
    if imp.no_shows_marked_at is not None:
        problems.append("No-shows were already marked for this import.")
    if occ.status == Occurrence.Status.CANCELLED:
        problems.append("The session is cancelled.")
    elif state != waiting_room.ENDED:
        problems.append("The session has not ended yet.")
    finalize = bool(occ.waiting_room_enabled and occ.attendance_finalized_at is None)
    return {
        "importId": str(imp.pk), "occurrenceId": str(imp.occurrence_id), "state": imp.state, "roomState": state,
        "remaining": remaining.count(), "checkedIn": checked, "waitingRoomFinalize": finalize, "problems": problems,
        "effect": ("Every remaining active registration without attendance becomes no_show via record_attendance "
                   "(attendance.recorded once each). Registrants who checked in through the waiting room are never "
                   "marked no_show: they are reported as refused"
                   + (" (the room is finalized first, so they become attended)." if finalize else ".")),
    }


def mark_no_shows(imp, *, user=None, api_key=None, expected_state=None, now=None):
    from webinars import waiting_room

    now = now or timezone.now()
    with transaction.atomic():
        imp = AttendanceImport.objects.select_for_update().select_related("occurrence").get(pk=imp.pk)
        _check_expected(imp, expected_state)
        preview = no_show_preview(imp, now)
        if preview["problems"]:
            raise ValidationError({"state": preview["problems"]})
        occ = Occurrence.objects.select_for_update().get(pk=imp.occurrence_id)
        before = Registration.objects.filter(occurrence_id=occ.pk, attendance=Registration.Attendance.NO_SHOW).count()
        finalized = False
        if occ.waiting_room_enabled and occ.attendance_finalized_at is None:
            # The room's own rule first: current check-ins become attended, the rest no_show.
            finalized = waiting_room.finalize_attendance(occ, now) is not None
        from webinars.scheduling import record_attendance

        refused = 0
        for reg in list(_remaining(occ).select_related("occurrence")):
            try:
                record_attendance(reg, AttendanceEvent.Kind.NO_SHOW, user=_actor(user), source=f"import:{imp.source}")
            except ValidationError as exc:
                if getattr(exc, "code", None) != "checked_in":
                    raise
                refused += 1
        after = Registration.objects.filter(occurrence_id=occ.pk, attendance=Registration.Attendance.NO_SHOW).count()
        imp.no_show_count = max(0, after - before)
        imp.no_show_refused_count = refused
        imp.no_shows_marked_at = now
        imp.save()
        audit("attendance_import.no_shows", user=user, api_key=api_key, obj=imp, marked=imp.no_show_count, refused=refused,
              room_finalized=finalized)
    return imp


# ---------------------------------------------------------------------------
# Projections (never emails, never links)
# ---------------------------------------------------------------------------


def serialize_row(row):
    return {
        "id": str(row.pk), "match": row.match, "externalId": row.external_id or None, "emailMasked": row.email_masked or None,
        "name": row.display_name or None, "joinedAt": _iso(row.joined_at), "leftAt": _iso(row.left_at),
        "durationSeconds": row.duration_seconds, "registrationId": str(row.registration_id) if row.registration_id else None,
        "appliedAt": _iso(row.applied_at),
    }


def serialize_import(imp, *, rows=False):
    data = {
        "id": str(imp.pk), "occurrenceId": str(imp.occurrence_id), "source": imp.source, "state": imp.state,
        "complete": imp.complete,
        "counts": {"rows": imp.row_count, "matched": imp.matched_count, "unmatched": imp.unmatched_count,
                   "ambiguous": imp.ambiguous_count, "ignored": imp.ignored_count, "applied": imp.applied_count},
        "noShows": {"markedAt": _iso(imp.no_shows_marked_at), "marked": imp.no_show_count,
                    "refused": imp.no_show_refused_count},
        "lastError": imp.last_error or None,
        "operationId": str(imp.operation_id) if imp.operation_id else None,
        "createdAt": _iso(imp.created_at), "updatedAt": _iso(imp.updated_at), "appliedAt": _iso(imp.applied_at),
    }
    if rows:
        data["rows"] = [serialize_row(r) for r in imp.rows.all()[:MAX_ROWS]]
    return data


def purge_rows(queryset, now=None):
    """Blank the personal data of rows (retention / erasure). Returns the number changed."""
    now = now or timezone.now()
    return queryset.exclude(email_key="", email_masked="", display_name="", external_id="").update(
        email_key="", email_masked="", display_name="", external_id="", updated_at=now)
