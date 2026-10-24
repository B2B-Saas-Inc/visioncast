"""Hosted waiting room: admission state, verification, check-in and the /w/<code>/ HTTP contract."""
import re
import threading
import uuid
from datetime import timedelta

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone

from tests.conftest import (
    COOKIE,
    JOIN_URL,
    make_destination,
    make_event,
    make_form,
    make_occurrence,
    make_person,
    make_registration,
    sha256,
)
from webinars import identity, scheduling, waiting_room
from webinars.models import (
    AttendanceEvent,
    BrowserCapability,
    CheckIn,
    Occurrence,
    OutboxMessage,
    Registration,
    WaitingRoomVerification,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.WAITING_ROOM_VERIFICATION_TTL_MINUTES = 15
    settings.WAITING_ROOM_MAX_CODE_ATTEMPTS = 5
    settings.WAITING_ROOM_MAX_CODES_PER_EMAIL = 3
    mail.outbox = []


def room_occurrence(starts_in=timedelta(hours=1), lead=15, **kw):
    form = make_form(version=f"wr-{uuid.uuid4().hex[:10]}")
    event = make_event(form)
    occ = make_occurrence(event, starts_at=timezone.now() + starts_in, **kw)
    waiting_room.configure(occ, enabled=True, lead_minutes=lead)
    occ.refresh_from_db()
    return occ


def capability_for(person):
    token = identity.issue_capability(person)
    return token, BrowserCapability.objects.get(token_hash=sha256(token))


def registered_viewer(occ, email=None):
    person = make_person(email=email or f"viewer-{uuid.uuid4().hex[:10]}@example.com", first_name="Lin")
    reg = make_registration(occ, person=person)
    token, cap = capability_for(person)
    return reg, token, cap


def latest_code():
    match = re.search(r"\b(\d{6})\b", mail.outbox[-1].subject)
    assert match, "verification subject must carry the code"
    return match.group(1)


def latest_link_token(code):
    match = re.search(rf"/w/{re.escape(code)}/verify/([A-Za-z0-9_\-]+)", mail.outbox[-1].body)
    assert match, "verification email must carry the one-time link"
    return match.group(1)


def wrong_code(code):
    return "000000" if code != "000000" else "111111"


# ---------------------------------------------------------------------------
# Configuration and state machine
# ---------------------------------------------------------------------------


def test_configure_allocates_stable_code():
    occ = room_occurrence()
    assert occ.waiting_room_enabled
    assert occ.waiting_room_code
    code = occ.waiting_room_code
    waiting_room.configure(occ, enabled=False)
    waiting_room.configure(occ, enabled=True)
    occ.refresh_from_db()
    assert occ.waiting_room_code == code
    assert waiting_room.room_path(occ) == f"/w/{code}/"
    assert waiting_room.occurrence_for_code(code).pk == occ.pk


def test_configure_rejects_out_of_range_lead():
    occ = room_occurrence()
    with pytest.raises(ValidationError):
        waiting_room.configure(occ, lead_minutes=999)


def test_room_state_transitions_with_explicit_now():
    occ = room_occurrence(starts_in=timedelta(days=2), lead=15)
    before_lead = occ.starts_at - timedelta(minutes=30)
    after_lead = occ.starts_at - timedelta(minutes=5)
    after_end = occ.ends_at + timedelta(minutes=1)

    assert waiting_room.room_state(occ, before_lead).state == waiting_room.WAITING
    assert waiting_room.room_state(occ, after_lead).state == waiting_room.OPEN
    assert waiting_room.room_state(occ, after_end).state == waiting_room.ENDED

    waiting_room.set_admission(occ, "hold")
    assert waiting_room.room_state(occ, after_lead).state == waiting_room.HOLD

    waiting_room.set_admission(occ, "open")
    assert waiting_room.room_state(occ, before_lead).state == waiting_room.OPEN

    waiting_room.set_admission(occ, "ended")
    assert waiting_room.room_state(occ, before_lead).state == waiting_room.ENDED


def test_open_without_join_url_is_preparing():
    occ = room_occurrence(join_url="")
    waiting_room.set_admission(occ, "open")
    assert waiting_room.room_state(occ).state == waiting_room.PREPARING


def test_cancelled_occurrence_state():
    occ = room_occurrence()
    scheduling.cancel_occurrence(occ)
    occ.refresh_from_db()
    assert waiting_room.room_state(occ).state == waiting_room.CANCELLED
    with pytest.raises(ValidationError):
        waiting_room.set_admission(occ, "open")


def test_reschedule_resets_mode_and_bumps_generation():
    occ = room_occurrence()
    waiting_room.set_admission(occ, "hold")
    occ.refresh_from_db()
    generation = occ.admission_generation
    new_local = (timezone.now() + timedelta(days=5)).replace(tzinfo=None, hour=15, minute=0, second=0, microsecond=0)
    scheduling.reschedule_occurrence(occ, local_start=new_local, tz_name="Europe/London")
    occ.refresh_from_db()
    assert occ.admission_mode == Occurrence.Admission.AUTO
    assert occ.admission_generation == generation + 1


# ---------------------------------------------------------------------------
# Admission and check-in
# ---------------------------------------------------------------------------


def test_admit_requires_open_room():
    occ = room_occurrence(starts_in=timedelta(days=2))
    _reg, _token, cap = registered_viewer(occ)
    result = waiting_room.admit(occ, cap)
    assert not result.admitted
    assert result.join_url is None
    assert CheckIn.objects.count() == 0


def test_admit_requires_capability_and_registration():
    occ = room_occurrence()
    waiting_room.set_admission(occ, "open")
    assert waiting_room.admit(occ, None).admitted is False
    stranger = make_person(email="stranger@example.com")
    _token, cap = capability_for(stranger)
    result = waiting_room.admit(occ, cap)
    assert result.admitted is False
    assert result.reason == "not_registered"
    assert CheckIn.objects.count() == 0


def test_admit_twice_records_one_check_in_and_one_message():
    occ = room_occurrence()
    dest = make_destination(event_types=("attendance.checked_in",))
    waiting_room.set_admission(occ, "open")
    reg, _token, cap = registered_viewer(occ)

    first = waiting_room.admit(occ, cap)
    second = waiting_room.admit(occ, cap)

    assert first.admitted and first.created
    assert first.join_url == JOIN_URL
    assert second.admitted and not second.created
    assert CheckIn.objects.filter(occurrence=occ, registration=reg).count() == 1
    assert AttendanceEvent.objects.filter(registration=reg, kind=AttendanceEvent.Kind.CHECKED_IN).count() == 1
    assert OutboxMessage.objects.filter(destination=dest, event_type="attendance.checked_in").count() == 1


def test_readmission_after_reschedule_emits_new_event_same_row():
    occ = room_occurrence()
    waiting_room.set_admission(occ, "open")
    reg, _token, cap = registered_viewer(occ)
    assert waiting_room.admit(occ, cap).created
    new_local = (timezone.now() + timedelta(days=3)).replace(tzinfo=None, hour=10, minute=0, second=0, microsecond=0)
    scheduling.reschedule_occurrence(occ, local_start=new_local, tz_name="Europe/London")
    waiting_room.set_admission(occ, "open")
    again = waiting_room.admit(occ, cap)
    assert again.admitted and again.created
    assert CheckIn.objects.filter(registration=reg).count() == 1
    assert AttendanceEvent.objects.filter(registration=reg, kind=AttendanceEvent.Kind.CHECKED_IN).count() == 2


def test_cancelled_registration_not_admitted():
    occ = room_occurrence()
    waiting_room.set_admission(occ, "open")
    reg, _token, cap = registered_viewer(occ)
    Registration.objects.filter(pk=reg.pk).update(status=Registration.Status.CANCELLED)
    result = waiting_room.admit(occ, cap)
    assert result.admitted is False
    assert result.join_url is None
    assert CheckIn.objects.count() == 0


def test_status_payload_never_contains_join_url():
    occ = room_occurrence()
    _reg, _token, cap = registered_viewer(occ)
    for mode in ("auto", "open", "hold", "ended"):
        waiting_room.set_admission(occ, mode)
        for viewer in (None, cap):
            payload = waiting_room.status_payload(occ, viewer)
            assert JOIN_URL not in repr(payload)
            assert "secret-join-token" not in repr(payload)


def test_finalize_attendance_after_end_is_idempotent():
    occ = room_occurrence()
    waiting_room.set_admission(occ, "open")
    attended_reg, _t, cap = registered_viewer(occ, email="came@example.com")
    absent_reg, _t2, _cap2 = registered_viewer(occ, email="missed@example.com")
    assert waiting_room.admit(occ, cap).admitted

    assert waiting_room.finalize_attendance(occ) is None  # still open: not applicable
    waiting_room.set_admission(occ, "ended")  # finalizes
    attended_reg.refresh_from_db()
    absent_reg.refresh_from_db()
    assert attended_reg.attendance == Registration.Attendance.ATTENDED
    assert absent_reg.attendance == Registration.Attendance.NO_SHOW
    events_before = AttendanceEvent.objects.count()
    assert waiting_room.finalize_attendance(occ) is None
    assert AttendanceEvent.objects.count() == events_before


def test_record_no_show_for_checked_in_registrant_fails():
    occ = room_occurrence()
    waiting_room.set_admission(occ, "open")
    reg, _token, cap = registered_viewer(occ)
    assert waiting_room.admit(occ, cap).admitted
    with pytest.raises(ValidationError):
        scheduling.record_attendance(reg, AttendanceEvent.Kind.NO_SHOW)
    reg.refresh_from_db()
    assert reg.attendance == Registration.Attendance.UNKNOWN


# ---------------------------------------------------------------------------
# Mailbox verification (service level)
# ---------------------------------------------------------------------------


def test_request_verification_rejects_bad_syntax_only():
    occ = room_occurrence()
    with pytest.raises(ValidationError):
        waiting_room.request_verification(occ, "not-an-email", waiting_room.new_pending_token())


def test_verify_code_correct_issues_capability():
    occ = room_occurrence()
    pending = waiting_room.new_pending_token()
    waiting_room.request_verification(occ, "new.person@example.com", pending)
    verified = waiting_room.verify_code(occ, pending, latest_code())
    assert verified.new_person
    assert BrowserCapability.objects.filter(token_hash=sha256(verified.token), person_id=verified.person_id).exists()


def test_verify_code_for_known_person_binds_existing_person():
    occ = room_occurrence()
    person = make_person(email="known@example.com")
    pending = waiting_room.new_pending_token()
    waiting_room.request_verification(occ, "Known@Example.com", pending)
    verified = waiting_room.verify_code(occ, pending, latest_code())
    assert verified.person_id == person.pk
    assert verified.new_person is False


def test_wrong_code_counts_attempts_and_burns_after_five():
    occ = room_occurrence()
    pending = waiting_room.new_pending_token()
    row = waiting_room.request_verification(occ, "guess@example.com", pending)
    code = latest_code()
    bad = wrong_code(code)
    with pytest.raises(waiting_room.VerificationFailed):
        waiting_room.verify_code(occ, pending, bad)
    row.refresh_from_db()
    assert row.attempts == 1
    for _ in range(4):
        with pytest.raises(waiting_room.VerificationFailed):
            waiting_room.verify_code(occ, pending, bad)
    row.refresh_from_db()
    assert row.attempts == 5
    assert row.consumed_at is not None
    with pytest.raises(waiting_room.VerificationFailed):
        waiting_room.verify_code(occ, pending, code)


def test_code_bound_to_requesting_browser():
    occ = room_occurrence()
    pending = waiting_room.new_pending_token()
    waiting_room.request_verification(occ, "bound@example.com", pending)
    with pytest.raises(waiting_room.VerificationFailed):
        waiting_room.verify_code(occ, waiting_room.new_pending_token(), latest_code())


def test_link_token_is_single_use():
    occ = room_occurrence()
    waiting_room.request_verification(occ, "link@example.com", waiting_room.new_pending_token())
    token = latest_link_token(occ.waiting_room_code)
    assert waiting_room.link_is_usable(occ, token)
    assert waiting_room.verify_link(occ, token).token
    assert not waiting_room.link_is_usable(occ, token)
    with pytest.raises(waiting_room.VerificationFailed):
        waiting_room.verify_link(occ, token)


def test_link_token_bound_to_occurrence():
    occ = room_occurrence()
    other = room_occurrence()
    waiting_room.request_verification(occ, "scope@example.com", waiting_room.new_pending_token())
    token = latest_link_token(occ.waiting_room_code)
    with pytest.raises(waiting_room.VerificationFailed):
        waiting_room.verify_link(other, token)


def test_expired_verification_fails():
    occ = room_occurrence()
    pending = waiting_room.new_pending_token()
    row = waiting_room.request_verification(occ, "late@example.com", pending)
    code = latest_code()
    token = latest_link_token(occ.waiting_room_code)
    WaitingRoomVerification.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    with pytest.raises(waiting_room.VerificationFailed):
        waiting_room.verify_code(occ, pending, code)
    assert not waiting_room.link_is_usable(occ, token)
    with pytest.raises(waiting_room.VerificationFailed):
        waiting_room.verify_link(occ, token)


def test_per_email_limit_suppresses_fourth_message():
    occ = room_occurrence()
    rows = [
        waiting_room.request_verification(occ, "busy@example.com", waiting_room.new_pending_token())
        for _ in range(4)
    ]
    assert all(r is not None for r in rows[:3])
    assert rows[3] is None
    assert len(mail.outbox) == 3


def test_verification_email_never_contains_join_url():
    occ = room_occurrence()
    waiting_room.request_verification(occ, "mailcheck@example.com", waiting_room.new_pending_token())
    assert JOIN_URL not in mail.outbox[-1].body
    assert JOIN_URL not in mail.outbox[-1].subject


# ---------------------------------------------------------------------------
# HTTP contract: /w/<code>/...
# ---------------------------------------------------------------------------


def _use_cookie(client, token):
    client.cookies.pop(COOKIE, None)
    if token:
        client.cookies[COOKIE] = token


def test_unknown_or_disabled_code_404(client):
    occ = room_occurrence()
    assert client.get("/w/doesnotexist1/").status_code == 404
    waiting_room.configure(occ, enabled=False)
    assert client.get(f"/w/{occ.waiting_room_code}/").status_code == 404


def test_draft_event_room_404(client):
    occ = room_occurrence()
    type(occ.event).objects.filter(pk=occ.event_id).update(visibility="draft")
    assert client.get(f"/w/{occ.waiting_room_code}/").status_code == 404


def test_room_page_never_contains_join_url(client):
    occ = room_occurrence()
    _reg, token, _cap = registered_viewer(occ)
    waiting_room.set_admission(occ, "open")
    _use_cookie(client, token)
    resp = client.get(f"/w/{occ.waiting_room_code}/")
    assert resp.status_code == 200
    assert JOIN_URL.encode() not in resp.content
    assert "no-store" in resp.get("Cache-Control", "")


def test_status_json_has_no_join_url(client):
    occ = room_occurrence()
    _reg, token, _cap = registered_viewer(occ)
    waiting_room.set_admission(occ, "open")
    _use_cookie(client, token)
    resp = client.get(f"/w/{occ.waiting_room_code}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "open"
    assert data["admit"] is True
    assert data["viewer"] == {"recognized": True, "registered": True}
    assert JOIN_URL.encode() not in resp.content
    assert "no-store" in resp.get("Cache-Control", "")


def test_enter_when_open_redirects_to_join_url(client):
    occ = room_occurrence()
    reg, token, _cap = registered_viewer(occ)
    waiting_room.set_admission(occ, "open")
    _use_cookie(client, token)
    resp = client.post(f"/w/{occ.waiting_room_code}/enter")
    assert resp.status_code == 303
    assert resp["Location"] == JOIN_URL
    assert "no-store" in resp.get("Cache-Control", "")
    assert resp.get("Referrer-Policy") == "no-referrer"
    assert JOIN_URL.encode() not in resp.content
    assert CheckIn.objects.filter(registration=reg).count() == 1


def test_enter_when_waiting_redirects_back_to_room(client):
    occ = room_occurrence(starts_in=timedelta(days=2))
    reg, token, _cap = registered_viewer(occ)
    _use_cookie(client, token)
    resp = client.post(f"/w/{occ.waiting_room_code}/enter")
    assert resp.status_code == 303
    assert resp["Location"] != JOIN_URL
    assert f"/w/{occ.waiting_room_code}/" in resp["Location"]
    assert CheckIn.objects.filter(registration=reg).count() == 0


def test_enter_anonymous_never_redirects_to_join(client):
    occ = room_occurrence()
    waiting_room.set_admission(occ, "open")
    _use_cookie(client, None)
    resp = client.post(f"/w/{occ.waiting_room_code}/enter")
    assert resp.status_code == 303
    assert JOIN_URL not in resp["Location"]


def test_identify_identical_for_known_and_unknown_email(client, django_user_model):
    from django.test import Client

    occ = room_occurrence()
    make_person(email="already.known@example.com")
    known = Client().post(f"/w/{occ.waiting_room_code}/identify", {"email": "already.known@example.com"})
    unknown = Client().post(f"/w/{occ.waiting_room_code}/identify", {"email": "brand.new@example.com"})
    assert known.status_code == unknown.status_code == 303
    assert known["Location"] == unknown["Location"]
    assert known["Location"].endswith(f"/w/{occ.waiting_room_code}/?step=code")
    assert len(mail.outbox) == 2
    assert {m.to[0] for m in mail.outbox} == {"already.known@example.com", "brand.new@example.com"}
    pending = known.cookies.get(waiting_room.PENDING_COOKIE)
    assert pending is not None and pending.value
    assert pending["httponly"]
    assert pending["path"] == "/w/"


def test_identify_rate_limit_suppression_same_response(client):
    occ = room_occurrence()
    responses = [
        client.post(f"/w/{occ.waiting_room_code}/identify", {"email": "spam.me@example.com"}) for _ in range(4)
    ]
    assert {r.status_code for r in responses} == {303}
    assert len({r["Location"] for r in responses}) == 1
    assert len(mail.outbox) == 3


def test_identify_invalid_email_400(client):
    occ = room_occurrence()
    resp = client.post(f"/w/{occ.waiting_room_code}/identify", {"email": "nope"})
    assert resp.status_code == 400
    assert len(mail.outbox) == 0


def test_verify_http_sets_capability_cookie(client):
    occ = room_occurrence()
    client.post(f"/w/{occ.waiting_room_code}/identify", {"email": "http.verify@example.com"})
    code = latest_code()
    bad = client.post(f"/w/{occ.waiting_room_code}/verify", {"code": wrong_code(code)})
    assert bad.status_code == 400
    good = client.post(f"/w/{occ.waiting_room_code}/verify", {"code": code})
    assert good.status_code == 303
    assert f"/w/{occ.waiting_room_code}/" in good["Location"]
    cap_cookie = good.cookies.get(COOKIE)
    assert cap_cookie is not None and cap_cookie.value
    assert BrowserCapability.objects.filter(token_hash=sha256(cap_cookie.value)).exists()


def test_verify_link_get_does_not_consume(client):
    occ = room_occurrence()
    waiting_room.request_verification(occ, "scanner@example.com", waiting_room.new_pending_token())
    token = latest_link_token(occ.waiting_room_code)
    path = f"/w/{occ.waiting_room_code}/verify/{token}"
    assert client.get(path).status_code == 200
    assert client.get(path).status_code == 200
    assert waiting_room.link_is_usable(occ, token)
    resp = client.post(path)
    assert resp.status_code == 303
    assert resp.cookies.get(COOKIE) is not None
    assert not waiting_room.link_is_usable(occ, token)


# ---------------------------------------------------------------------------
# Concurrency (PostgreSQL only: SQLite serializes writers differently)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(connection.vendor != "postgresql", reason="row locking semantics need PostgreSQL")
@pytest.mark.django_db(transaction=True)
def test_concurrent_admit_creates_one_check_in():
    occ = room_occurrence()
    waiting_room.set_admission(occ, "open")
    reg, _token, cap = registered_viewer(occ)
    barrier = threading.Barrier(6)
    results, errors = [], []

    def worker():
        from django.db import connection as thread_conn

        try:
            barrier.wait()
            results.append(waiting_room.admit(occ, cap))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            thread_conn.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert all(r.admitted for r in results)
    assert sum(1 for r in results if r.created) == 1
    assert CheckIn.objects.filter(registration=reg).count() == 1
    assert AttendanceEvent.objects.filter(registration=reg, kind=AttendanceEvent.Kind.CHECKED_IN).count() == 1
