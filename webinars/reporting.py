"""Minimal aggregate reporting. Aggregates only — no PII, no inferred attendance.

Attendance is reported exactly as recorded on ``Registration.attendance``; a
registration with no recorded attendance is counted as ``unknown``, never as
attended or no-show (an RSVP is not evidence of attendance).
"""
from collections import defaultdict

from django.db.models import Count, Q

from webinars.models import ConsentRecord, Occurrence, OutboxMessage, Registration

PURPOSES = [p for p, _ in ConsentRecord.Purpose.choices]


def _current_consents():
    """{(person_id, purpose): granted} using the latest record per person and purpose."""
    current = {}
    rows = ConsentRecord.objects.order_by("person_id", "purpose", "-recorded_at", "-id").values_list(
        "person_id", "purpose", "granted"
    )
    for person_id, purpose, granted in rows.iterator():
        current.setdefault((person_id, purpose), granted)  # first row per key is the latest
    return current


def _consent_counts_by_occurrence():
    current = _current_consents()
    counts = defaultdict(lambda: {p: 0 for p in PURPOSES})
    if not current:
        return counts
    for occ_id, person_id in Registration.objects.values_list("occurrence_id", "person_id"):
        for purpose in PURPOSES:
            if current.get((person_id, purpose)):
                counts[occ_id][purpose] += 1
    return counts


def report_summary():
    """Return aggregate counts per occurrence plus outbox status counts."""
    occurrences = (
        Occurrence.objects.select_related("event")
        .annotate(
            registered=Count("registrations", filter=Q(registrations__status=Registration.Status.REGISTERED)),
            cancelled=Count("registrations", filter=Q(registrations__status=Registration.Status.CANCELLED)),
            attended=Count("registrations", filter=Q(registrations__attendance=Registration.Attendance.ATTENDED)),
            no_show=Count("registrations", filter=Q(registrations__attendance=Registration.Attendance.NO_SHOW)),
            unknown=Count("registrations", filter=Q(registrations__attendance=Registration.Attendance.UNKNOWN)),
        )
        .order_by("starts_at")
    )
    consent_counts = _consent_counts_by_occurrence()

    rows = []
    for occ in occurrences:
        rows.append(
            {
                "occurrenceId": str(occ.pk),
                "event": occ.event.internal_name,
                "eventSlug": occ.event.slug,
                "startsAt": occ.starts_at.isoformat(),
                "status": occ.status,
                "registrations": {"registered": occ.registered, "cancelled": occ.cancelled},
                # Recorded attendance only; "unknown" is never inferred from RSVPs.
                "attendance": {"attended": occ.attended, "no_show": occ.no_show, "unknown": occ.unknown},
                "consentsGranted": dict(consent_counts.get(occ.pk, {p: 0 for p in PURPOSES})),
            }
        )

    outbox = {s: 0 for s, _ in OutboxMessage.Status.choices}
    for status, n in OutboxMessage.objects.values_list("status").annotate(n=Count("id")).order_by():
        outbox[status] = n

    return {"occurrences": rows, "outbox": outbox}
