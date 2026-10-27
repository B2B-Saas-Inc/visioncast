"""Reminders, cancellation, rescheduling, recording publication and attendance.

All state changes and their outbox rows are written in one transaction.
Attendance is only ever recorded explicitly; it is never inferred from RSVPs.
"""
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from webinars.audit import audit
from webinars.models import AttendanceEvent, Occurrence, OutboxMessage, Registration, ReminderSchedule
from webinars.timeutils import local_to_utc
from webinars.validation import validate_public_https_url

PROCESS_BATCH_SIZE = 500


def _enqueue(event_type, **kwargs):
    from webinars.outbox import enqueue_event

    return enqueue_event(event_type, **kwargs)


def _offsets():
    raw = getattr(settings, "REMINDER_OFFSETS_MINUTES", None)
    if raw is None:
        raw = [1440, 60]
    return sorted({int(o) for o in raw if int(o) > 0}, reverse=True)


def _grace():
    return timedelta(minutes=int(getattr(settings, "REMINDER_LATE_GRACE_MINUTES", 15)))


def _actor(user):
    return user if (user is not None and getattr(user, "is_authenticated", False)) else None


def _sync(target, source, fields):
    for name in fields:
        setattr(target, name, getattr(source, name))


def _registered(occurrence):
    return Registration.objects.filter(occurrence=occurrence, status=Registration.Status.REGISTERED).select_related(
        "person"
    )


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------


def create_reminders(registration):
    """Create one reminder per configured offset for the occurrence's current generation."""
    occurrence = registration.occurrence
    if registration.status != Registration.Status.REGISTERED or occurrence.status == Occurrence.Status.CANCELLED:
        return
    now = timezone.now()
    for offset in _offsets():
        send_at = occurrence.starts_at - timedelta(minutes=offset)
        late = send_at <= now
        ReminderSchedule.objects.get_or_create(
            registration=registration,
            offset_minutes=offset,
            generation=occurrence.schedule_generation,
            defaults={
                "send_at": send_at,
                "status": ReminderSchedule.Status.SKIPPED_LATE if late else ReminderSchedule.Status.PENDING,
                "processed_at": now if late else None,
            },
        )


def process_due_reminders(now=None):
    """Process due PENDING reminders; return the number enqueued."""
    now = now or timezone.now()
    grace = _grace()
    enqueued = 0
    with transaction.atomic():
        due = list(
            ReminderSchedule.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("registration__occurrence")
            .filter(status=ReminderSchedule.Status.PENDING, send_at__lte=now)
            .order_by("send_at")[:PROCESS_BATCH_SIZE]
        )
        for reminder in due:
            registration = reminder.registration
            occurrence = registration.occurrence
            if (
                registration.status != Registration.Status.REGISTERED
                or occurrence.status == Occurrence.Status.CANCELLED
            ):
                reminder.status = ReminderSchedule.Status.SUPPRESSED
            elif reminder.generation != occurrence.schedule_generation:
                reminder.status = ReminderSchedule.Status.SUPERSEDED
            elif now - reminder.send_at > grace or now >= occurrence.starts_at:
                reminder.status = ReminderSchedule.Status.SKIPPED_LATE
            else:
                _enqueue("reminder.due", registration=registration, occurrence=occurrence)
                reminder.status = ReminderSchedule.Status.ENQUEUED
                enqueued += 1
            reminder.processed_at = now
            reminder.save(update_fields=["status", "processed_at"])
    return enqueued


# --------------------------------------------------------------------------
# Occurrence lifecycle
# --------------------------------------------------------------------------


def cancel_occurrence(occurrence, user=None):
    """Cancel an occurrence, suppress reminders and notify registrants (idempotent)."""
    with transaction.atomic():
        occ = Occurrence.objects.select_for_update().get(pk=occurrence.pk)
        if occ.status == Occurrence.Status.CANCELLED:
            _sync(occurrence, occ, ["status", "cancelled_at", "ics_sequence", "updated_at"])
            return
        now = timezone.now()
        occ.status = Occurrence.Status.CANCELLED
        occ.cancelled_at = now
        occ.registration_open = False
        occ.ics_sequence += 1
        # Waiting room: a cancelled session never admits anyone (waiting_room.room_state).
        occ.admission_generation += 1
        occ.save(
            update_fields=[
                "status", "cancelled_at", "registration_open", "ics_sequence", "admission_generation", "updated_at",
            ]
        )
        suppressed = ReminderSchedule.objects.filter(
            registration__occurrence=occ, status=ReminderSchedule.Status.PENDING
        ).update(status=ReminderSchedule.Status.SUPPRESSED, processed_at=now)
        notified = 0
        for registration in _registered(occ):
            registration.occurrence = occ
            _enqueue("occurrence.cancelled", registration=registration, occurrence=occ)
            notified += 1
        audit(
            "occurrence.cancelled",
            user=_actor(user),
            obj=occ,
            ics_sequence=occ.ics_sequence,
            reminders_suppressed=suppressed,
            registrations_notified=notified,
        )
        # Paid registrations of a cancelled session are owed a refund (made in Stripe by
        # staff, then recorded); same transaction so the flag cannot be lost.
        from webinars.payments import flag_cancelled_occurrence

        flag_cancelled_occurrence(occ)
    _sync(
        occurrence, occ, ["status", "cancelled_at", "registration_open", "ics_sequence", "admission_generation", "updated_at"]
    )
    _hosting_hook("note_cancelled", occ)


_RESCHEDULE_FIELDS = [
    "local_start", "timezone", "dst_fold", "duration_minutes", "starts_at", "ends_at",
    "ics_sequence", "schedule_generation", "admission_generation", "admission_mode",
    "attendance_finalized_at", "updated_at",
]


def reschedule_occurrence(occurrence, *, local_start, tz_name, fold=None, duration_minutes=None, user=None):
    """Move an occurrence; supersede old reminders and schedule new ones.

    Starts a new waiting-room admission generation: manual admission mode, the
    attendance finalization marker and recorded attended/no_show values are reset,
    and unsent ``attendance.recorded`` messages for the old run are cancelled.
    """
    starts_at = local_to_utc(local_start, tz_name, fold)
    with transaction.atomic():
        occ = Occurrence.objects.select_for_update().get(pk=occurrence.pk)
        if occ.status == Occurrence.Status.CANCELLED:
            raise ValidationError("Cancelled occurrences cannot be rescheduled.", code="cancelled")
        duration = occ.duration_minutes if duration_minutes is None else duration_minutes
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            raise ValidationError("Duration must be a positive number of minutes.", code="invalid_duration")
        if duration <= 0:
            raise ValidationError("Duration must be a positive number of minutes.", code="invalid_duration")

        now = timezone.now()
        previous_start = occ.starts_at
        # Wall-clock value stored UTC-tagged (see timeutils.local_to_utc) to avoid naive-datetime warnings.
        occ.local_start = local_start.replace(tzinfo=dt_timezone.utc)
        occ.timezone = tz_name
        occ.dst_fold = fold
        occ.duration_minutes = duration
        occ.starts_at = starts_at
        occ.ends_at = starts_at + timedelta(minutes=duration)
        occ.ics_sequence += 1
        occ.schedule_generation += 1
        # Waiting room: earlier admissions (and manual Open/Hold/End) belonged to the old
        # time. Back to automatic doors; check-ins from the old generation no longer count.
        occ.admission_generation += 1
        occ.admission_mode = Occurrence.Admission.AUTO
        # Attendance belongs to a run: the new generation starts unfinalized, with no
        # attended/no_show carried over from the old time (it is finalized again when
        # the new run ends).
        occ.attendance_finalized_at = None
        occ.save(update_fields=_RESCHEDULE_FIELDS)
        superseded = ReminderSchedule.objects.filter(
            registration__occurrence=occ, status=ReminderSchedule.Status.PENDING
        ).update(status=ReminderSchedule.Status.SUPERSEDED, processed_at=now)
        attendance_reset = (
            Registration.objects.filter(occurrence=occ)
            .exclude(attendance=Registration.Attendance.UNKNOWN)
            .update(attendance=Registration.Attendance.UNKNOWN, updated_at=now)
        )
        # Old-run attendance messages not yet sent would drive follow-ups (e.g. no-show
        # campaigns) from stale state; the occurrence.rescheduled message below carries
        # the reset attendance instead. Already delivered / in-flight ones are history.
        stale_attendance_messages = OutboxMessage.objects.filter(
            status=OutboxMessage.Status.PENDING,
            event_type="attendance.recorded",
            payload__event__occurrence__id=str(occ.pk),
        ).update(status=OutboxMessage.Status.CANCELLED, last_error="superseded_by_reschedule")
        notified = 0
        for registration in _registered(occ):
            registration.occurrence = occ
            create_reminders(registration)
            _enqueue("occurrence.rescheduled", registration=registration, occurrence=occ)
            notified += 1
        audit(
            "occurrence.rescheduled",
            user=_actor(user),
            obj=occ,
            previous_starts_at=previous_start.isoformat(),
            starts_at=occ.starts_at.isoformat(),
            timezone=tz_name,
            ics_sequence=occ.ics_sequence,
            schedule_generation=occ.schedule_generation,
            reminders_superseded=superseded,
            registrations_notified=notified,
            attendance_reset=attendance_reset,
            attendance_messages_cancelled=stale_attendance_messages,
        )
    _sync(occurrence, occ, _RESCHEDULE_FIELDS)
    _hosting_hook("mark_out_of_date", occ)


def _hosting_hook(name, occ):
    """Flag a linked provider session (webinars.hosting.service). Never calls the provider,
    never deletes anything remotely, and never fails the schedule change."""
    import logging

    try:
        from webinars.hosting import service

        getattr(service, name)(occ)
    except Exception as exc:  # pragma: no cover - defensive
        logging.getLogger("webinars.hosting").warning("hosting hook %s failed: %s", name, type(exc).__name__)


def publish_recording(occurrence, recording_url, user=None):
    """Publish a recording URL (https only) and notify registrants."""
    validate_public_https_url(recording_url)
    with transaction.atomic():
        occ = Occurrence.objects.select_for_update().get(pk=occurrence.pk)
        occ.recording_url = recording_url
        occ.recording_published_at = timezone.now()
        occ.save(update_fields=["recording_url", "recording_published_at", "updated_at"])
        notified = 0
        for registration in _registered(occ):
            registration.occurrence = occ
            _enqueue("recording.published", registration=registration, occurrence=occ)
            notified += 1
        audit("recording.published", user=_actor(user), obj=occ, registrations_notified=notified)
    _sync(occurrence, occ, ["recording_url", "recording_published_at", "updated_at"])


# Kinds a person or API may record. ``checked_in`` is written only by the waiting room.
MANUAL_ATTENDANCE_KINDS = (
    AttendanceEvent.Kind.ATTENDED,
    AttendanceEvent.Kind.NO_SHOW,
    AttendanceEvent.Kind.FOLLOW_UP,
)


def has_current_check_in(registration, occurrence):
    from webinars.models import CheckIn

    return CheckIn.objects.filter(
        registration=registration, occurrence=occurrence, admission_generation=occurrence.admission_generation
    ).exists()


def record_attendance(registration, kind, user=None, source="admin"):
    """Record an explicit attendance / no-show / follow-up event.

    A registrant admitted through the waiting room (current check-in) cannot be
    marked as a no-show.
    """
    if kind not in MANUAL_ATTENDANCE_KINDS:
        raise ValidationError("Unknown attendance kind.", code="invalid_kind")
    source = (source or "")[:64]
    with transaction.atomic():
        reg = Registration.objects.select_for_update(of=("self",)).select_related("occurrence").get(pk=registration.pk)
        if kind == AttendanceEvent.Kind.NO_SHOW and has_current_check_in(reg, reg.occurrence):
            raise ValidationError("This registrant checked in through the waiting room.", code="checked_in")
        AttendanceEvent.objects.create(registration=reg, kind=kind, source=source, recorded_by=_actor(user))
        if kind in (AttendanceEvent.Kind.ATTENDED, AttendanceEvent.Kind.NO_SHOW):
            reg.attendance = kind
            reg.save(update_fields=["attendance", "updated_at"])
            event_type = "attendance.recorded"
        else:
            event_type = "follow_up.requested"
        _enqueue(event_type, registration=reg, occurrence=reg.occurrence)
        audit("attendance.recorded", user=_actor(user), obj=reg, kind=kind, source=source)
    registration.attendance = reg.attendance
