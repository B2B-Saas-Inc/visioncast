"""What people did in a self-hosted live room, brought back after it ends (docs/hosting.md).

The service keeps chat, questions, poll votes and survey answers. Once a session is over, the worker
asks it for them, stores each person's on their registration and sends ``engagement.recorded`` to
destinations that subscribe. It's personal data: the ``engagement`` group goes only to destinations
that list it, and erasing a person erases it, here and on the live server. An anonymous question
isn't sent here at all.

The worker keeps asking for a day after the session ends, because surveys are answered after it.
Only what changed is stored and sent. A live server that doesn't answer is asked less and less often,
and given up on after a while, so one dead server can't hold up anything else.
"""
import logging
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from webinars import outbox
from webinars.audit import audit
from webinars.hosting import base, service
from webinars.models import HostedSession, Occurrence, Registration

logger = logging.getLogger(__name__)

# Long enough for a session that runs a little over, and for the crew to end it.
SETTLE = timedelta(minutes=10)
# Nobody pressed End session: take what's there, and stop asking this long after it should've finished.
GIVE_UP = timedelta(hours=6)
# Surveys are answered after the end. Keep asking for this long after it.
SURVEY_WINDOW = timedelta(hours=24)
ASK_AGAIN = timedelta(hours=1)
MAX_ATTEMPTS = 10


def _registrations(occ, people):
    ids = []
    for identity in people:
        try:
            ids.append(uuid.UUID(identity))
        except ValueError:
            continue  # someone let in another way, with no registration to attach it to
    # Someone who's been erased stays erased, whatever the live server still has.
    return list(Registration.objects.filter(occurrence=occ, pk__in=ids, person__erased_at__isnull=True).values_list("pk", flat=True))


def _store(pk, people):
    """Stored and sent together, once, however many workers or people ask at the same time."""
    with transaction.atomic():
        registration = Registration.objects.select_for_update(of=("self",)).select_related("person", "occurrence").get(pk=pk)
        row = people[str(pk)]
        if registration.engagement == row:
            return False
        registration.engagement = row
        registration.save(update_fields=["engagement", "updated_at"])
        outbox.enqueue_event("engagement.recorded", registration=registration)
    return True


def sync(occ, *, user=None, api_key=None, transport=None):
    """Brings it back now. Safe to repeat: only what changed is stored and sent. Raises ``ValidationError``."""
    session, adapter, secrets = service._live_session(occ)
    outcome = adapter.fetch_engagement(session.account.config or {}, secrets, session.external_id, transport=transport or base.default_transport)
    if outcome.kind != "succeeded":
        raise ValidationError({"action": [f"Your live server didn't answer ({outcome.error})."]})
    people = outcome.data["people"]
    changed = sum(1 for pk in _registrations(occ, people) if _store(pk, people))
    now = timezone.now()
    ended = outcome.data["phase"] == "ended"
    if ended and session.engagement_ended_at is None:
        session.engagement_ended_at = now
    if ended:
        final = now - session.engagement_ended_at >= SURVEY_WINDOW
    else:
        final = bool(occ.ends_at and now - occ.ends_at > GIVE_UP)
    # Everyone hasn't come back if there were more pages than it would read.
    final = final and outcome.data["complete"]
    HostedSession.objects.filter(pk=session.pk).update(
        engagement_ended_at=session.engagement_ended_at, engagement_attempts=0,
        engagement_next_at=now + ASK_AGAIN, engagement_synced_at=now if final else None,
    )
    if changed or final or user or api_key:
        audit("hosting.engagement_synced", user=user, api_key=api_key, obj=session, people=len(people), changed=changed)
    return {"people": len(people), "changed": changed, "final": bool(final)}


def summary(occ):
    rows = Registration.objects.filter(occurrence=occ).exclude(engagement={}).values_list("engagement", flat=True)
    out = {"people": 0, "chatMessages": 0, "questions": 0, "pollVotes": 0, "surveyAnswers": 0}
    for row in rows:
        out["people"] += 1
        out["chatMessages"] += row.get("chatMessages") or 0
        out["questions"] += len(row.get("questions") or [])
        out["pollVotes"] += len(row.get("polls") or [])
        out["surveyAnswers"] += 1 if row.get("survey") else 0
    return out


def sweep(limit=5, transport=None):
    """Worker step: sessions that are over and due. Returns how many got to their last pass, so that
    a session still going doesn't count as work and keep the worker from resting."""
    now = timezone.now()
    due = list(HostedSession.objects.filter(
        Q(engagement_next_at__isnull=True) | Q(engagement_next_at__lte=now),
        provider="livekit", engagement_synced_at__isnull=True, state__in=(HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE),
        occurrence__ends_at__lt=now - SETTLE, account__enabled=True,
    ).exclude(external_id="").exclude(occurrence__status=Occurrence.Status.CANCELLED).select_related("occurrence", "account").order_by(F("engagement_next_at").asc(nulls_first=True), "occurrence__ends_at")[:limit])
    done = 0
    for session in due:
        # Claimed first, so that a second worker skips it.
        claim = Q(engagement_next_at__isnull=True) | Q(engagement_next_at=session.engagement_next_at)
        if not HostedSession.objects.filter(claim, pk=session.pk).update(engagement_next_at=now + SETTLE):
            continue
        try:
            done += 1 if sync(session.occurrence, transport=transport)["final"] else 0
        except Exception as exc:  # A bad session must never stop the others, or delivery.
            attempts = session.engagement_attempts + 1
            logger.warning("Engagement for a session could not be fetched: %s", type(exc).__name__)
            HostedSession.objects.filter(pk=session.pk).update(
                engagement_attempts=attempts, engagement_next_at=now + SETTLE * (2 ** min(attempts, 7)),
                engagement_synced_at=now if attempts >= MAX_ATTEMPTS else None,
            )
    return done


def forget_on_live_server(registrations, transport=None):
    """Asks the live server to erase what it holds on these people. Returns how many it couldn't reach."""
    failed = 0
    for registration in registrations:
        session = service.session_for(registration.occurrence)
        if session is None or session.provider != "livekit" or not session.external_id or session.account is None:
            continue
        try:
            adapter = service.adapter_for(session)
            outcome = adapter.forget_person(session.account.config or {}, service.load_secrets(session.account), session.external_id,
                                            str(registration.pk), transport=transport or base.default_transport)
            failed += 0 if outcome.kind == "succeeded" else 1
        except Exception as exc:
            logger.warning("Couldn't erase someone on a live server: %s", type(exc).__name__)
            failed += 1
    return failed
