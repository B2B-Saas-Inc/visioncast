"""Erasure requests and retention purge.

    python manage.py purge_personal_data --person <uuid> [--dry-run]
    python manage.py purge_personal_data --older-than-days 365 [--dry-run]
    python manage.py purge_personal_data            # housekeeping only

Erasing a person (in one transaction, person row locked):

* Person PII fields blanked, ``email_key`` NULL, ``custom_fields`` {}, ``erased_at`` set.
  The UUID row stays so registrations/aggregate reporting remain consistent.
* ``Registration.submitted_profile`` and ``custom_fields`` -> {}.
* AttributionTouch, OrganizationExternalId and BrowserCapability rows deleted
  (capabilities are thereby revoked).
* ConsentRecord: ``phone_snapshot`` blanked. Purpose, granted flag, disclosure
  version/text, source and timestamps are kept as minimal, non-identifying
  evidence of what was consented to and when (linked only to the erased UUID).
* OutboxMessage for the person: undelivered -> ``cancelled``; every payload
  (delivered or not) replaced with ``{"purged": true}``.
* IdempotencyRecord rows for the person's registrations deleted.
* AttendanceImportRow: email key, masked email, display name and provider id blanked for
  rows matched to the person's registrations or carrying their email key.
* ``audit("person.purged")`` with the person id and counts only.

Housekeeping (always runs): IdempotencyRecord older than ``IDEMPOTENCY_TTL_HOURS``
(72) deleted; payloads of delivered/cancelled/dead OutboxMessages older than
``OUTBOX_PAYLOAD_RETENTION_DAYS`` (30) replaced with ``{"purged": true}`` (purged
dead messages can no longer be requeued). PENDING messages older than that
retention (e.g. stuck behind a disabled destination) are cancelled and purged.
Person-linked messages whose Person row was deleted outside this command (FK
``SET_NULL``) are purged, and cancelled if undelivered. The delivery worker
re-checks erasure before each send, so a message claimed just before erasure
committed is not sent (only an HTTP request already in progress can complete).
"""
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from webinars.audit import audit
from webinars.delivery import PERSON_LINKED_KEYS
from webinars.models import (
    AttendanceImportRow,
    AttributionTouch,
    BrowserCapability,
    ConsentRecord,
    IdempotencyRecord,
    OrganizationExternalId,
    OutboxMessage,
    Person,
    PreviewMessage,
    Registration,
    TicketOrder,
    WaitingRoomVerification,
)

# Preview-mailbox copies (preview/dev only) hold addresses and one-time links.
PREVIEW_MESSAGE_RETENTION = timedelta(days=1)

PURGED = {"purged": True}
PII_FIELDS = ("email", "phone", "first_name", "last_name", "name", "company", "job_title", "company_size")
UNDELIVERED = (
    OutboxMessage.Status.PENDING,
    OutboxMessage.Status.IN_FLIGHT,
    OutboxMessage.Status.DEAD,
)


def person_counts(person):
    regs = Registration.objects.filter(person=person)
    return {
        "registrations": regs.count(),
        "attribution_touches": AttributionTouch.objects.filter(person=person).count(),
        "organization_ids": OrganizationExternalId.objects.filter(person=person).count(),
        "capabilities": BrowserCapability.objects.filter(person=person).count(),
        "consent_records": ConsentRecord.objects.filter(person=person).count(),
        "outbox_cancelled": OutboxMessage.objects.filter(person=person, status__in=UNDELIVERED).count(),
        "outbox_payloads": OutboxMessage.objects.filter(person=person).exclude(payload=PURGED).count(),
        "idempotency_records": IdempotencyRecord.objects.filter(registration__person=person).count(),
    }


def erase_person(person_id, *, user=None):
    """Erase all PII for ``person_id``. Returns counts dict (no PII)."""
    now = timezone.now()
    with transaction.atomic():
        person = Person.objects.select_for_update().get(pk=person_id)
        counts = person_counts(person)
        original_email_key = person.email_key
        if person.email_key:
            # Waiting-room verification rows carry the address until they expire.
            WaitingRoomVerification.objects.filter(email_key=person.email_key).delete()

        for field in PII_FIELDS:
            setattr(person, field, "")
        person.email_key = None
        person.custom_fields = {}
        person.erased_at = now
        person.save(update_fields=[*PII_FIELDS, "email_key", "custom_fields", "erased_at", "updated_at"])

        Registration.objects.filter(person=person).update(submitted_profile={}, custom_fields={}, updated_at=now)
        AttributionTouch.objects.filter(Q(person=person) | Q(registration__person=person)).delete()
        OrganizationExternalId.objects.filter(person=person).delete()
        BrowserCapability.objects.filter(person=person).update(revoked_at=now, revoked_reason="erased")
        BrowserCapability.objects.filter(person=person).delete()
        ConsentRecord.objects.filter(person=person).update(phone_snapshot="")
        OutboxMessage.objects.filter(person=person, status__in=UNDELIVERED).update(
            status=OutboxMessage.Status.CANCELLED,
            payload=PURGED,
            lease_owner="",
            lease_expires_at=None,
            last_error="purged",
        )
        OutboxMessage.objects.filter(person=person).update(payload=PURGED)
        IdempotencyRecord.objects.filter(registration__person=person).delete()
        # Ticket orders keep the registration envelope and email key (PII) only until
        # fulfillment. Holds made before the person existed are found by email key. The
        # order rows themselves stay: they are the financial record (amount, status, ids).
        orders = Q(person=person)
        if original_email_key:
            orders |= Q(email_key=original_email_key)
        TicketOrder.objects.filter(orders).update(pending_payload={}, email_key="", updated_at=now)
        # Attendance import rows (email key, masked email, display name) for this person:
        # by matched registration and by the normalised email of unmatched rows.
        from webinars.attendance_import import purge_rows

        rows = Q(registration__person=person)
        if original_email_key:
            rows |= Q(email_key=original_email_key)
        counts["attendance_rows"] = purge_rows(AttendanceImportRow.objects.filter(rows), now)

        audit("person.purged", user=user, obj=person, **counts)
    return counts


def retention_candidates(days):
    cutoff = timezone.now() - timedelta(days=days)
    return (
        Person.objects.filter(erased_at__isnull=True, updated_at__lt=cutoff)
        .annotate(last_end=Max("registrations__occurrence__ends_at"), last_reg=Max("registrations__updated_at"))
        .filter(Q(last_end__lt=cutoff) | Q(last_end__isnull=True))
        .filter(Q(last_reg__lt=cutoff) | Q(last_reg__isnull=True))
    )


def housekeeping(dry_run=False):
    now = timezone.now()
    ttl_hours = int(getattr(settings, "IDEMPOTENCY_TTL_HOURS", 72))
    retention_days = int(getattr(settings, "OUTBOX_PAYLOAD_RETENTION_DAYS", 30))
    idem = IdempotencyRecord.objects.filter(created_at__lt=now - timedelta(hours=ttl_hours))
    old_cutoff = now - timedelta(days=retention_days)
    outbox = (
        OutboxMessage.objects.filter(
            status__in=[OutboxMessage.Status.DELIVERED, OutboxMessage.Status.CANCELLED, OutboxMessage.Status.DEAD]
        )
        .filter(Q(delivered_at__lt=old_cutoff) | Q(delivered_at__isnull=True, created_at__lt=old_cutoff))
        .exclude(payload=PURGED)
    )
    # Messages stuck PENDING (e.g. destination disabled) must not keep PII past retention.
    stale_pending = OutboxMessage.objects.filter(status=OutboxMessage.Status.PENDING, created_at__lt=old_cutoff)
    # Person row deleted outside erase_person (admin/ORM; FK is SET_NULL): the payload
    # still carries their data and can no longer be found by person.
    orphaned = OutboxMessage.objects.filter(person__isnull=True, payload__has_any_keys=list(PERSON_LINKED_KEYS)).exclude(
        payload=PURGED
    )
    verifications = WaitingRoomVerification.objects.filter(expires_at__lt=now - timedelta(hours=1))
    preview_messages = PreviewMessage.objects.filter(created_at__lt=now - PREVIEW_MESSAGE_RETENTION)
    # Ticket envelopes: closed orders never need them; open ones past retention are stale.
    ticket_payloads = TicketOrder.objects.exclude(Q(pending_payload={}) & Q(email_key="")).filter(
        ~Q(status__in=TicketOrder.HOLDING) | Q(created_at__lt=old_cutoff)
    )
    # Attendance import rows keep an email key / masked email / display name only for review;
    # after retention they are blanked (the import's counts and match states stay).
    attendance_rows = AttendanceImportRow.objects.filter(created_at__lt=old_cutoff).exclude(
        email_key="", email_masked="", display_name="", external_id="")
    if dry_run:
        return {
            "attendance_rows_purged": attendance_rows.count(),
            "ticket_payloads_purged": ticket_payloads.count(),
            "idempotency_deleted": idem.count(),
            "outbox_payloads_purged": outbox.count(),
            "outbox_stale_pending_cancelled": stale_pending.count(),
            "outbox_orphaned_purged": orphaned.count(),
            "verifications_deleted": verifications.count(),
            "preview_messages_deleted": preview_messages.count(),
        }
    deleted, _ = idem.delete()
    verifications_deleted, _ = verifications.delete()
    preview_deleted, _ = preview_messages.delete()
    cancel = {
        "status": OutboxMessage.Status.CANCELLED,
        "payload": PURGED,
        "lease_owner": "",
        "lease_expires_at": None,
    }
    with transaction.atomic():
        stale = stale_pending.update(last_error="retention_expired", **cancel)
        orphan_count = orphaned.filter(status__in=UNDELIVERED).update(last_error="person_deleted", **cancel)
        orphan_count += orphaned.update(payload=PURGED)
        purged = outbox.update(payload=PURGED)
        # Note: this bumps updated_at, which also restarts the sweep's recheck timer.
        tickets_purged = ticket_payloads.update(pending_payload={}, email_key="", updated_at=now)
        attendance_purged = attendance_rows.update(email_key="", email_masked="", display_name="", external_id="",
                                                   updated_at=now)
    return {
        "attendance_rows_purged": attendance_purged,
        "ticket_payloads_purged": tickets_purged,
        "idempotency_deleted": deleted,
        "outbox_payloads_purged": purged,
        "outbox_stale_pending_cancelled": stale,
        "outbox_orphaned_purged": orphan_count,
        "verifications_deleted": verifications_deleted,
        "preview_messages_deleted": preview_deleted,
    }


class Command(BaseCommand):
    help = "Erase a person's PII on request and/or purge personal data past retention."

    def add_arguments(self, parser):
        parser.add_argument("--person", help="Person UUID to erase (erasure request).")
        parser.add_argument("--older-than-days", type=int, help="Erase people with no activity for N days.")
        parser.add_argument("--dry-run", action="store_true", help="Report counts without changing anything.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        targets = []
        if opts.get("person"):
            try:
                pid = uuid.UUID(str(opts["person"]))
            except ValueError:
                raise CommandError("--person must be a UUID") from None
            if not Person.objects.filter(pk=pid).exists():
                raise CommandError("Person not found")
            targets.append(pid)
        if opts.get("older_than_days") is not None:
            days = opts["older_than_days"]
            if days < 1:
                raise CommandError("--older-than-days must be >= 1")
            targets.extend(p for p in retention_candidates(days).values_list("pk", flat=True) if p not in targets)

        erased = 0
        for pid in targets:
            if dry:
                counts = person_counts(Person.objects.get(pk=pid))
                self.stdout.write(f"[dry-run] would erase person {pid}: {counts}")
            else:
                counts = erase_person(pid)
                erased += 1
                self.stdout.write(f"erased person {pid}: {counts}")

        hk = housekeeping(dry_run=dry)
        if not dry:
            audit("retention.housekeeping", persons_erased=erased, **hk)
        prefix = "[dry-run] " if dry else ""
        self.stdout.write(f"{prefix}persons={len(targets)} housekeeping={hk}")
