"""Lease-based outbox delivery with signed webhooks, retries and dead-lettering.

Flow: ``claim_batch`` locks due rows with ``FOR UPDATE SKIP LOCKED`` and sets a
lease; ``deliver`` performs the HTTP request *outside* any transaction and then
updates the row only if this worker still owns the lease (so a worker whose
lease expired and was re-claimed cannot overwrite the new owner's outcome).
Immediately before each send the lease is renewed conditionally (``renew_lease``);
if that fails the message was re-claimed or cancelled and is not sent. The
destination, erasure state and required consents are then re-read (a message
queued before a withdrawal or erasure is cancelled), and the body is rebuilt from
the stored canonical groups by ``outbox.refilter_for_send`` against the current
destination (``include_private_links``, ``allowed_groups``, ``field_mapping``) and
the occurrence's current managed-room state.
Delivery remains at-least-once: a crash after the HTTP send but before the
outcome is recorded resends the same delivery id.

``last_error`` only ever contains a short machine-readable reason and the HTTP
status: never response bodies (receivers may echo PII), payload values or secrets.
"""
import json
import logging
import random
import socket
import ssl
import time
import http.client
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from webinars import native_integrations
from webinars.audit import audit
from webinars.crypto import DecryptionError, decrypt_secret, sign_webhook
from webinars.models import Destination, OutboxMessage, Person, Registration
from webinars.outbox import consents_satisfied, refilter_for_send, registration_consents
from webinars.ssrf import TERMINATE_GRACE_SECONDS, ResolutionFailed, UnsafeDestination, post_json_bounded, post_json_pinned

_PINNED = post_json_pinned

logger = logging.getLogger(__name__)

USER_AGENT = "webinar-platform-webhook/1"
MAX_BACKOFF_SECONDS = 3600
BASE_BACKOFF_SECONDS = 30
DISABLED_RECHECK_SECONDS = 900


def _lease_seconds():
    return int(getattr(settings, "OUTBOX_LEASE_SECONDS", 120))


def _timeout():
    return float(getattr(settings, "WEBHOOK_TIMEOUT_SECONDS", 10))


def _batch_size():
    return int(getattr(settings, "OUTBOX_BATCH_SIZE", 20))


# Time reserved inside the lease for child start-up, termination and recording the outcome.
LEASE_SAFETY_SECONDS = 5.0 + 2 * TERMINATE_GRACE_SECONDS


def _total_timeout(message):
    """Hard deadline for one send: never longer than the lease just renewed for it."""
    configured = float(getattr(settings, "WEBHOOK_TOTAL_TIMEOUT_SECONDS", 30))
    remaining = (message.lease_expires_at - timezone.now()).total_seconds() - LEASE_SAFETY_SECONDS
    return min(configured, remaining)


def _send(message, url, body, headers):
    if post_json_pinned is not _PINNED:
        # The transport was replaced (tests): call it in-process, no child, no network.
        return post_json_pinned(url, body, headers, _timeout())
    total = _total_timeout(message)
    if total < 1.0:
        raise TimeoutError("lease_too_short")  # recorded as a timeout and retried
    return post_json_bounded(url, body, headers, _timeout(), total)


def backoff_seconds(attempts, rng=random):
    base = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** max(0, min(attempts, 20))))
    return max(1.0, base * rng.uniform(0.8, 1.2))


def claim_batch(worker_id: str, limit: int = 20):
    now = timezone.now()
    lease_until = now + timedelta(seconds=_lease_seconds())
    with transaction.atomic():
        due = Q(status=OutboxMessage.Status.PENDING, next_attempt_at__lte=now) | Q(
            status=OutboxMessage.Status.IN_FLIGHT, lease_expires_at__lt=now
        )
        rows = list(
            OutboxMessage.objects.select_for_update(skip_locked=True)
            .filter(due)
            .order_by("next_attempt_at", "created_at")[: max(1, int(limit))]
        )
        if not rows:
            return []
        expired_ids = [m.id for m in rows if m.status == OutboxMessage.Status.IN_FLIGHT]
        ids = [m.id for m in rows]
        # A lease that expired means the previous worker crashed or hung mid-delivery.
        # Count that as an attempt so a poison message cannot loop forever.
        if expired_ids:
            OutboxMessage.objects.filter(id__in=expired_ids).update(
                attempts=F("attempts") + 1, last_error="lease_expired"
            )
        OutboxMessage.objects.filter(id__in=ids).update(
            status=OutboxMessage.Status.IN_FLIGHT, lease_owner=worker_id, lease_expires_at=lease_until
        )
        claimed = list(OutboxMessage.objects.filter(id__in=ids).select_related("destination").order_by("next_attempt_at"))
    return claimed


def _owned(message):
    """Rows still held by this exact claim.

    ``lease_expires_at`` is part of the match so a re-claim by a worker that happens
    to reuse the same ``worker_id`` (restart, fixed hostname) is not mistaken for ours.
    """
    return OutboxMessage.objects.filter(
        id=message.id,
        lease_owner=message.lease_owner,
        lease_expires_at=message.lease_expires_at,
        status=OutboxMessage.Status.IN_FLIGHT,
    )


def renew_lease(message) -> bool:
    """Extend this message's lease immediately before sending it.

    A batch shares one lease, so slow receivers could let the tail of the batch
    expire and be re-claimed by another worker while this one still sends it. The
    conditional update fails if the row was re-claimed, cancelled (erasure) or
    finished elsewhere; the caller must then skip the send. Under Postgres the
    UPDATE waits for a concurrent ``claim_batch`` holding the row lock and then
    re-evaluates the WHERE clause, so at most one worker wins.
    """
    new_until = timezone.now() + timedelta(seconds=_lease_seconds())
    updated = _owned(message).update(lease_expires_at=new_until)
    if updated:
        message.lease_expires_at = new_until
    return bool(updated)


def _finish(message, **fields):
    """Conditional update: only applies while we still hold the lease."""
    fields.setdefault("lease_owner", "")
    fields.setdefault("lease_expires_at", None)
    updated = _owned(message).update(**fields)
    if updated:
        for k, v in fields.items():
            setattr(message, k, v)
    else:
        logger.warning("Lost lease before recording outcome for delivery %s", message.id)
    return bool(updated)


def _record_failure(message, *, error, status_code=None, dead=False, retry_after=None):
    attempts = message.attempts + 1
    max_attempts = message.destination.max_attempts or 1
    now = timezone.now()
    fields = {
        "attempts": attempts,
        "last_status_code": status_code,
        "last_error": str(error)[:500],
    }
    if dead or attempts >= max_attempts:
        fields["status"] = OutboxMessage.Status.DEAD
        fields["next_attempt_at"] = now
    else:
        fields["status"] = OutboxMessage.Status.PENDING
        delay = backoff_seconds(attempts)
        if retry_after is not None:
            # Receiver-requested wait (already capped in ssrf.parse_retry_after); never sooner than backoff.
            delay = max(delay, min(float(retry_after), MAX_BACKOFF_SECONDS))
        fields["next_attempt_at"] = now + timedelta(seconds=delay)
    _finish(message, **fields)
    logger.info(
        "Delivery %s failed (%s) attempt=%s status=%s",
        message.id,
        str(error)[:80],
        attempts,
        fields["status"],
    )


def serialize_body(message, destination=None):
    """Signed body: the stored snapshot re-filtered for the current destination/occurrence."""
    destination = destination if destination is not None else message.destination
    payload = refilter_for_send(dict(message.payload or {}), destination)
    metadata = dict(payload.get("delivery_metadata") or {})
    metadata["deliveryId"] = str(message.id)
    metadata["attempt"] = message.attempts + 1
    payload["delivery_metadata"] = metadata
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, cls=DjangoJSONEncoder).encode(
        "utf-8"
    )


PURGED = {"purged": True}
# Payload groups that only exist for person-linked events (see outbox.build_full_payload).
PERSON_LINKED_KEYS = ("identity", "profile", "organization", "consents", "attribution", "registration", "mapped")


def _is_person_linked(payload):
    return isinstance(payload, dict) and any(k in payload for k in PERSON_LINKED_KEYS)


def send_time_block_reason(message, destination):
    """Re-check erasure and consent right before sending (enqueue-time checks go stale).

    Returns a short reason when the message must be cancelled, else ``None``.
    """
    person = None
    if message.person_id is not None:
        person = Person.objects.filter(pk=message.person_id).first()
    if person is None and (message.person_id is not None or _is_person_linked(message.payload)):
        # The Person row was deleted (FK SET_NULL) but the payload still carries their data.
        return "person_deleted"
    if person is not None and person.erased_at is not None:
        return "person_erased"
    consents = None
    if person is not None and destination.required_consents:
        # Same rule as enqueue (outbox.registration_consents): an unverified registration's
        # submitted profile is never paired with the person's real consents. Messages queued
        # before that rule existed are caught here.
        if _payload_unverified(message.payload):
            return "consent_not_granted"
        registration = _message_registration(message, person)
        if registration is None:
            return "consent_not_granted"  # cannot prove the registration is verified
        consents = registration_consents(registration)
    if not consents_satisfied(destination, person, consents):
        return "consent_not_granted"
    return None


def _payload_unverified(payload):
    if not isinstance(payload, dict):
        return False
    identity_group = payload.get("identity")
    registration_group = payload.get("registration")
    return (isinstance(identity_group, dict) and identity_group.get("verified") is False) or (
        isinstance(registration_group, dict) and registration_group.get("identityVerified") is False
    )


def _message_registration(message, person):
    """The registration a person-linked message was built from (payload ids, else occurrence)."""
    payload = message.payload if isinstance(message.payload, dict) else {}
    qs = Registration.objects.filter(person=person)
    reg_id = (payload.get("registration") or {}).get("id") if isinstance(payload.get("registration"), dict) else None
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    occ = event.get("occurrence") if isinstance(event.get("occurrence"), dict) else {}
    try:
        if reg_id:
            return qs.filter(pk=reg_id).first()
        if occ.get("id"):
            return qs.filter(occurrence_id=occ["id"]).first()
    except (ValueError, ValidationError):
        return None
    return None


def deliver(message) -> None:
    if not renew_lease(message):
        logger.info("Skipping delivery %s: lease no longer held", message.id)
        return
    # Fresh copy: the destination may have been disabled or changed since the claim.
    # Enabled state and max_attempts are checked below, consent/erasure in
    # send_time_block_reason, and include_private_links / allowed_groups /
    # field_mapping plus the managed-room link policy in serialize_body
    # (outbox.refilter_for_send).
    destination = Destination.objects.filter(pk=message.destination_id).first()
    if destination is None:
        return
    message.destination = destination
    now = timezone.now()

    if not destination.enabled:
        _finish(
            message,
            status=OutboxMessage.Status.PENDING,
            next_attempt_at=now + timedelta(seconds=DISABLED_RECHECK_SECONDS),
            last_error="destination_disabled",
        )
        return

    if message.attempts >= (destination.max_attempts or 1):
        _finish(message, status=OutboxMessage.Status.DEAD, last_error="max_attempts_exceeded")
        return

    if isinstance(message.payload, dict) and message.payload.get("purged"):
        _finish(message, status=OutboxMessage.Status.CANCELLED, last_error="payload_purged")
        return

    reason = send_time_block_reason(message, destination)
    if reason:
        # Cancelled, not retried: the payload snapshot carries data we may no longer send.
        _finish(message, status=OutboxMessage.Status.CANCELLED, payload=PURGED, last_error=reason)
        logger.info("Delivery %s cancelled at send time (%s)", message.id, reason)
        return

    try:
        credentials = native_integrations.load_credentials(destination)
    except native_integrations.CredentialsUnavailable:
        _record_failure(message, error="credentials_unavailable")
        return
    provider = native_integrations.provider_of(credentials)
    if provider:
        _deliver_native(message, destination, provider, credentials)
        return
    del credentials

    if not destination.signing_secret_encrypted:
        _record_failure(message, error="signing_secret_missing")
        return
    try:
        secret = decrypt_secret(destination.signing_secret_encrypted)
    except (DecryptionError, ImproperlyConfigured):
        _record_failure(message, error="signing_secret_unavailable")
        return

    body = serialize_body(message, destination)
    timestamp = int(time.time())
    delivery_id = str(message.id)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": USER_AGENT,
        "Webinar-Delivery-Id": delivery_id,
        "Webinar-Event-Type": message.event_type,
        "Webinar-Timestamp": str(timestamp),
        "Webinar-Signature": sign_webhook(secret, delivery_id, timestamp, body),
    }
    del secret

    try:
        status, _snippet = _send(message, destination.url, body, headers)
    except ResolutionFailed:
        _record_failure(message, error="dns_resolution_failed")
        return
    except UnsafeDestination as exc:
        _record_failure(message, error=f"unsafe_destination: {exc}", dead=True)
        return
    except (socket.timeout, TimeoutError):
        _record_failure(message, error="timeout")
        return
    except ssl.SSLError as exc:
        _record_failure(message, error=f"tls_error: {type(exc).__name__}")
        return
    except (OSError, http.client.HTTPException) as exc:
        _record_failure(message, error=f"transport_error: {type(exc).__name__}")
        return

    if 200 <= status < 300:
        _finish(
            message,
            status=OutboxMessage.Status.DELIVERED,
            attempts=message.attempts + 1,
            last_status_code=status,
            last_error="",
            delivered_at=timezone.now(),
        )
        logger.info("Delivery %s delivered status=%s", message.id, status)
        return
    if 300 <= status < 400:
        _record_failure(message, error=f"redirect_not_followed: http {status}", status_code=status)
        return
    _record_failure(message, error=f"http_status: {status}", status_code=status)


def _native_post(message, url, body, headers):
    """POST without reading the body. Tests that replace ``post_json_pinned`` keep that seam."""
    if post_json_pinned is not _PINNED:
        # Same in-process test seam as _send, still restricted to vendor hosts.
        from webinars.ssrf import _check_native_request, _validated_target

        host = _validated_target(url)[0]
        _check_native_request("POST", host, headers, native_integrations.VENDOR_HOSTS)
        status, _snippet = post_json_pinned(url, body, headers, _timeout())
        return status, b"", None
    return _native_request(message, "POST", url, body, headers)


def _native_request(message, method, url, body, headers):
    """``(status, raw_body or None when oversized, retry_after)`` via ``ssrf.request_json_bounded``."""
    import webinars.ssrf

    request = getattr(webinars.ssrf, "request_json_bounded", None)
    if request is None:
        raise native_integrations.TransportUnavailable()
    total = _total_timeout(message)
    if total < 1.0:
        raise TimeoutError("lease_too_short")
    status, raw, retry_after, truncated = request(
        method,
        url,
        body,
        headers,
        _timeout(),
        total,
        native_integrations.MAX_RESPONSE_BYTES,
        allowed_hosts=native_integrations.VENDOR_HOSTS,
    )
    return status, (None if truncated else raw), retry_after


# Same value as mcp_config.TEST_EVENT_TYPE (kept local so delivery does not import the MCP layer).
NATIVE_TEST_EVENT_TYPE = "webhook.test"


def _native_step_check(message, provider):
    """Before each vendor call after the first: re-read the destination and re-run the
    send-time consent/erasure check and native policy. Returns an ``Outcome`` to stop, else None.
    Writes already made by earlier steps are not recalled."""
    fresh = Destination.objects.filter(pk=message.destination_id).first()
    if fresh is None:
        return native_integrations.Outcome("lost_lease")  # row cascaded with the destination
    if not fresh.enabled:
        return native_integrations.Outcome("disabled")
    reason = send_time_block_reason(message, fresh)
    if reason:
        return native_integrations.Outcome("cancelled", f"{reason}_mid_sequence")
    try:
        credentials = native_integrations.load_credentials(fresh)
    except native_integrations.CredentialsUnavailable:
        return native_integrations.Outcome("retry", "credentials_unavailable_mid_sequence")
    if native_integrations.provider_of(credentials) != provider:
        return native_integrations.Outcome("dead", "native_config_changed_mid_sequence")
    blocked = native_integrations.check_send_policy(provider, credentials, fresh)
    del credentials
    if blocked:
        return native_integrations.Outcome("dead", f"{blocked}_mid_sequence")
    return None


def _deliver_native(message, destination, provider, credentials):
    """Native vendor delivery: same lease, consent and retry/dead bookkeeping as webhooks."""
    if message.event_type == NATIVE_TEST_EVENT_TYPE:
        # The synthetic test fixture has no person, so no vendor contract applies. Say so
        # explicitly instead of a generic skip or a fake success.
        _finish(message, status=OutboxMessage.Status.CANCELLED, last_error="native_test_send_unsupported")
        return
    blocked = native_integrations.check_send_policy(provider, credentials, destination)
    if blocked:
        _record_failure(message, error=blocked, dead=True)
        return
    body = json.loads(serialize_body(message, destination))
    ctx = native_integrations.NativeContext(
        post=lambda url, data, headers: _native_post(message, url, data, headers),
        request=lambda method, url, data, headers: _native_request(message, method, url, data, headers),
        renew=lambda: renew_lease(message),
        check=lambda: _native_step_check(message, provider),
    )
    outcome = native_integrations.execute(provider, credentials, body, message.event_type, ctx)
    del credentials
    if outcome.kind == "lost_lease":
        logger.info("Native delivery %s stopped: lease no longer held", message.id)
        return
    if outcome.kind == "disabled":
        _finish(
            message,
            status=OutboxMessage.Status.PENDING,
            next_attempt_at=timezone.now() + timedelta(seconds=DISABLED_RECHECK_SECONDS),
            last_error="destination_disabled_mid_sequence",
        )
        return
    if outcome.kind == "delivered":
        _finish(
            message,
            status=OutboxMessage.Status.DELIVERED,
            attempts=message.attempts + 1,
            last_status_code=None,  # several vendor calls; each was 2xx
            last_error="",
            delivered_at=timezone.now(),
        )
        logger.info("Delivery %s delivered natively (%s, %s calls)", message.id, provider, ctx.calls)
        return
    if outcome.kind == "cancelled":
        _finish(message, status=OutboxMessage.Status.CANCELLED, payload=PURGED, last_error=outcome.error)
        return
    _record_failure(
        message,
        error=outcome.error,
        status_code=outcome.status,
        dead=outcome.kind == "dead",
        retry_after=outcome.retry_after,
    )


def run_once(worker_id: str) -> int:
    try:
        from webinars.scheduling import process_due_reminders

        process_due_reminders()
    except Exception as exc:  # keep the worker alive; never log exception text (may contain PII)
        logger.error("Reminder processing failed: %s", type(exc).__name__)

    try:
        from webinars.waiting_room import finalize_due_attendance

        finalize_due_attendance()
    except Exception as exc:
        logger.error("Attendance finalization failed: %s", type(exc).__name__)

    batch = claim_batch(worker_id, _batch_size())
    attempted = 0
    for message in batch:
        try:
            deliver(message)
        except Exception as exc:
            logger.error("Unexpected delivery error for %s: %s", message.id, type(exc).__name__)
            try:
                _record_failure(message, error=f"internal_error: {type(exc).__name__}")
            except Exception:
                pass  # lease will expire and the message will be re-claimed
        attempted += 1
    return attempted


def requeue_message(message, user=None) -> bool:
    """Admin action: move a dead-lettered message back to pending."""
    if isinstance(message.payload, dict) and message.payload.get("purged"):
        return False
    updated = OutboxMessage.objects.filter(id=message.id, status=OutboxMessage.Status.DEAD).update(
        status=OutboxMessage.Status.PENDING,
        attempts=0,
        next_attempt_at=timezone.now(),
        lease_owner="",
        lease_expires_at=None,
        last_error="",
        last_status_code=None,
    )
    if updated:
        audit("outbox.requeued", user=user, obj=message, destination_id=str(message.destination_id))
    return bool(updated)
