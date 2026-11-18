"""Where a self-hosted live room is, mirrored onto its session (docs/hosting.md).

The live server knows when a session goes live and when the crew ends it. The worker asks it about
every linked LiveKit session that's coming up or just finished, about once a minute, and keeps the
answer on the session so the studio can show it ("Live now", "Ended in the live room").

When the room has ended and the session's start time has passed, the session is marked completed:
registration closes and it leaves the upcoming lists. A room ended before the start time (a
rehearsal, a mistake) is shown, but changes nothing, so it can't close registration hours early.
Reopening the room puts a session this sync completed back to scheduled. A session completed any
other way, or cancelled, is never touched.

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
from webinars.models import HostedSession, Occurrence

logger = logging.getLogger(__name__)

POLL = timedelta(seconds=60)
# Which sessions are worth asking about: starting within this long, or ended less than AFTER ago.
LOOKAHEAD = timedelta(hours=24)
AFTER = timedelta(hours=6)
MAX_BACKOFF = timedelta(minutes=30)

LIVE_NOW = "live"
ENDED = "ended"


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
    return apply(session.pk, occ.pk, outcome.data["phase"], user=user, api_key=api_key)


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
    due = list(HostedSession.objects.filter(
        Q(room_phase_next_at__isnull=True) | Q(room_phase_next_at__lte=now),
        provider="livekit", state__in=(HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE), account__enabled=True,
        occurrence__starts_at__lte=now + LOOKAHEAD, occurrence__ends_at__gte=now - AFTER,
    ).exclude(external_id="").exclude(occurrence__status=Occurrence.Status.CANCELLED)
        .select_related("occurrence", "account").order_by(F("room_phase_next_at").asc(nulls_first=True), "occurrence__starts_at")[:limit])
    changed = 0
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
