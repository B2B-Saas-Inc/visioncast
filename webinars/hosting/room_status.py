"""Where a self-hosted live room is, mirrored onto its session (docs/hosting.md).

The live server knows when a session goes live and when the crew ends it. The worker asks it about
every linked LiveKit session that's coming up or just finished, about once a minute, and keeps the
answer on the session so the studio can show it ("Live now", "Ended in the live room").

When the room has ended and the session's start time has passed, the session is marked completed:
registration closes and it leaves the upcoming lists. A room ended before the start time (a
rehearsal, a mistake) is shown, but changes nothing, so it can't close registration hours early.
Reopening the room puts a session this sync completed back to scheduled. A session completed any
other way, or cancelled, is never touched.

A room deleted on the live server's settings page unlinks its session, as Unlink on the Hosting
page would: the event and its registrations stay, and a new room can be created for it. The worker
asks each live server for its deleted rooms every couple of minutes, so this reaches sessions
outside the window above too; a link made after the deletion is a new room and is left alone.

A live server that doesn't answer is asked less and less often, and one session's failure never
stops the others.
"""
import logging
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from webinars.audit import audit
from webinars.hosting import base, service
from webinars.models import HostedSession, HostingAccount, Occurrence

logger = logging.getLogger(__name__)

POLL = timedelta(seconds=60)
# Which sessions are worth asking about: starting within this long, or ended less than AFTER ago.
LOOKAHEAD = timedelta(hours=24)
AFTER = timedelta(hours=6)
MAX_BACKOFF = timedelta(minutes=30)

LIVE_NOW = "live"
ENDED = "ended"

# How often each live server is asked which rooms were deleted, kept per worker process.
DELETED_POLL = timedelta(minutes=2)
_deleted_next = {}
UNLINKABLE = (HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE, HostedSession.State.FAILED)
DELETED_NOTE = "The room was deleted on the live server. Create a new room to host this session here."


def _session(occ):
    session = service.session_for(occ)
    adapter = service.adapter_for(session) if session is not None else None
    if adapter is None or session.provider != "livekit" or not hasattr(adapter, "fetch_room"):
        raise ValidationError({"action": ["This session isn't in a live room."]})
    return service._crew_session(occ)


def apply(session_pk, occ_pk, phase, *, user=None, api_key=None, now=None):
    """Store what the live server said and change the session's status if the rules say so.
    Returns ``{"phase", "status", "changed"}``; ``changed`` is ``"completed"``, ``"scheduled"`` or ``""``."""
    now = now or timezone.now()
    with transaction.atomic():
        occ = Occurrence.objects.select_for_update().get(pk=occ_pk)
        session = HostedSession.objects.select_for_update(of=("self",)).get(pk=session_pk)
        previous = session.room_phase
        changed = ""
        if phase == ENDED and occ.status == Occurrence.Status.SCHEDULED and now >= occ.starts_at:
            occ.status = Occurrence.Status.COMPLETED
            occ.save(update_fields=["status", "updated_at"])
            session.room_completed_at = now
            changed = "completed"
        elif phase != ENDED and occ.status == Occurrence.Status.COMPLETED and session.room_completed_at is not None:
            # Reopened: undo what this sync did, and nothing else.
            occ.status = Occurrence.Status.SCHEDULED
            occ.save(update_fields=["status", "updated_at"])
            session.room_completed_at = None
            changed = "scheduled"
        elif occ.status != Occurrence.Status.COMPLETED:
            session.room_completed_at = None
        session.room_phase = phase
        session.room_phase_at = now
        session.room_phase_attempts = 0
        session.room_phase_next_at = now + POLL
        session.save(update_fields=["room_phase", "room_phase_at", "room_phase_attempts", "room_phase_next_at", "room_completed_at", "updated_at"])
        if changed or phase != previous or user or api_key:
            audit("hosting.room_status_synced", user=user, api_key=api_key, obj=session,
                  phase=phase, previous=previous or None, status=occ.status, changed=changed or None)
    return {"phase": phase, "status": occ.status, "changed": changed}


def sync(occ, *, user=None, api_key=None, transport=None):
    """Ask the live server now. Safe to repeat. Raises ``ValidationError``."""
    session, adapter, secrets = _session(occ)
    outcome = adapter.fetch_room(session.account.config or {}, secrets, session.external_id, transport=transport or base.default_transport)
    if outcome.kind != "succeeded":
        raise ValidationError({"action": [f"Your live server didn't answer ({outcome.error})."]})
    if outcome.data.get("deleted"):
        unlinked = unlink_deleted(session.pk, user=user, api_key=api_key)
        return {"phase": "", "status": occ.status, "changed": "unlinked" if unlinked else ""}
    return apply(session.pk, occ.pk, outcome.data["phase"], user=user, api_key=api_key)


def unlink_deleted(session_pk, *, deleted_at=None, user=None, api_key=None, now=None):
    """The live server deleted the session's room: unlink it. Nothing on the live server is touched,
    and the occurrence keeps its status. ``deleted_at`` is when the live server deleted it: a session
    linked after that has a new room. Returns True if it unlinked."""
    now = now or timezone.now()
    with transaction.atomic():
        session = HostedSession.objects.select_for_update().filter(pk=session_pk, state__in=UNLINKABLE).exclude(external_id="").first()
        if session is None:
            return False
        if deleted_at is not None and session.last_synced_at is not None and session.last_synced_at > deleted_at:
            return False
        session.state = HostedSession.State.DETACHED
        session.last_error = DELETED_NOTE
        session.room_phase = ""
        session.room_phase_at = now
        session.room_phase_next_at = None
        session.room_phase_attempts = 0
        session.save(update_fields=["state", "last_error", "room_phase", "room_phase_at", "room_phase_next_at", "room_phase_attempts", "updated_at"])
        audit("hosting.room_deleted", user=user, api_key=api_key, obj=session, room=session.external_id)
    return True


def sweep_deleted(transport=None, now=None):
    """Worker step: unlink sessions whose rooms were deleted on their live server. Returns how many."""
    now = now or timezone.now()
    accounts = HostingAccount.objects.filter(
        provider="livekit", enabled=True, sessions__state__in=UNLINKABLE,
    ).exclude(sessions__external_id="").distinct()
    unlinked = 0
    for account in accounts:
        if _deleted_next.get(account.pk, now) > now:
            continue
        _deleted_next[account.pk] = now + DELETED_POLL
        try:
            adapter = service.adapter_for(account)
            outcome = adapter.fetch_deleted_rooms(account.config or {}, service.load_secrets(account), transport=transport or base.default_transport)
            if outcome.kind != "succeeded":
                logger.info("Deleted live rooms not fetched: %s", outcome.error)
                continue
            deleted = {room["id"]: room["deletedAt"] for room in outcome.data["rooms"]}
            if not deleted:
                continue
            for pk, external_id in HostedSession.objects.filter(account=account, state__in=UNLINKABLE, external_id__in=list(deleted)).values_list("pk", "external_id"):
                unlinked += unlink_deleted(pk, deleted_at=deleted[external_id], now=now)
        except Exception as exc:  # One live server's trouble never stops the others.
            logger.warning("Deleted live rooms could not be checked: %s", type(exc).__name__)
    return unlinked


def try_sync(occ, *, transport=None):
    """``sync`` for page views: never raises, returns None when it couldn't ask."""
    try:
        return sync(occ, transport=transport)
    except Exception as exc:
        logger.info("Live room status not refreshed: %s", type(exc).__name__)
        return None


def reopen(occ, *, user=None, api_key=None, transport=None):
    """An ended room back to "not live", for a session ended too early, then the status synced.
    Raises ``ValidationError``."""
    session, adapter, secrets = _session(occ)
    outcome = adapter.reopen_room(session.account.config or {}, secrets, session.external_id, transport=transport or base.default_transport)
    if outcome.kind != "succeeded":
        if "http 409" in outcome.error:
            raise ValidationError({"action": ["The session is on the air, so there's nothing to reopen."]})
        raise ValidationError({"action": [f"Your live server didn't answer ({outcome.error})."]})
    audit("hosting.room_reopened", user=user, api_key=api_key, obj=session)
    return apply(session.pk, occ.pk, outcome.data["phase"], user=user, api_key=api_key)


def sweep(limit=5, transport=None):
    """Worker step. Returns how many sessions changed status, so a quiet poll doesn't count as work."""
    now = timezone.now()
    changed = sweep_deleted(transport=transport, now=now)
    due = list(HostedSession.objects.filter(
        Q(room_phase_next_at__isnull=True) | Q(room_phase_next_at__lte=now),
        provider="livekit", state__in=(HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE), account__enabled=True,
        occurrence__starts_at__lte=now + LOOKAHEAD, occurrence__ends_at__gte=now - AFTER,
    ).exclude(external_id="").exclude(occurrence__status=Occurrence.Status.CANCELLED)
        .select_related("occurrence", "account").order_by(F("room_phase_next_at").asc(nulls_first=True), "occurrence__starts_at")[:limit])
    for session in due:
        # Claimed first, so that a second worker skips it.
        claim = Q(room_phase_next_at__isnull=True) | Q(room_phase_next_at=session.room_phase_next_at)
        if not HostedSession.objects.filter(claim, pk=session.pk).update(room_phase_next_at=now + POLL):
            continue
        try:
            changed += 1 if sync(session.occurrence, transport=transport)["changed"] else 0
        except Exception as exc:  # A bad session must never stop the others, or delivery.
            attempts = session.room_phase_attempts + 1
            logger.warning("Live room status for a session could not be fetched: %s", type(exc).__name__)
            HostedSession.objects.filter(pk=session.pk).update(
                room_phase_attempts=attempts, room_phase_next_at=now + min(POLL * (2 ** min(attempts, 6)), MAX_BACKOFF),
            )
    return changed


def label(session):
    """What the studio shows next to the status, or ''."""
    phase = getattr(session, "room_phase", "") if session is not None else ""
    if phase == LIVE_NOW:
        return "Live now"
    if phase == ENDED:
        return "Ended in the live room"
    return ""
