"""Regression tests for the rebuild review findings M3, S1, S2 and S3 (waiting room)."""
import re
import uuid
from datetime import timedelta

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from tests.conftest import COOKIE, make_destination, make_event, make_form, make_occurrence, make_person, make_registration
from webinars import identity, ratelimit, scheduling, waiting_room
from webinars.models import BrowserCapability, CheckIn, Occurrence, OutboxMessage, Registration

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def room_settings(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.WAITING_ROOM_VERIFICATION_TTL_MINUTES = 15
    settings.WAITING_ROOM_MAX_CODE_ATTEMPTS = 5
    settings.WAITING_ROOM_MAX_CODES_PER_EMAIL = 3
    settings.WAITING_ROOM_MAX_CODES_PER_EMAIL_GLOBAL = 10
    settings.WAITING_ROOM_MAX_GUESSES_PER_EMAIL = 10
    mail.outbox = []


def room_occurrence(starts_in=timedelta(hours=1), lead=15, **kw):
    form = make_form(version=f"wrfix-{uuid.uuid4().hex[:10]}")
    event = make_event(form)
    occ = make_occurrence(event, starts_at=timezone.now() + starts_in, **kw)
    waiting_room.configure(occ, enabled=True, lead_minutes=lead)
    occ.refresh_from_db()
    return occ


def registered_viewer(occ, email=None):
    person = make_person(email=email or f"viewer-{uuid.uuid4().hex[:10]}@example.com", first_name="Lin")
    reg = make_registration(occ, person=person)
    token = identity.issue_capability(person)
    cap = BrowserCapability.objects.get(token_hash=identity_hash(token))
    return reg, token, cap


def identity_hash(token):
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def latest_code():
    match = re.search(r"\b(\d{6})\b", mail.outbox[-1].subject)
    assert match
    return match.group(1)


def latest_link_token(code):
    match = re.search(rf"/w/{re.escape(code)}/verify/([A-Za-z0-9_\-]+)", mail.outbox[-1].body)
    assert match
    return match.group(1)


def wrong_code(code):
    return "000000" if code != "000000" else "111111"


def new_local(days):
    return (timezone.now() + timedelta(days=days)).replace(tzinfo=None, hour=10, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# M3: finalization is final for the generation; reschedule starts a new one
# ---------------------------------------------------------------------------


def test_reopen_after_end_is_refused_and_room_stays_ended():
    occ = room_occurrence()
    waiting_room.set_admission(occ, "open")
    _reg, _token, cap = registered_viewer(occ)
    assert waiting_room.admit(occ, cap).admitted
    waiting_room.set_admission(occ, "ended")
    occ.refresh_from_db()
    assert occ.attendance_finalized_at is not None

    for mode in ("open", "hold", "auto"):
        with pytest.raises(ValidationError) as excinfo:
            waiting_room.set_admission(occ, mode)
        assert excinfo.value.code == "attendance_finalized"
    occ.refresh_from_db()
    assert occ.admission_mode == Occurrence.Admission.ENDED
    waiting_room.set_admission(occ, "ended")  # idempotent, no error

    late_reg, _t, late_cap = registered_viewer(occ)
    result = waiting_room.admit(occ, late_cap)
    assert result.admitted is False
    assert result.join_url is None
    assert not CheckIn.objects.filter(registration=late_reg).exists()


def test_worker_finalized_room_cannot_be_reopened_for_overrun():
    occ = room_occurrence(starts_in=-timedelta(hours=2))  # 60 min session, ended an hour ago
    _reg, _t, _cap = registered_viewer(occ)
    assert waiting_room.finalize_due_attendance() == 1
    occ.refresh_from_db()
    assert occ.attendance_finalized_at is not None
    with pytest.raises(ValidationError) as excinfo:
        waiting_room.set_admission(occ, "open")
    assert excinfo.value.code == "attendance_finalized"
    # Even if the mode were changed behind the service's back, a finalized run is ended.
    Occurrence.objects.filter(pk=occ.pk).update(admission_mode=Occurrence.Admission.OPEN)
    occ.refresh_from_db()
    assert waiting_room.room_state(occ).state == waiting_room.ENDED


def test_studio_reopen_after_end_shows_message_not_500(client, django_user_model):
    occ = room_occurrence()
    waiting_room.set_admission(occ, "ended")
    user = django_user_model.objects.create_superuser("owner", "owner@example.com", "studio-pass-Quiet-Harbor-7")
    client.force_login(user)
    resp = client.post(reverse("studio:session-admission", kwargs={"pk": occ.pk}), {"mode": "open"}, follow=True)
    assert resp.status_code == 200
    assert "attendance is final" in resp.content.decode()
    occ.refresh_from_db()
    assert occ.admission_mode == Occurrence.Admission.ENDED


def test_reschedule_resets_finalization_attendance_and_stale_messages():
    occ = room_occurrence()
    make_destination(event_types=("attendance.recorded", "occurrence.rescheduled"))
    waiting_room.set_admission(occ, "open")
    came, _t1, came_cap = registered_viewer(occ, email="came@example.com")
    missed, _t2, missed_cap = registered_viewer(occ, email="missed@example.com")
    assert waiting_room.admit(occ, came_cap).admitted
    waiting_room.set_admission(occ, "ended")
    came.refresh_from_db()
    missed.refresh_from_db()
    assert came.attendance == Registration.Attendance.ATTENDED
    assert missed.attendance == Registration.Attendance.NO_SHOW
    stale = list(OutboxMessage.objects.filter(event_type="attendance.recorded").values_list("pk", flat=True))
    assert len(stale) == 2

    scheduling.reschedule_occurrence(occ, local_start=new_local(3), tz_name="Europe/London")
    occ.refresh_from_db()
    assert occ.attendance_finalized_at is None
    assert occ.admission_mode == Occurrence.Admission.AUTO
    came.refresh_from_db()
    missed.refresh_from_db()
    assert came.attendance == Registration.Attendance.UNKNOWN
    assert missed.attendance == Registration.Attendance.UNKNOWN
    # Unsent old-run attendance messages must not drive follow-ups from stale state.
    assert set(OutboxMessage.objects.filter(pk__in=stale).values_list("status", flat=True)) == {
        OutboxMessage.Status.CANCELLED
    }
    rescheduled = OutboxMessage.objects.filter(event_type="occurrence.rescheduled")
    assert rescheduled.count() == 2
    assert {m.payload["registration"]["attendance"] for m in rescheduled} == {"unknown"}

    # The new run is admitted and finalized on its own terms.
    waiting_room.set_admission(occ, "open")
    assert waiting_room.admit(occ, missed_cap).created
    waiting_room.set_admission(occ, "ended")
    occ.refresh_from_db()
    assert occ.attendance_finalized_at is not None
    came.refresh_from_db()
    missed.refresh_from_db()
    assert missed.attendance == Registration.Attendance.ATTENDED
    assert came.attendance == Registration.Attendance.NO_SHOW


# ---------------------------------------------------------------------------
# S3: check-in vs End
# ---------------------------------------------------------------------------


def test_finalization_with_unseen_checked_in_record_marks_attended(monkeypatch):
    occ = room_occurrence()
    waiting_room.set_admission(occ, "open")
    reg, _token, cap = registered_viewer(occ)
    assert waiting_room.admit(occ, cap).created

    class _NoCheckIns:  # finalization's snapshot misses the check-in (the old race)
        class objects:
            @staticmethod
            def filter(**kwargs):
                return CheckIn.objects.none()

    monkeypatch.setattr(waiting_room, "CheckIn", _NoCheckIns)
    waiting_room.set_admission(occ, "ended")  # must not raise ValidationError("checked_in")
    reg.refresh_from_db()
    occ.refresh_from_db()
    assert reg.attendance == Registration.Attendance.ATTENDED
    assert occ.attendance_finalized_at is not None


def test_admit_rechecks_state_after_end():
    occ = room_occurrence()
    waiting_room.set_admission(occ, "open")
    reg, _token, cap = registered_viewer(occ)
    stale = Occurrence.objects.get(pk=occ.pk)  # caller still holds the "open" row
    waiting_room.set_admission(occ, "ended")
    result = waiting_room.admit(stale, cap)
    assert result.admitted is False
    assert not CheckIn.objects.filter(registration=reg).exists()


# ---------------------------------------------------------------------------
# S1: rate limits
# ---------------------------------------------------------------------------


def _request(xff=None, remote="10.0.0.1"):
    meta = {"REMOTE_ADDR": remote}
    if xff is not None:
        meta["HTTP_X_FORWARDED_FOR"] = xff
    return RequestFactory().get("/w/x/status", **meta)


def test_xff_uses_rightmost_untrusted_hop(settings):
    settings.RATE_LIMIT_TRUST_X_FORWARDED_FOR = True
    settings.RATE_LIMIT_TRUSTED_PROXY_COUNT = 1
    assert ratelimit.client_ip(_request("6.6.6.6, 203.0.113.7")) == "203.0.113.7"
    assert ratelimit.client_ip(_request("203.0.113.7")) == "203.0.113.7"
    settings.RATE_LIMIT_TRUSTED_PROXY_COUNT = 2
    assert ratelimit.client_ip(_request("6.6.6.6, 203.0.113.7, 198.51.100.2")) == "203.0.113.7"
    settings.RATE_LIMIT_TRUST_X_FORWARDED_FOR = False
    assert ratelimit.client_ip(_request("6.6.6.6, 203.0.113.7")) == "10.0.0.1"


def test_spoofed_leftmost_xff_does_not_reset_the_bucket(settings):
    settings.RATE_LIMIT_ENABLED = True
    settings.RATE_LIMIT_TRUST_X_FORWARDED_FOR = True
    settings.RATE_LIMIT_TRUSTED_PROXY_COUNT = 1
    limit, _window = ratelimit.LIMITS["wr_identify"]
    results = [ratelimit.hit(_request(f"10.9.{i}.1, 203.0.113.9"), "wr_identify") for i in range(limit + 1)]
    assert results[-1] is False


def test_hit_key_counts_per_key(settings):
    settings.RATE_LIMIT_ENABLED = True
    assert ratelimit.hit_key("mcp_key", "key-a", limit=2, window=60)
    assert ratelimit.hit_key("mcp_key", "key-a", limit=2, window=60)
    assert not ratelimit.hit_key("mcp_key", "key-a", limit=2, window=60)
    assert ratelimit.hit_key("mcp_key", "key-b", limit=2, window=60)


def test_per_email_code_limit_applies_across_rooms(settings):
    settings.WAITING_ROOM_MAX_CODES_PER_EMAIL_GLOBAL = 4
    rooms = [room_occurrence() for _ in range(5)]
    rows = [
        waiting_room.request_verification(occ, "roamer@example.com", waiting_room.new_pending_token())
        for occ in rooms
    ]
    assert all(r is not None for r in rows[:4])
    assert rows[4] is None
    assert len(mail.outbox) == 4


def test_guess_limit_is_per_browser_and_email_across_rooms(settings):
    settings.WAITING_ROOM_MAX_GUESSES_PER_EMAIL = 6
    room_a, room_b = room_occurrence(), room_occurrence()
    attacker = waiting_room.new_pending_token()
    waiting_room.request_verification(room_a, "target@example.com", attacker)
    bad = wrong_code(latest_code())
    for _ in range(5):
        with pytest.raises(waiting_room.VerificationFailed):
            waiting_room.verify_code(room_a, attacker, bad)
    waiting_room.request_verification(room_b, "target@example.com", attacker)
    code_b = latest_code()
    with pytest.raises(waiting_room.VerificationFailed):
        waiting_room.verify_code(room_b, attacker, wrong_code(code_b))
    # Cross-room budget (6) exhausted for this browser: even the right code fails now.
    with pytest.raises(waiting_room.VerificationFailed):
        waiting_room.verify_code(room_b, attacker, code_b)

    # The address owner's own browser is not locked out by someone else's guesses.
    owner = waiting_room.new_pending_token()
    waiting_room.request_verification(room_b, "target@example.com", owner)
    assert waiting_room.verify_code(room_b, owner, latest_code()).token


def test_suppressed_request_keeps_existing_code_valid():
    occ = room_occurrence()
    owner = waiting_room.new_pending_token()
    waiting_room.request_verification(occ, "keep@example.com", owner)
    owner_code = latest_code()
    for _ in range(3):  # someone else floods requests for the same address
        waiting_room.request_verification(occ, "keep@example.com", waiting_room.new_pending_token())
    assert len(mail.outbox) == 3  # the last request was suppressed
    assert waiting_room.verify_code(occ, owner, owner_code).token


# ---------------------------------------------------------------------------
# S2: magic link bound to the requesting browser, explicit confirm elsewhere
# ---------------------------------------------------------------------------


def test_magic_link_in_foreign_browser_shows_email_and_requires_explicit_confirm():
    occ = room_occurrence()
    waiting_room.request_verification(occ, "attacker.own@example.com", waiting_room.new_pending_token())
    token = latest_link_token(occ.waiting_room_code)
    path = f"/w/{occ.waiting_room_code}/verify/{token}"

    victim = Client()
    page = victim.get(path)
    assert page.status_code == 200
    body = page.content.decode()
    assert "attacker.own@example.com" in body
    assert "This is me, continue" in body
    assert "Not me" in body
    assert "<script" not in body  # nothing submits on its own
    assert waiting_room.link_is_usable(occ, token)  # GET never spends it
    assert victim.cookies.get(COOKIE) is None or not victim.cookies[COOKIE].value

    resp = victim.post(path, {"confirm": "me"})
    assert resp.status_code == 303
    assert resp.cookies.get(COOKIE) is not None and resp.cookies[COOKIE].value
    assert not waiting_room.link_is_usable(occ, token)


def test_magic_link_in_requesting_browser_is_plain_continue():
    occ = room_occurrence()
    browser = Client()
    browser.post(f"/w/{occ.waiting_room_code}/identify", {"email": "same.browser@example.com"})
    token = latest_link_token(occ.waiting_room_code)
    path = f"/w/{occ.waiting_room_code}/verify/{token}"

    page = browser.get(path)
    assert page.status_code == 200
    body = page.content.decode()
    assert "Continue to the waiting room" in body
    assert "Not me" not in body
    assert "<script" not in body

    resp = browser.post(path)
    assert resp.status_code == 303
    assert resp.cookies.get(COOKIE) is not None and resp.cookies[COOKIE].value
    pending = resp.cookies.get(waiting_room.PENDING_COOKIE)
    assert pending is not None and pending.value == ""  # deleted after use
