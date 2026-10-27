"""Hosted waiting room: server-authoritative admission, mailbox verification, check-in.

Contract (docs/waiting-room.md):

* Each occurrence may enable a room at the stable attendee URL ``/w/<code>/``. The
  provider join link (``Occurrence.join_url``) is never rendered into any page, JSON
  response, invite or public API. It is disclosed only as the ``Location`` of the
  303 response to an authorized ``POST /w/<code>/enter``; from then on the attendee's
  browser (and anyone they share it with) knows it. That is inherent to redirecting
  to a third-party link and is documented, not hidden.
* Admission state is computed on the server for every request (``room_state``):
  cancelled > ended > hold > open > waiting. ``hold`` overrides the clock, ``open``
  admits immediately, ``ended`` closes. Automatic doors open
  ``admission_lead_minutes`` before the start and close at the scheduled end.
* A browser is *recognized* when it holds a valid ``BrowserCapability`` cookie (the
  existing opaque, hashed cookie). Recognition is not email verification; it only
  says this browser registered (or verified) earlier.
* Unknown browsers start with email only. The response is identical whether or not
  the address is known, and no person lookup happens before the mailbox is proven:
  the server emails a 6-digit code + one-time link. Verification (code typed into the
  requesting browser, or the link) binds a capability to that mailbox's person
  (creating the person if new). Only then does the room show either the welcome or
  the remaining form fields.
* Verification secrets: link token (32 random bytes, SHA-256 stored) and 6-digit code
  (HMAC-SHA256 with SECRET_KEY and the row id) expire after
  ``WAITING_ROOM_VERIFICATION_TTL_MINUTES``, are single-use, bound to one occurrence,
  and the code is bound to the requesting browser (``browser_hash``) with at most
  ``WAITING_ROOM_MAX_CODE_ATTEMPTS`` guesses.
* Check-in: exactly one ``CheckIn`` row per (occurrence, registration), enforced by a
  unique constraint; the row, the ``AttendanceEvent(kind=checked_in)`` and the
  ``attendance.checked_in`` outbox messages commit in one transaction. A reschedule
  bumps ``admission_generation``: a later admission updates the same row and emits one
  new event for the new generation.
* Attendance is finalized only after the attendance window closes (``ended``):
  registrants with a current check-in become ``attended``, the rest ``no_show``;
  explicitly recorded attendance is never overwritten.
* Finalization is final for the current admission generation: a finalized room is
  ``ended`` whatever the mode or clock says, and ``set_admission`` refuses to leave
  ``ended`` (code ``attendance_finalized``). Only a reschedule
  (``scheduling.reschedule_occurrence``) starts a new generation; it clears
  ``attendance_finalized_at`` and resets recorded attendance for the new run.
* Check-in and finalization serialize on the occurrence row lock (``admit`` and
  ``finalize_attendance`` both ``select_for_update`` the occurrence first).
"""
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from webinars import identity, normalization, outbox
from webinars.audit import audit
from webinars.models import (
    AttendanceEvent,
    CheckIn,
    Event,
    Occurrence,
    Person,
    Registration,
    WaitingRoomVerification,
)

PENDING_COOKIE = "wr_pending"
PENDING_COOKIE_MAX_AGE = 60 * 60
CODE_LENGTH = 6
# Manual "Open" keeps the doors open past the scheduled end (overruns) for at most this long.
OPEN_OVERRUN = timedelta(hours=6)

WAITING = "waiting"
OPEN = "open"
HOLD = "hold"
ENDED = "ended"
CANCELLED = "cancelled"
PREPARING = "preparing"  # would be open, but no join destination is configured yet
STATES = (WAITING, OPEN, HOLD, ENDED, CANCELLED, PREPARING)


# ---------------------------------------------------------------------------
# Configuration (studio / management API / MCP)
# ---------------------------------------------------------------------------


def _new_code():
    for _ in range(10):
        code = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")[:12]
        if not Occurrence.objects.filter(waiting_room_code=code).exists():
            return code
    raise RuntimeError("Could not allocate a waiting room code")  # pragma: no cover


def room_path(occ):
    return f"/w/{occ.waiting_room_code}/" if occ.waiting_room_code else None


def room_url(occ):
    path = room_path(occ)
    if path is None:
        return None
    return (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/") + path


def configure(occ, *, enabled=None, lead_minutes=None, user=None, api_key=None):
    """Enable/disable the room and set the automatic lead time. The code is kept when
    disabling so a re-enabled room keeps the URL attendees already have."""
    with transaction.atomic():
        occ_locked = Occurrence.objects.select_for_update().get(pk=occ.pk)
        changed = []
        if enabled is not None and bool(enabled) != occ_locked.waiting_room_enabled:
            occ_locked.waiting_room_enabled = bool(enabled)
            changed.append("waiting_room_enabled")
        if enabled and not occ_locked.waiting_room_code:
            occ_locked.waiting_room_code = _new_code()
            changed.append("waiting_room_code")
        if lead_minutes is not None:
            try:
                lead = int(lead_minutes)
            except (TypeError, ValueError):
                raise ValidationError({"admission_lead_minutes": "Enter a number of minutes."})
            if not 0 <= lead <= 240:
                raise ValidationError({"admission_lead_minutes": "Choose between 0 and 240 minutes."})
            if lead != occ_locked.admission_lead_minutes:
                occ_locked.admission_lead_minutes = lead
                changed.append("admission_lead_minutes")
        if changed:
            occ_locked.save(update_fields=changed + ["updated_at"])
            audit("waiting_room.configured", user=user, api_key=api_key, obj=occ_locked, fields=sorted(changed))
    _refresh(occ, occ_locked)
    return occ


def set_admission(occ, mode, *, user=None, api_key=None):
    """Live control: ``auto`` | ``hold`` | ``open`` | ``ended``. Ending finalizes attendance.

    Once attendance is finalized for the current generation the room cannot be
    reopened (``ValidationError`` code ``attendance_finalized``); reschedule instead.
    """
    if mode not in Occurrence.Admission.values:
        raise ValidationError({"admission_mode": "Unknown admission mode."})
    with transaction.atomic():
        occ_locked = Occurrence.objects.select_for_update().get(pk=occ.pk)
        if occ_locked.status == Occurrence.Status.CANCELLED:
            raise ValidationError("Cancelled sessions have no waiting room.")
        if not occ_locked.waiting_room_enabled:
            raise ValidationError("Turn the waiting room on first.")
        if (
            occ_locked.attendance_finalized_at is not None
            and mode != Occurrence.Admission.ENDED
            and occ_locked.admission_mode != mode
        ):
            raise ValidationError(
                "This session has ended and attendance is final. Reschedule it to run it again.",
                code="attendance_finalized",
            )
        if occ_locked.admission_mode != mode:
            previous = occ_locked.admission_mode
            occ_locked.admission_mode = mode
            occ_locked.save(update_fields=["admission_mode", "updated_at"])
            audit("waiting_room.admission", user=user, api_key=api_key, obj=occ_locked, mode=mode, previous=previous)
    _refresh(occ, occ_locked)
    if mode == Occurrence.Admission.ENDED:
        finalize_attendance(occ)
    return occ


def _refresh(target, source):
    for name in ("waiting_room_enabled", "waiting_room_code", "admission_mode", "admission_lead_minutes",
                 "admission_generation", "attendance_finalized_at", "updated_at"):
        setattr(target, name, getattr(source, name))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class RoomState:
    state: str
    opens_at: object
    starts_at: object
    ends_at: object
    closes_at: object

    @property
    def admitting(self):
        return self.state == OPEN


def room_state(occ, now=None):
    now = now or timezone.now()
    opens_at = occ.starts_at - timedelta(minutes=occ.admission_lead_minutes)
    mode = occ.admission_mode
    closes_at = occ.ends_at + OPEN_OVERRUN if mode == Occurrence.Admission.OPEN else occ.ends_at
    if occ.status == Occurrence.Status.CANCELLED:
        state = CANCELLED
    elif mode == Occurrence.Admission.ENDED or now >= closes_at or occ.attendance_finalized_at is not None:
        # A finalized generation never admits again (only a reschedule clears it).
        state = ENDED
    elif mode == Occurrence.Admission.HOLD:
        state = HOLD
    elif mode == Occurrence.Admission.OPEN or now >= opens_at:
        state = OPEN if _has_join_destination(occ) else PREPARING
    else:
        state = WAITING
    return RoomState(state=state, opens_at=opens_at, starts_at=occ.starts_at, ends_at=occ.ends_at, closes_at=closes_at)


def attendance_window_closed(occ, now=None):
    return room_state(occ, now).state in (ENDED, CANCELLED)


def occurrence_for_code(code):
    """The occurrence behind a public room code, or None (disabled, draft or unknown)."""
    if not isinstance(code, str) or not 6 <= len(code) <= 32 or not code.isascii():
        return None
    return (
        Occurrence.objects.select_related("event", "event__form_version")
        .filter(
            waiting_room_code=code,
            waiting_room_enabled=True,
            event__visibility__in=[Event.Visibility.PUBLIC, Event.Visibility.UNLISTED],
        )
        .first()
    )


def viewer_registration(occ, capability):
    """The capability holder's active registration for ``occ`` (None when not registered)."""
    if capability is None:
        return None
    return Registration.objects.filter(
        occurrence=occ, person_id=capability.person_id, status=Registration.Status.REGISTERED
    ).first()


def _iso(dt):
    return dt.astimezone(timezone.get_fixed_timezone(0)).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def status_payload(occ, capability, now=None):
    """Public poll body. No PII, no join link: only the caller's own browser state."""
    now = now or timezone.now()
    rs = room_state(occ, now)
    registration = viewer_registration(occ, capability)
    poll = int(getattr(settings, "WAITING_ROOM_POLL_SECONDS", 15))
    seconds_until_open = None
    if rs.state == WAITING:
        seconds_until_open = max(0, int((rs.opens_at - now).total_seconds()))
        poll = max(2, min(poll, seconds_until_open + 1))
    admit = rs.state == OPEN and registration is not None
    return {
        "state": rs.state,
        "serverTime": _iso(now),
        "startsAt": _iso(rs.starts_at),
        "endsAt": _iso(rs.ends_at),
        "opensAt": _iso(rs.opens_at),
        "secondsUntilOpen": seconds_until_open,
        "viewer": {"recognized": capability is not None, "registered": registration is not None},
        "admit": admit,
        "enterPath": f"/w/{occ.waiting_room_code}/enter" if admit else None,
        "pollAfterSeconds": None if rs.state in (ENDED, CANCELLED) else poll,
    }


# ---------------------------------------------------------------------------
# Mailbox verification
# ---------------------------------------------------------------------------


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _code_hash(row_id, code):
    key = settings.SECRET_KEY.encode("utf-8")
    return hmac.new(key, f"wr-code|{row_id}|{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def new_pending_token():
    return secrets.token_urlsafe(32)


def _ttl():
    return timedelta(minutes=int(getattr(settings, "WAITING_ROOM_VERIFICATION_TTL_MINUTES", 15)))


def request_verification(occ, raw_email, pending_token):
    """Email a code + link to ``raw_email``. Always behaves the same for known and
    unknown addresses: no person lookup happens here.

    Raises ``ValidationError`` only for a syntactically invalid address. Returns the
    verification row, or None when the per-address limit suppressed the message (the
    caller must show the same generic response either way).
    """
    try:
        email = normalization.normalize_email(raw_email)
    except normalization.FieldError as exc:
        raise ValidationError({"email": exc.message})
    if not pending_token:
        raise ValidationError({"email": "Your browser must accept cookies to verify."})
    key = normalization.email_key(email)
    now = timezone.now()
    ttl = _ttl()
    browser_hash = _sha(pending_token)
    # Two per-address caps (database-backed, so global across workers):
    # * per room: WAITING_ROOM_MAX_CODES_PER_EMAIL (default 3) per TTL window;
    # * across all rooms: WAITING_ROOM_MAX_CODES_PER_EMAIL_GLOBAL (default 10) per TTL
    #   window. This bounds total code guesses per address platform-wide (codes x
    #   WAITING_ROOM_MAX_CODE_ATTEMPTS), since the identity cookie is platform-wide.
    # Suppression returns before anything is superseded, so codes a browser already
    # holds stay valid: a flood of requests for someone else's address cannot cancel
    # the code the owner already received.
    recent = WaitingRoomVerification.objects.filter(email_key=key, created_at__gt=now - ttl)
    if recent.filter(occurrence=occ).count() >= int(getattr(settings, "WAITING_ROOM_MAX_CODES_PER_EMAIL", 3)):
        audit("waiting_room.verification_suppressed", obj=occ, reason="per_email_limit")
        return None
    if recent.count() >= int(getattr(settings, "WAITING_ROOM_MAX_CODES_PER_EMAIL_GLOBAL", 10)):
        audit("waiting_room.verification_suppressed", obj=occ, reason="per_email_global_limit")
        return None
    token = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"
    with transaction.atomic():
        # A new request supersedes this browser's earlier unconsumed codes for the room.
        WaitingRoomVerification.objects.filter(
            occurrence=occ, browser_hash=browser_hash, consumed_at__isnull=True
        ).update(consumed_at=now)
        row = WaitingRoomVerification(
            occurrence=occ,
            email=email,
            email_key=key,
            token_hash=_sha(token),
            code_hash="",
            browser_hash=browser_hash,
            created_at=now,
            expires_at=now + ttl,
        )
        row.code_hash = _code_hash(row.id, code)
        row.save()
    sent = _send_verification(occ, email, code, token)
    if sent:
        WaitingRoomVerification.objects.filter(pk=row.pk).update(sent=True)
        row.sent = True
    audit("waiting_room.verification_requested", obj=occ, sent=bool(sent))
    return row


def _send_verification(occ, email, code, token):
    from webinars import mail

    link = (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/") + f"/w/{occ.waiting_room_code}/verify/{token}"
    minutes = int(_ttl().total_seconds() // 60)
    title = occ.event.public_title
    body = (
        f"Your code for “{title}” is {code}\n\n"
        f"Enter it in the waiting room, or open this link to confirm it's you:\n{link}\n\n"
        f"The code and link work once and expire in {minutes} minutes.\n"
        "If you didn't ask for this, you can ignore this email: nothing happens without the code.\n"
    )
    return mail.send(email, f"Your waiting room code: {code}", body)


@dataclass
class Verified:
    token: str  # raw capability token: set as cookie, never log
    person_id: object
    new_person: bool


class VerificationFailed(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


GENERIC_FAILURE = "That code didn't work. Check the latest email or request a new code."


def verify_code(occ, pending_token, code):
    """Verify a typed code for this browser. Raises ``VerificationFailed`` (generic message)."""
    code = (code or "").strip().replace(" ", "")
    if not pending_token or not code.isdigit() or len(code) != CODE_LENGTH:
        raise VerificationFailed(GENERIC_FAILURE)
    now = timezone.now()
    max_attempts = int(getattr(settings, "WAITING_ROOM_MAX_CODE_ATTEMPTS", 5))
    with transaction.atomic():
        row = (
            WaitingRoomVerification.objects.select_for_update()
            .filter(occurrence=occ, browser_hash=_sha(pending_token), consumed_at__isnull=True, expires_at__gt=now)
            .order_by("-created_at")
            .first()
        )
        if row is None:
            raise VerificationFailed(GENERIC_FAILURE)
        # Guesses are also capped per (browser, address) across all rooms. Keyed by the
        # guessing browser, so wrong guesses never lock the address owner's own browser.
        guessed = (
            WaitingRoomVerification.objects.filter(
                browser_hash=row.browser_hash, email_key=row.email_key, created_at__gt=now - _ttl()
            ).aggregate(n=Sum("attempts"))["n"]
            or 0
        )
        reason = ""
        if guessed >= int(getattr(settings, "WAITING_ROOM_MAX_GUESSES_PER_EMAIL", 10)):
            row.consumed_at = now  # burned without comparing: request a new code later
            row.save(update_fields=["consumed_at"])
            reason = "guess_limit"
        elif not hmac.compare_digest(row.code_hash, _code_hash(row.id, code)):
            row.attempts += 1
            fields = ["attempts"]
            if row.attempts >= max_attempts:
                row.consumed_at = now  # burned: request a new code
                fields.append("consumed_at")
            row.save(update_fields=fields)
            reason = "code_mismatch"  # raise after the block so the attempt counter commits
        else:
            row.consumed_at = now
            row.save(update_fields=["consumed_at"])
    if reason:
        audit("waiting_room.verification_failed", obj=occ, reason=reason)
        raise VerificationFailed(GENERIC_FAILURE)
    return _complete(occ, row)


def link_is_usable(occ, token):
    """True when the emailed link token is valid (GET shows a confirm button; it does not consume)."""
    if not token or len(token) > 128:
        return False
    return WaitingRoomVerification.objects.filter(
        occurrence=occ, token_hash=_sha(token), consumed_at__isnull=True, expires_at__gt=timezone.now()
    ).exists()


@dataclass
class LinkCheck:
    """What the confirm page may show for an emailed link (nothing is consumed)."""

    usable: bool
    email: str = ""  # the address being verified; shown so a foreign browser can say "not me"
    same_browser: bool = False  # this browser holds the pending cookie that requested the code


def inspect_link(occ, token, pending_token=None):
    """Check an emailed link without consuming it.

    ``same_browser`` is True only when the browser opening the link holds the pending
    cookie that requested it. Any other browser (a forwarded or planted link: login
    CSRF) must be shown the address and choose explicitly before the link is spent.
    """
    if not token or len(token) > 128:
        return LinkCheck(False)
    row = WaitingRoomVerification.objects.filter(
        occurrence=occ, token_hash=_sha(token), consumed_at__isnull=True, expires_at__gt=timezone.now()
    ).first()
    if row is None:
        return LinkCheck(False)
    same = bool(pending_token) and len(pending_token) <= 128 and hmac.compare_digest(row.browser_hash, _sha(pending_token))
    return LinkCheck(True, email=row.email, same_browser=same)


def verify_link(occ, token):
    """Consume an emailed link token (POST only, so mail scanners' GETs never use it)."""
    if not token or len(token) > 128:
        raise VerificationFailed(GENERIC_FAILURE)
    now = timezone.now()
    with transaction.atomic():
        updated = WaitingRoomVerification.objects.filter(
            occurrence=occ, token_hash=_sha(token), consumed_at__isnull=True, expires_at__gt=now
        ).update(consumed_at=now)
        if not updated:
            raise VerificationFailed("This link was already used or has expired. Request a new code in the room.")
        row = WaitingRoomVerification.objects.get(token_hash=_sha(token))
    return _complete(occ, row)


def _complete(occ, row):
    """Mailbox proven: bind a new capability to that address's person (create if new)."""
    for attempt in range(2):
        try:
            with transaction.atomic():
                person = Person.objects.select_for_update().filter(email_key=row.email_key).first()
                new_person = False
                if person is not None and person.erased_at is not None:
                    raise VerificationFailed(GENERIC_FAILURE)
                if person is None:
                    person = Person.objects.create(email=row.email, email_key=row.email_key)
                    new_person = True
                token = identity.issue_capability(person, mailbox_verified=True)
                audit("waiting_room.verified", obj=occ, person_id=str(person.pk), new_person=new_person)
            return Verified(token=token, person_id=person.pk, new_person=new_person)
        except IntegrityError:
            if attempt == 0:
                continue  # concurrent creation of the same person: re-read it
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Registration inside the room (verified or recognized browsers only)
# ---------------------------------------------------------------------------


def missing_field_keys(occ, capability):
    person = capability.person if capability is not None else None
    return identity.missing_fields(person, occ.event.form_version)


def register_in_room(occ, capability, *, profile, custom_fields, consents, idempotency_key, rate_key=None):
    """Register the capability holder through the shared registration service.

    Unknown browsers never reach this: they must verify first (the view enforces a
    capability). Late walk-ins are allowed until the room ends.
    """
    from webinars.registration import RegistrationResult, error_body, register

    if capability is None:
        return RegistrationResult(403, error_body("forbidden", "Verify your email first."))
    rs = room_state(occ)
    if rs.state in (ENDED, CANCELLED):
        return RegistrationResult(410, error_body("registration_closed", "This session has ended."))
    payload = {
        "schemaVersion": "1",
        "occurrenceId": str(occ.pk),
        "formVersion": occ.event.form_version.version,
        "idempotencyKey": idempotency_key,
        "profile": profile,
        "customFields": custom_fields,
        "consents": consents,
        "attribution": {"parameters": {}, "identifiers": {}, "pageUrl": "", "referrer": ""},
    }
    return register(payload=payload, capability=capability, api_key=None, trusted=False, allow_started=True,
                    rate_key=rate_key)


# ---------------------------------------------------------------------------
# Admission + check-in
# ---------------------------------------------------------------------------


@dataclass
class Admission:
    admitted: bool
    state: str
    join_url: str | None = None  # only for the 303 Location header; never render it
    created: bool = False
    reason: str = ""


def admit(occ, capability, now=None):
    """Authorize entry and record the check-in exactly once per admission generation.

    The room state is re-checked under the occurrence row lock, the same lock End
    (``set_admission``) and ``finalize_attendance`` take, so a check-in either commits
    before finalization reads check-ins or sees the room ended.
    """
    now = now or timezone.now()
    occ = Occurrence.objects.select_related("event").get(pk=occ.pk)
    rs = room_state(occ, now)
    if rs.state != OPEN:
        return Admission(False, rs.state, reason="not_open")
    if capability is None:
        return Admission(False, rs.state, reason="unknown_browser")
    registration = viewer_registration(occ, capability)
    if registration is None:
        return Admission(False, rs.state, reason="not_registered")
    created = False
    try:
        with transaction.atomic():
            locked = Occurrence.objects.select_for_update().get(pk=occ.pk)
            locked.event = occ.event
            occ = locked
            state = room_state(occ, now).state
            if state != OPEN:
                return Admission(False, state, reason="not_open")
            reg = Registration.objects.select_for_update(of=("self",)).get(pk=registration.pk)
            if reg.status != Registration.Status.REGISTERED:
                return Admission(False, state, reason="not_registered")
            from webinars import payments

            if payments.blocks_admission(reg):
                # Paid ticket flagged for refund or refunded: never admitted.
                return Admission(False, state, reason="refund")
            join_url = _attendee_join_url(occ, reg)
            if not join_url:
                return Admission(False, state, reason="no_link")
            generation = occ.admission_generation
            check_in = CheckIn.objects.filter(occurrence=occ, registration=reg).first()
            late = now > occ.starts_at
            if check_in is None:
                CheckIn.objects.create(
                    occurrence=occ, registration=reg, checked_in_at=now, admission_generation=generation, late=late
                )
                created = True
            elif check_in.admission_generation != generation:
                CheckIn.objects.filter(pk=check_in.pk).update(
                    admission_generation=generation, checked_in_at=now, late=late
                )
                created = True
            if created:
                AttendanceEvent.objects.create(
                    registration=reg, kind=AttendanceEvent.Kind.CHECKED_IN, source="waiting_room", occurred_at=now
                )
                reg.occurrence = occ
                outbox.enqueue_event("attendance.checked_in", registration=reg, occurrence=occ)
                audit("attendance.checked_in", obj=reg, occurrence_id=str(occ.pk), late=late, generation=generation)
    except IntegrityError:
        # A concurrent request inserted the check-in first; its transaction owns the event.
        created = False
        join_url = _attendee_join_url(occ, registration)
        if not join_url:
            return Admission(False, OPEN, reason="no_link")
    return Admission(True, OPEN, join_url=join_url, created=created)


def _has_join_destination(occ):
    """Doors may open: the shared link, or (linked Riverside) an enabled account whose
    per-registrant links are checked at admission. Fails closed (``preparing``)."""
    try:
        from webinars.hosting import service

        return service.has_join_destination(occ)
    except Exception:
        return False


def _attendee_join_url(occ, registration):
    """Per-registrant provider link (e.g. Riverside) when linked, else the occurrence link.
    Fails closed: an unexpected error admits nobody rather than handing out a shared link."""
    try:
        from webinars.hosting import service

        return service.attendee_join_url(occ, registration) or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Attendance finalization (after the window closes)
# ---------------------------------------------------------------------------


def finalize_attendance(occ, now=None):
    """Mark checked-in registrants attended and the rest no-show, once, after close.

    Returns the number of registrations updated, or None when not (yet) applicable.
    Only rooms that were enabled count: without a room, absence of a check-in says
    nothing about attendance.
    """
    from webinars.scheduling import record_attendance

    now = now or timezone.now()
    with transaction.atomic():
        locked = Occurrence.objects.select_for_update().get(pk=occ.pk)
        if (
            locked.attendance_finalized_at is not None
            or not locked.waiting_room_enabled
            or locked.status == Occurrence.Status.CANCELLED
            or room_state(locked, now).state != ENDED
        ):
            return None
        checked = set(
            CheckIn.objects.filter(occurrence=locked, admission_generation=locked.admission_generation).values_list(
                "registration_id", flat=True
            )
        )
        updated = 0
        pending = Registration.objects.filter(
            occurrence=locked, status=Registration.Status.REGISTERED, attendance=Registration.Attendance.UNKNOWN
        )
        for reg in pending:
            kind = AttendanceEvent.Kind.ATTENDED if reg.pk in checked else AttendanceEvent.Kind.NO_SHOW
            try:
                record_attendance(reg, kind, source="waiting_room")
            except ValidationError as exc:
                # A current check-in we did not see (should not happen under the lock):
                # the registrant was admitted, so they attended. Never fail the End.
                if kind != AttendanceEvent.Kind.NO_SHOW or getattr(exc, "code", None) != "checked_in":
                    raise
                record_attendance(reg, AttendanceEvent.Kind.ATTENDED, source="waiting_room")
            updated += 1
        locked.attendance_finalized_at = now
        locked.save(update_fields=["attendance_finalized_at", "updated_at"])
        audit("waiting_room.attendance_finalized", obj=locked, updated=updated, checked_in=len(checked))
    occ.attendance_finalized_at = now
    return updated


def finalize_due_attendance(now=None, limit=50):
    """Worker hook: finalize rooms whose attendance window has closed."""
    now = now or timezone.now()
    candidates = Occurrence.objects.filter(
        waiting_room_enabled=True,
        attendance_finalized_at__isnull=True,
        status=Occurrence.Status.SCHEDULED,
        starts_at__lte=now,
    ).order_by("ends_at")[:limit]
    done = 0
    for occ in candidates:
        if finalize_attendance(occ, now) is not None:
            done += 1
    return done


def purge_expired_verifications(now=None):
    """Housekeeping: verification rows hold an email address; delete them after expiry."""
    now = now or timezone.now()
    deleted, _ = WaitingRoomVerification.objects.filter(expires_at__lt=now - timedelta(hours=1)).delete()
    return deleted
