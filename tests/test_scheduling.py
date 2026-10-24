from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from tests.conftest import make_destination, make_occurrence, make_registration, payload, post_registration
from webinars.models import AttendanceEvent, OutboxMessage, Registration, ReminderSchedule
from webinars.scheduling import (
    cancel_occurrence,
    create_reminders,
    process_due_reminders,
    publish_recording,
    record_attendance,
    reschedule_occurrence,
)

pytestmark = pytest.mark.django_db

P = ReminderSchedule.Status


def test_reminders_created_per_offset(occurrence):
    reg = make_registration(occurrence)
    create_reminders(reg)
    rows = {r.offset_minutes: r for r in ReminderSchedule.objects.filter(registration=reg)}
    assert set(rows) == {1440, 60}
    for offset, r in rows.items():
        assert r.status == P.PENDING
        assert r.send_at == occurrence.starts_at - timedelta(minutes=offset)
        assert r.generation == occurrence.schedule_generation


def test_registration_via_api_creates_reminders(client, occurrence, proxy_key):
    assert post_registration(client, payload(occurrence), proxy_key).status_code == 201
    assert ReminderSchedule.objects.filter(status=P.PENDING).count() == 2


def test_late_reminders_skipped_at_creation(event):
    occ = make_occurrence(event, starts_at=timezone.now() + timedelta(minutes=30))
    reg = make_registration(occ)
    create_reminders(reg)
    assert not ReminderSchedule.objects.filter(registration=reg, status=P.PENDING).exists()
    assert all(r.status == P.SKIPPED_LATE for r in ReminderSchedule.objects.filter(registration=reg))


def test_partially_late_reminders(event):
    occ = make_occurrence(event, starts_at=timezone.now() + timedelta(hours=3))
    reg = make_registration(occ)
    create_reminders(reg)
    statuses = {r.offset_minutes: r.status for r in ReminderSchedule.objects.filter(registration=reg)}
    assert statuses.get(60) == P.PENDING
    assert statuses.get(1440, P.SKIPPED_LATE) == P.SKIPPED_LATE


def _reminder(reg, send_at, offset=60):
    return ReminderSchedule.objects.create(
        registration=reg, offset_minutes=offset, send_at=send_at, generation=reg.occurrence.schedule_generation
    )


def test_process_due_reminder_within_grace_enqueues(occurrence):
    make_destination(event_types=["reminder.due"])
    reg = make_registration(occurrence)
    r = _reminder(reg, timezone.now() - timedelta(minutes=1))
    assert process_due_reminders() == 1
    r.refresh_from_db()
    assert r.status == P.ENQUEUED
    assert r.processed_at is not None
    assert OutboxMessage.objects.filter(event_type="reminder.due").count() == 1


def test_process_due_reminder_beyond_grace_skipped(occurrence):
    make_destination(event_types=["reminder.due"])
    reg = make_registration(occurrence)
    r = _reminder(reg, timezone.now() - timedelta(hours=2))
    assert process_due_reminders() == 0
    r.refresh_from_db()
    assert r.status == P.SKIPPED_LATE
    assert not OutboxMessage.objects.filter(event_type="reminder.due").exists()


def test_future_reminders_not_processed(occurrence):
    reg = make_registration(occurrence)
    r = _reminder(reg, timezone.now() + timedelta(hours=1))
    assert process_due_reminders() == 0
    r.refresh_from_db()
    assert r.status == P.PENDING


def test_process_is_idempotent(occurrence):
    make_destination(event_types=["reminder.due"])
    reg = make_registration(occurrence)
    _reminder(reg, timezone.now() - timedelta(minutes=1))
    process_due_reminders()
    process_due_reminders()
    assert OutboxMessage.objects.filter(event_type="reminder.due").count() == 1


def test_cancellation_suppresses_reminders_and_notifies(occurrence):
    make_destination(event_types=["occurrence.cancelled", "reminder.due"])
    reg = make_registration(occurrence)
    create_reminders(reg)
    cancel_occurrence(occurrence)
    occurrence.refresh_from_db()
    assert occurrence.status == "cancelled"
    assert occurrence.cancelled_at is not None
    assert occurrence.ics_sequence == 1
    assert not ReminderSchedule.objects.filter(status=P.PENDING).exists()
    assert ReminderSchedule.objects.filter(status=P.SUPPRESSED).count() == 2
    assert OutboxMessage.objects.filter(event_type="occurrence.cancelled").count() == 1
    # Even if a stale row became due, it must not be sent.
    ReminderSchedule.objects.update(status=P.PENDING, send_at=timezone.now() - timedelta(minutes=1))
    assert process_due_reminders() == 0
    assert not OutboxMessage.objects.filter(event_type="reminder.due").exists()


def test_reschedule_supersedes_and_recreates(occurrence):
    make_destination(event_types=["occurrence.rescheduled"])
    reg = make_registration(occurrence)
    create_reminders(reg)
    old_gen = occurrence.schedule_generation
    old_start = occurrence.starts_at
    new_local = datetime.combine((timezone.now() + timedelta(days=40)).date(), datetime.min.time()).replace(hour=9)
    reschedule_occurrence(occurrence, local_start=new_local, tz_name="America/New_York")
    occurrence.refresh_from_db()
    assert occurrence.schedule_generation == old_gen + 1
    assert occurrence.ics_sequence == 1
    assert occurrence.starts_at != old_start
    assert occurrence.timezone == "America/New_York"
    old = ReminderSchedule.objects.filter(registration=reg, generation=old_gen)
    assert old.count() == 2 and all(r.status == P.SUPERSEDED for r in old)
    new = ReminderSchedule.objects.filter(registration=reg, generation=old_gen + 1)
    assert {r.offset_minutes for r in new} == {1440, 60}
    assert all(r.status == P.PENDING for r in new)
    assert all(r.send_at == occurrence.starts_at - timedelta(minutes=r.offset_minutes) for r in new)
    assert OutboxMessage.objects.filter(event_type="occurrence.rescheduled").count() == 1


def test_superseded_generation_not_sent(occurrence):
    make_destination(event_types=["reminder.due"])
    reg = make_registration(occurrence)
    stale = ReminderSchedule.objects.create(
        registration=reg, offset_minutes=60, send_at=timezone.now() - timedelta(minutes=1), generation=0
    )
    process_due_reminders()
    stale.refresh_from_db()
    assert stale.status != P.ENQUEUED
    assert not OutboxMessage.objects.filter(event_type="reminder.due").exists()


def test_reschedule_rejects_nonexistent_local_time(occurrence):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        reschedule_occurrence(occurrence, local_start=datetime(2027, 3, 28, 1, 30), tz_name="Europe/London")
    occurrence.refresh_from_db()
    assert occurrence.ics_sequence == 0


def test_attendance_not_inferred_from_rsvp(client, occurrence, proxy_key):
    assert post_registration(client, payload(occurrence), proxy_key).status_code == 201
    reg = Registration.objects.get()
    assert reg.attendance == Registration.Attendance.UNKNOWN
    assert not AttendanceEvent.objects.exists()


def test_record_attendance_sets_attended(occurrence):
    make_destination(event_types=["attendance.recorded"])
    reg = make_registration(occurrence)
    record_attendance(reg, "attended")
    reg.refresh_from_db()
    assert reg.attendance == Registration.Attendance.ATTENDED
    assert AttendanceEvent.objects.filter(registration=reg, kind="attended").count() == 1
    assert OutboxMessage.objects.filter(event_type="attendance.recorded").count() == 1


def test_record_no_show(occurrence):
    reg = make_registration(occurrence)
    record_attendance(reg, "no_show")
    reg.refresh_from_db()
    assert reg.attendance == Registration.Attendance.NO_SHOW


def test_publish_recording_requires_https(occurrence):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        publish_recording(occurrence, "http://media.example.com/rec")
    publish_recording(occurrence, "https://media.example.com/rec")
    occurrence.refresh_from_db()
    assert occurrence.recording_published
