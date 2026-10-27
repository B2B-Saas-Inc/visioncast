"""Attendance import: pipeline, provider pulls (fake transport), file parsing, REST, MCP,
studio and retention (docs/hosting.md, "Attendance import"). No network anywhere."""
import json
import re
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from tests.conftest import make_api_key, make_destination, make_event, make_form, make_occurrence, make_person, make_registration
from tests.test_hosting import TOKEN, During, Fake
from tests.test_mcp import tool_names
from tests.test_mcp_config import confirm, run, text_of
from webinars import attendance_import as pipeline
from webinars.hosting import base, service
from webinars.models import (
    AttendanceEvent,
    AttendanceImport,
    AttendanceImportRow,
    AuditLog,
    CheckIn,
    OutboxMessage,
    ProviderRegistrant,
    ProvisioningOperation,
    Registration,
)

pytestmark = pytest.mark.django_db

ZOOM_ID = "81234567890"
URL = "/api/v1/manage/occurrences/{}/attendance/imports"


@pytest.fixture(autouse=True)
def hosting_settings(settings, monkeypatch):
    settings.WEBHOOK_HOST_ALLOWLIST = ["zoom.us", "api.zoom.us", "platform.riverside.fm", "api.wistia.com", "hooks.example.net"]
    base.clear_token_cache()

    def no_network(*a, **kw):
        raise AssertionError("no provider call may be made here")

    monkeypatch.setattr(base, "default_transport", no_network)
    yield settings
    base.clear_token_cache()


def _ended_occurrence(slug="att", waiting_room=False, **kw):
    """An occurrence that ended an hour ago (no-shows need an ended session)."""
    form = make_form(version=f"fa-{slug}")
    event = make_event(form, slug=slug)
    starts = timezone.now() - timedelta(hours=2)
    occ = make_occurrence(event, starts_at=starts, duration=60, join_url="", **kw)
    if waiting_room:
        occ.waiting_room_enabled = True
        occ.waiting_room_code = f"room{slug}"
        occ.save()
    return occ


def _reg(occ, email, **kw):
    return make_registration(occ, person=make_person(email=email, first_name="Pat"), **kw)


def _zoom_linked(occ):
    account, _w = service.save_account(
        provider="zoom", name="z", config={"host_user_id": "host@example.com"},
        posted_secrets={"account_id": "acct_123456", "client_id": "client-id-abc", "client_secret": "client-secret-xyz"}, enabled=True)
    out = service.execute(occ, account, "attach", external_id=ZOOM_ID, transport=Fake(TOKEN, (200, {"id": int(ZOOM_ID)})))
    assert out["state"] == "linked"
    base.clear_token_cache()
    return account


def _participants(*emails, token=None, **extra):
    items = [{"id": f"p-{i}", "user_id": f"u{i}", "user_email": e, "name": f"Person {i}", "join_time": "2026-10-20T10:00:00Z",
              "leave_time": "2026-10-20T10:30:00Z", "duration": 1800, **extra} for i, e in enumerate(emails)]
    body = {"page_count": 1, "page_size": 100, "total_records": len(items), "participants": items, "next_page_token": token or ""}
    return 200, body


def _rows(*emails, **extra):
    return [{"email": e, "name": "N", "duration_seconds": 600, **extra} for e in emails]


# ---------------------------------------------------------------- pipeline: matching, dedupe, apply


def test_file_import_matches_by_email_dedupes_and_flags_ambiguity(db):
    occ = _ended_occurrence("m1")
    reg = _reg(occ, "Ada@Example.com")
    other = _reg(occ, "other@example.com")
    make_registration(occ, person=make_person(email="gone@example.com"), status=Registration.Status.CANCELLED)
    session = service.get_or_create_session(occ, service.save_account(
        provider="riverside", name="rv", config={}, posted_secrets={"api_key": "rv-api-key-1"}, enabled=True)[0])
    ProviderRegistrant.objects.create(registration=other, session=session, state="linked", external_id="ext-other")
    rows = pipeline.normalise_rows([
        {"email": "ada@example.com", "name": "Ada", "duration_seconds": 600, "joined_at": "2026-10-20T10:00:00Z"},
        {"email": "ADA@example.com", "name": "", "duration_seconds": 300, "joined_at": "2026-10-20T09:50:00Z"},
        {"email": "nobody@example.com", "name": "Nobody"},
        {"email": "gone@example.com"},
        {"external_id": "ext-other", "email": "ada@example.com"},  # id says one person, email another
    ])
    imp = pipeline.create_file_import(occ, rows)
    assert imp.state == "needs_review" and imp.source == "file"
    by_match = {r.match: r for r in imp.rows.all()}
    assert imp.row_count == 4 and imp.matched_count == 1 and imp.unmatched_count == 2 and imp.ambiguous_count == 1
    matched = by_match["matched"]
    assert matched.registration_id == reg.pk and matched.duration_seconds == 900 and matched.display_name == "Ada"
    assert matched.email_key == "ada@example.com" and matched.email_masked == "a***@example.com"
    assert matched.joined_at.isoformat().startswith("2026-10-20T09:50")
    assert by_match["ambiguous"].registration_id is None
    assert {r.email_masked for r in imp.rows.filter(match="unmatched")} == {"n***@example.com", "g***@example.com"}
    assert Registration.objects.get(pk=reg.pk).attendance == "unknown"  # nothing applied yet
    blob = json.dumps(list(AuditLog.objects.values_list("metadata", flat=True)))
    assert "ada@example.com" not in blob.lower() and "Ada" not in blob


def test_apply_is_idempotent_and_emits_once(db):
    make_destination(event_types=("attendance.recorded",))
    occ = _ended_occurrence("m2")
    reg = _reg(occ, "one@example.com")
    attended = _reg(occ, "two@example.com")
    from webinars.scheduling import record_attendance

    record_attendance(attended, "attended", source="admin")
    assert OutboxMessage.objects.count() == 1
    imp = pipeline.create_file_import(occ, pipeline.normalise_rows(_rows("one@example.com", "two@example.com")))
    preview = pipeline.apply_preview(imp)
    assert preview["toApply"] == 1 and preview["alreadyAttended"] == 1 and not preview["problems"]
    imp = pipeline.apply_import(imp)
    assert imp.state == "applied" and imp.applied_count == 1
    assert Registration.objects.get(pk=reg.pk).attendance == "attended"
    assert OutboxMessage.objects.count() == 2
    event = AttendanceEvent.objects.get(registration=reg, kind="attended")
    assert event.source == "import:file"
    assert pipeline.apply_preview(imp)["toApply"] == 0
    pipeline.apply_import(imp)
    again = pipeline.create_file_import(occ, pipeline.normalise_rows(_rows("one@example.com", "two@example.com")))
    pipeline.apply_import(again)
    assert OutboxMessage.objects.count() == 2
    assert AttendanceEvent.objects.filter(registration=reg).count() == 1


def test_resolve_and_ignore_rows(db):
    occ = _ended_occurrence("m3")
    reg = _reg(occ, "known@example.com")
    imp = pipeline.create_file_import(occ, pipeline.normalise_rows(_rows("x@example.com", "y@example.com")))
    rows = list(imp.rows.all())
    other_occ = _ended_occurrence("m3b")
    foreign = _reg(other_occ, "foreign@example.com")
    with pytest.raises(ValidationError):
        pipeline.resolve_row(imp, rows[0], registration=foreign)
    row = pipeline.resolve_row(imp, rows[0], registration=reg)
    assert row.match == "resolved" and row.registration_id == reg.pk and row.applied_at is not None
    assert Registration.objects.get(pk=reg.pk).attendance == "attended"
    with pytest.raises(ValidationError):
        pipeline.resolve_row(imp, rows[0], ignore=True)  # already resolved
    row2 = pipeline.resolve_row(imp, rows[1], ignore=True)
    assert row2.match == "ignored"
    imp.refresh_from_db()
    assert (imp.matched_count, imp.unmatched_count, imp.ignored_count, imp.applied_count) == (1, 0, 1, 1)


def test_no_shows_only_after_apply_and_end_and_skip_checked_in(db):
    occ = _ended_occurrence("m4")
    attended = _reg(occ, "a@example.com")
    absent = _reg(occ, "b@example.com")
    checked = _reg(occ, "c@example.com")
    CheckIn.objects.create(registration=checked, occurrence=occ, admission_generation=occ.admission_generation)
    imp = pipeline.create_file_import(occ, pipeline.normalise_rows(_rows("a@example.com")))
    with pytest.raises(ValidationError):
        pipeline.mark_no_shows(imp)  # not applied yet
    imp = pipeline.apply_import(imp)
    future = _ended_occurrence("m4f")
    future.starts_at = timezone.now() + timedelta(hours=1)
    future.ends_at = future.starts_at + timedelta(hours=1)
    future.save()
    other = pipeline.apply_import(pipeline.create_file_import(future, pipeline.normalise_rows(_rows("zz@example.com"))))
    assert "not ended" in " ".join(pipeline.no_show_preview(other)["problems"])
    preview = pipeline.no_show_preview(imp)
    assert preview["remaining"] == 2 and preview["checkedIn"] == 1 and not preview["problems"]
    imp = pipeline.mark_no_shows(imp)
    assert imp.no_show_count == 1 and imp.no_show_refused_count == 1
    assert Registration.objects.get(pk=absent.pk).attendance == "no_show"
    assert Registration.objects.get(pk=checked.pk).attendance == "unknown"
    assert Registration.objects.get(pk=attended.pk).attendance == "attended"
    assert AttendanceEvent.objects.get(registration=absent).source == "import:file"
    with pytest.raises(ValidationError):
        pipeline.mark_no_shows(imp)  # once per import


def test_no_shows_compose_with_waiting_room_finalization(db):
    occ = _ended_occurrence("m5", waiting_room=True)
    checked = _reg(occ, "c@example.com")
    absent = _reg(occ, "b@example.com")
    CheckIn.objects.create(registration=checked, occurrence=occ, admission_generation=occ.admission_generation)
    imp = pipeline.apply_import(pipeline.create_file_import(occ, pipeline.normalise_rows(_rows("nobody@example.com"))))
    assert pipeline.no_show_preview(imp)["waitingRoomFinalize"] is True
    imp = pipeline.mark_no_shows(imp)
    occ.refresh_from_db()
    assert occ.attendance_finalized_at is not None
    assert Registration.objects.get(pk=checked.pk).attendance == "attended"
    assert Registration.objects.get(pk=absent.pk).attendance == "no_show"
    assert imp.no_show_count == 1 and imp.no_show_refused_count == 0


# ---------------------------------------------------------------- provider pulls


def test_zoom_pull_paginates_parses_and_matches(db):
    occ = _ended_occurrence("z1")
    reg = _reg(occ, "p1@example.com")
    _zoom_linked(occ)
    fake = Fake(TOKEN, _participants("p0@example.com", "p1@example.com", token="tok-2"), _participants("p2@example.com"))
    imp = pipeline.pull_from_provider(occ, transport=fake)
    assert imp.state == "needs_review" and imp.source == "zoom" and imp.complete is True
    urls = [c["url"] for c in fake.calls[1:]]
    assert urls[0] == f"https://api.zoom.us/v2/past_webinars/{ZOOM_ID}/participants?page_size=100"
    assert urls[1].endswith("&next_page_token=tok-2") and all(c["method"] == "GET" for c in fake.calls[1:])
    assert all(c["headers"]["Authorization"] == "Bearer zoom-access-token" for c in fake.calls[1:])
    assert imp.row_count == 3 and imp.matched_count == 1 and imp.unmatched_count == 2
    row = imp.rows.get(match="matched")
    assert row.registration_id == reg.pk and row.external_id == "p-1" and row.duration_seconds == 1800
    assert row.joined_at.isoformat().startswith("2026-10-20T10:00") and row.email_masked == "p***@example.com"
    op = ProvisioningOperation.objects.get(action="attendance")
    assert op.status == "succeeded" and imp.operation_id == op.pk
    assert service.serialize_session(service.session_for(occ))["state"] == "linked"
    assert Registration.objects.get(pk=reg.pk).attendance == "unknown"


def test_zoom_pull_page_cap_marks_incomplete(db, monkeypatch):
    from webinars.hosting import zoom

    occ = _ended_occurrence("z2")
    _zoom_linked(occ)
    monkeypatch.setattr(zoom, "ATTENDANCE_MAX_PAGES", 2)
    fake = Fake(TOKEN, _participants("a@example.com", token="t1"), _participants("b@example.com", token="t2"),
                _participants("c@example.com"))
    imp = pipeline.pull_from_provider(occ, transport=fake)
    assert len(fake.calls) == 3 and imp.complete is False and imp.row_count == 2
    assert "cut short" in imp.last_error


def test_zoom_pull_failure_is_retryable_and_never_unknown(db):
    occ = _ended_occurrence("z3")
    _zoom_linked(occ)
    for transport in (Fake(TOKEN, (503, {"message": "busy"})), Fake(TimeoutError()), Fake((200, {"nope": True}))):
        imp = pipeline.pull_from_provider(occ, transport=transport)
        assert imp.state == "failed" and imp.last_error.startswith("attendance:") or imp.last_error.startswith("token:")
        assert "busy" not in imp.last_error
    assert set(ProvisioningOperation.objects.filter(action="attendance").values_list("status", flat=True)) == {"failed"}
    assert not pipeline.pull_problems(occ)[1]
    assert service.session_for(occ).state == "linked"
    with pytest.raises(Exception):
        service.reconcile(occ, "not_created")  # nothing to reconcile: pulls never leave unknown outcomes


def test_pull_refused_for_unsupported_provider_and_unlinked(db):
    occ = _ended_occurrence("w1")
    assert "not linked" in pipeline.pull_problems(occ)[1][0]
    account, _w = service.save_account(provider="wistia", name="w", config={}, posted_secrets={"api_token": "wistia-token-1"}, enabled=True)
    service.execute(occ, account, "create", transport=Fake((201, {"id": "abc123"})))
    problems = pipeline.pull_problems(occ)[1]
    assert any("does not support attendance import" in p for p in problems)
    with pytest.raises(ValidationError):
        pipeline.pull_from_provider(occ, transport=Fake())
    assert not AttendanceImport.objects.exists()
    sy, _w = service.save_account(provider="streamyard", name="s", config={}, posted_secrets={}, enabled=True)
    occ2 = _ended_occurrence("w2")
    service.set_manual_link(occ2, sy, join_url="https://streamyard.com/watch/abc")
    assert any("does not support" in p for p in pipeline.pull_problems(occ2)[1])


def test_lost_pull_recovered_and_late_outcome_fenced(db):
    occ = _ended_occurrence("z4")
    _zoom_linked(occ)

    def lose():
        assert service.recover_stale_operations(now=timezone.now() + timedelta(minutes=6)) >= 1
        imp = AttendanceImport.objects.get()
        assert imp.state == "failed" and "lost in flight" in imp.last_error

    fake = During(TOKEN, _participants("late@example.com"), hooks={1: lose})
    imp = pipeline.pull_from_provider(occ, transport=fake)
    assert imp.state == "failed" and imp.row_count == 0 and not imp.rows.exists()
    op = ProvisioningOperation.objects.get(action="attendance")
    assert op.status == "failed" and "late outcome: succeeded" in op.error
    assert service.session_for(occ).state == "linked"
    assert AuditLog.objects.filter(action="attendance_import.late_outcome").exists()
    ok = pipeline.pull_from_provider(occ, transport=Fake(_participants("late@example.com")))
    assert ok.state == "needs_review"


def test_riverside_pull_matches_external_id_first(db):
    occ = _ended_occurrence("r1")
    account, _w = service.save_account(provider="riverside", name="rv", config={}, posted_secrets={"api_key": "rv-api-key-1"}, enabled=True)
    service.execute(occ, account, "attach", external_id="rv-event-r1", transport=Fake((200, {"id": "rv-event-r1"})))
    reg = _reg(occ, "changed@example.com")
    assert service.ensure_registrant(reg, transport=Fake((201, {"id": "rv-reg-7", "join_link": "https://riverside.fm/x?t=1"}))) == "linked"
    by_email = _reg(occ, "mail@example.com")
    fake = Fake((200, {"registrants": [
        {"id": "rv-reg-7", "email": "old@example.com", "first_name": "Pat", "last_name": "Q", "attended": True, "duration": 900},
        {"id": "rv-reg-8", "email": "mail@example.com", "name": "Mail", "joined_at": "2026-10-20T10:00:00Z"},
        {"id": "rv-reg-9", "email": "absent@example.com", "attended": False},
    ]}))
    imp = pipeline.pull_from_provider(occ, transport=fake)
    assert fake.calls[0]["url"] == "https://platform.riverside.fm/api/v1/events/rv-event-r1/registrants"
    assert imp.source == "riverside" and imp.row_count == 2 and imp.matched_count == 2
    by_ext = imp.rows.get(external_id="rv-reg-7")
    assert by_ext.registration_id == reg.pk and by_ext.display_name == "Pat Q" and by_ext.duration_seconds == 900
    assert imp.rows.get(external_id="rv-reg-8").registration_id == by_email.pk
    assert "riverside.fm" not in json.dumps(pipeline.serialize_import(imp, rows=True))


# ---------------------------------------------------------------- file parsing


def test_csv_zoom_headers_minutes_preamble_and_attended_flag(db):
    csv = ("Attendee Report\nTopic,Webinar X\n\n"
           "Attended,User Name (Original Name),First Name,Last Name,Email,Join Time,Leave Time,Time in Session (minutes)\n"
           "Yes,Ada L,Ada,Lovelace,ada@example.com,10/20/2026 10:00:05 AM,10/20/2026 10:45:05 AM,45\n"
           "No,Bob,Bob,B,bob@example.com,--,--,0\n"
           "Yes,,Cy,C,cy@example.com,2026-10-20T10:00:00Z,2026-10-20T10:20:00Z,20.5\n").encode()
    rows = pipeline.parse_csv(csv)
    assert [r["email_key"] for r in rows] == ["ada@example.com", "cy@example.com"]
    assert rows[0]["duration_seconds"] == 2700 and rows[0]["name"] == "Ada L"
    assert rows[0]["joined_at"].isoformat().startswith("2026-10-20T10:00:05")
    assert rows[1]["name"] == "Cy C" and rows[1]["duration_seconds"] == 1230
    streamyard = b"\xef\xbb\xbfName,Email Address,Duration\nSy Person,sy@example.com,00:12:30\n"
    sy = pipeline.parse_csv(streamyard)
    assert sy[0]["email_key"] == "sy@example.com" and sy[0]["duration_seconds"] == 750
    seconds = pipeline.parse_csv(b"email,duration_seconds\nq@example.com,90\n")
    assert seconds[0]["duration_seconds"] == 90


def test_file_limits_and_validation(db):
    with pytest.raises(ValidationError):
        pipeline.parse_csv(b"x" * (pipeline.MAX_FILE_BYTES + 1))
    with pytest.raises(ValidationError):
        pipeline.parse_csv(b"name,other\nA,B\n")
    body = "email\n" + "".join(f"u{i}@example.com\n" for i in range(pipeline.MAX_ROWS + 1))
    with pytest.raises(ValidationError):
        pipeline.parse_csv(body.encode())
    with pytest.raises(ValidationError):
        pipeline.normalise_rows([{"email": 5}])
    with pytest.raises(ValidationError):
        pipeline.normalise_rows([{"email": "a@example.com", "duration_seconds": -1}])
    with pytest.raises(ValidationError):
        pipeline.normalise_rows([{"email": "a@example.com", "bogus": 1}])
    assert pipeline.normalise_rows([{"name": "only a name"}]) == []
    occ = _ended_occurrence("f1")
    with pytest.raises(ValidationError):
        pipeline.create_file_import(occ, [])


# ---------------------------------------------------------------- REST


def _auth(raw, token=None):
    headers = {"Authorization": f"Bearer {raw}"}
    if token:
        headers["Webinar-Confirm-Token"] = token
    return headers


def _post(client, raw, path, body, token=None):
    return client.post(path, data=json.dumps(body), content_type="application/json", headers=_auth(raw, token))


@pytest.fixture
def att_key(db):
    return make_api_key(("attendance_write",), name="att")


def test_rest_file_import_apply_confirm_and_masking(client, att_key):
    make_destination(event_types=("attendance.recorded",))
    occ = _ended_occurrence("api1")
    reg = _reg(occ, "rest@example.com")
    resp = _post(client, att_key, URL.format(occ.pk), {"source": "file", "rows": [
        {"email": "rest@example.com", "name": "Rest", "joinedAt": "2026-10-20T10:00:00Z", "durationSeconds": 60},
        {"email": "unknown@example.com"}]})
    assert resp.status_code == 201, resp.content
    data = resp.json()
    assert data["state"] == "needs_review" and data["counts"] == {"rows": 2, "matched": 1, "unmatched": 1, "ambiguous": 0, "ignored": 0, "applied": 0}
    text = resp.content.decode()
    assert "rest@example.com" not in text and "r***@example.com" in text and "unknown@example.com" not in text
    import_id = data["id"]
    apply_url = URL.format(occ.pk) + f"/{import_id}/apply"
    first = _post(client, att_key, apply_url, {})
    assert first.status_code == 428, first.content
    assert first.json()["preview"]["toApply"] == 1 and first.json()["confirmToken"]
    assert Registration.objects.get(pk=reg.pk).attendance == "unknown"
    token = first.json()["confirmToken"]
    done = _post(client, att_key, apply_url, {}, token=token)
    assert done.status_code == 200, done.content
    assert done.json()["state"] == "applied" and Registration.objects.get(pk=reg.pk).attendance == "attended"
    assert OutboxMessage.objects.count() == 1
    replay = _post(client, att_key, apply_url, {}, token=token)
    assert replay.status_code == 400 and replay.json()["error"]["code"] in ("confirmation_used", "confirmation_mismatch")
    again = _post(client, att_key, apply_url, {})
    assert again.status_code == 428 and again.json()["preview"]["toApply"] == 0
    assert _post(client, att_key, apply_url, {}, token=again.json()["confirmToken"]).status_code == 200
    assert OutboxMessage.objects.count() == 1
    listing = client.get(URL.format(occ.pk), headers=_auth(att_key)).json()
    assert [i["id"] for i in listing["results"]] == [import_id] and "rows" not in listing["results"][0]
    detail = client.get(URL.format(occ.pk) + f"/{import_id}", headers=_auth(att_key))
    assert detail.status_code == 200 and len(detail.json()["rows"]) == 2
    assert "rest@example.com" not in detail.content.decode()
    other = _ended_occurrence("api1b")
    assert client.get(URL.format(other.pk) + f"/{import_id}", headers=_auth(att_key)).status_code == 404


def test_rest_resolve_and_no_shows(client, att_key):
    occ = _ended_occurrence("api2")
    reg = _reg(occ, "known@example.com")
    absent = _reg(occ, "absent@example.com")
    created = _post(client, att_key, URL.format(occ.pk), {"source": "file", "rows": [{"email": "mystery@example.com"}]}).json()
    row_id = created["rows"][0]["id"]
    base_url = URL.format(occ.pk) + f"/{created['id']}"
    no_shows = _post(client, att_key, base_url + "/no-shows", {})
    assert no_shows.status_code == 409  # not applied yet
    bad = _post(client, att_key, base_url + f"/rows/{row_id}/resolve", {"registrationId": str(absent.pk), "ignore": True})
    assert bad.status_code == 400
    resolved = _post(client, att_key, base_url + f"/rows/{row_id}/resolve", {"registrationId": str(reg.pk)})
    assert resolved.status_code == 200 and resolved.json()["row"]["match"] == "resolved"
    assert Registration.objects.get(pk=reg.pk).attendance == "attended"
    assert _post(client, att_key, base_url + f"/rows/{row_id}/resolve", {"ignore": True}).status_code == 409
    token = _post(client, att_key, base_url + "/apply", {}).json()["confirmToken"]
    assert _post(client, att_key, base_url + "/apply", {}, token=token).status_code == 200
    first = _post(client, att_key, base_url + "/no-shows", {})
    assert first.status_code == 428 and first.json()["preview"]["remaining"] == 1
    done = _post(client, att_key, base_url + "/no-shows", {}, token=first.json()["confirmToken"])
    assert done.status_code == 200 and done.json()["noShows"]["marked"] == 1
    assert Registration.objects.get(pk=absent.pk).attendance == "no_show"
    assert _post(client, att_key, base_url + "/no-shows", {}).status_code == 409


def test_rest_provider_pull_and_refusals(client, att_key, monkeypatch):
    occ = _ended_occurrence("api3")
    _reg(occ, "p1@example.com")
    refused = _post(client, att_key, URL.format(occ.pk), {"source": "provider"})
    assert refused.status_code == 409 and "not linked" in refused.content.decode()
    assert _post(client, att_key, URL.format(occ.pk), {"source": "provider", "rows": []}).status_code == 400
    assert _post(client, att_key, URL.format(occ.pk), {"source": "file"}).status_code == 400
    assert _post(client, att_key, URL.format(occ.pk), {"source": "nope"}).status_code == 400
    _zoom_linked(occ)
    monkeypatch.setattr(base, "default_transport", Fake(TOKEN, _participants("p0@example.com", "p1@example.com")))
    resp = _post(client, att_key, URL.format(occ.pk), {"source": "provider"})
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["source"] == "zoom" and body["state"] == "needs_review" and body["counts"]["matched"] == 1
    text = resp.content.decode()
    assert "client-secret-xyz" not in text and "zoom-access-token" not in text and "p1@example.com" not in text


def test_rest_scopes(client, att_key):
    occ = _ended_occurrence("api4")
    imp = pipeline.create_file_import(occ, pipeline.normalise_rows(_rows("s@example.com")))
    ro = make_api_key(("config_read",), name="ro")
    assert client.get(URL.format(occ.pk), headers=_auth(ro)).status_code == 200
    assert client.get(URL.format(occ.pk) + f"/{imp.pk}", headers=_auth(ro)).status_code == 200
    assert _post(client, ro, URL.format(occ.pk), {"source": "file", "rows": []}).status_code == 403
    assert _post(client, ro, URL.format(occ.pk) + f"/{imp.pk}/apply", {}).status_code == 403
    for scopes in (("content_write",), ("hosting_provision",), ("reporting",)):
        raw = make_api_key(scopes, name="x")
        assert client.get(URL.format(occ.pk), headers=_auth(raw)).status_code == 403
        assert _post(client, raw, URL.format(occ.pk) + f"/{imp.pk}/apply", {}).status_code == 403
    assert AttendanceImport.objects.get(pk=imp.pk).state == "needs_review"


def test_openapi_documents_attendance_routes(client):
    from webinars.openapi import build_spec

    spec = build_spec()
    base_path = "/api/v1/manage/occurrences/{id}/attendance/imports"
    assert "config_read" in spec["paths"][base_path]["get"]["description"]
    assert "428" in spec["paths"][base_path + "/{importId}/apply"]["post"]["responses"]
    assert "428" in spec["paths"][base_path + "/{importId}/no-shows"]["post"]["responses"]
    assert "rowId" in [p["name"] for p in spec["paths"][base_path + "/{importId}/rows/{rowId}/resolve"]["parameters"]]
    row = spec["components"]["schemas"]["AttendanceImportRow"]["properties"]
    assert "emailMasked" in row and "email" not in row and "joinUrl" not in row


# ---------------------------------------------------------------- MCP


def test_mcp_tools_scoped(client, att_key):
    names = {"list_attendance_imports", "import_session_attendance", "apply_attendance_import", "resolve_attendance_row",
             "mark_session_no_shows"}
    assert names <= tool_names(client, att_key)
    ro = tool_names(client, make_api_key(("config_read",), name="ro"))
    assert "list_attendance_imports" in ro and not (names - {"list_attendance_imports"}) & ro
    assert not names & tool_names(client, make_api_key(("hosting_provision", "content_write"), name="hp"))


def test_mcp_import_apply_dry_run_and_confirm(client, att_key):
    make_destination(event_types=("attendance.recorded",))
    occ = _ended_occurrence("mcp1")
    reg = _reg(occ, "mcp@example.com")
    args = {"occurrenceId": str(occ.pk), "source": "file", "rows": [{"email": "mcp@example.com", "durationSeconds": 5}]}
    dry = run(client, att_key, "import_session_attendance", args)
    assert not dry["isError"], dry
    assert dry["structuredContent"]["preview"]["counts"]["matched"] == 1 and not AttendanceImport.objects.exists()
    _d, done = confirm(client, att_key, "import_session_attendance", args)
    assert not done["isError"], done
    assert "mcp@example.com" not in text_of(done)
    imp = AttendanceImport.objects.get()
    assert imp.state == "needs_review"
    listed = run(client, att_key, "list_attendance_imports", {"occurrenceId": str(occ.pk), "importId": str(imp.pk)})
    assert not listed["isError"] and "m***@example.com" in text_of(listed) and "mcp@example.com" not in text_of(listed)
    apply_args = {"occurrenceId": str(occ.pk), "importId": str(imp.pk)}
    dry = run(client, att_key, "apply_attendance_import", apply_args)
    assert not dry["isError"] and dry["structuredContent"]["preview"]["toApply"] == 1
    assert Registration.objects.get(pk=reg.pk).attendance == "unknown"
    _d, done = confirm(client, att_key, "apply_attendance_import", apply_args)
    assert not done["isError"], done
    assert Registration.objects.get(pk=reg.pk).attendance == "attended" and OutboxMessage.objects.count() == 1
    stale = run(client, att_key, "apply_attendance_import",
                {**apply_args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert stale["isError"]
    ns = run(client, att_key, "mark_session_no_shows", apply_args)
    assert not ns["isError"], ns
    _d, done = confirm(client, att_key, "mark_session_no_shows", apply_args)
    assert not done["isError"] and done["structuredContent"]["noShows"]["marked"] == 0


def test_mcp_provider_dry_run_makes_no_call_and_resolve(client, att_key):
    occ = _ended_occurrence("mcp2")
    reg = _reg(occ, "r@example.com")
    _zoom_linked(occ)
    dry = run(client, att_key, "import_session_attendance", {"occurrenceId": str(occ.pk), "source": "provider"})
    assert not dry["isError"], dry
    preview = dry["structuredContent"]["preview"]
    assert preview["plans"][1]["url"].startswith(f"https://api.zoom.us/v2/past_webinars/{ZOOM_ID}/participants")
    assert "client-secret-xyz" not in text_of(dry) and not AttendanceImport.objects.exists()
    imp = pipeline.create_file_import(occ, pipeline.normalise_rows(_rows("who@example.com")))
    row = imp.rows.get()
    args = {"occurrenceId": str(occ.pk), "importId": str(imp.pk), "rowId": str(row.pk), "registrationId": str(reg.pk)}
    _d, done = confirm(client, att_key, "resolve_attendance_row", args)
    assert not done["isError"], done
    assert Registration.objects.get(pk=reg.pk).attendance == "attended"
    wrong = run(client, att_key, "resolve_attendance_row", {**args, "importId": str(occ.pk)})
    assert wrong["isError"]


# ---------------------------------------------------------------- studio


def _user(name, group=None, superuser=False):
    from django.contrib.auth.models import Group, User

    if superuser:
        return User.objects.create_superuser(name, f"{name}@example.com", f"pw-{name}-123")
    user = User.objects.create_user(name, password=f"pw-{name}-123")
    user.groups.add(Group.objects.get_or_create(name=group)[0])
    return user


def _studio_url(occ):
    return f"/studio/sessions/{occ.pk}/attendance"


def test_studio_roles(client):
    from webinars.roles import ANALYST, EDITOR

    occ = _ended_occurrence("st1")
    for user in (_user("ana", ANALYST), _user("eddie", EDITOR)):
        client.force_login(user)
        assert client.get(_studio_url(occ)).status_code == 403
        assert client.post(_studio_url(occ), {"op": "pull"}).status_code == 403
    client.logout()
    assert client.get(_studio_url(occ)).status_code in (302, 403)
    assert not AttendanceImport.objects.exists()


def test_studio_upload_review_apply_with_tokens(client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    occ = _ended_occurrence("st2")
    reg = _reg(occ, "studio@example.com")
    _reg(occ, "absent@example.com")
    client.force_login(_user("root", superuser=True))
    page = client.get(_studio_url(occ))
    assert page.status_code == 200 and "No imports yet" in page.content.decode()
    csv = b"Email,Name,Duration (minutes)\nstudio@example.com,Studio Person,30\nnew@example.com,New,5\n"
    resp = client.post(_studio_url(occ), {"op": "upload", "file": SimpleUploadedFile("report.csv", csv, content_type="text/csv")})
    assert resp.status_code == 302, resp.content
    imp = AttendanceImport.objects.get()
    assert imp.matched_count == 1 and imp.unmatched_count == 1
    html = client.get(resp["Location"]).content.decode()
    assert "studio@example.com" not in html and "new@example.com" not in html
    assert "s***@example.com" in html and "n***@example.com" in html and "Studio Person" in html
    assert "https://" not in html.replace("https://webinars.example.com", "")
    apply_token = re.search(r'name="op" value="apply">.*?name="state_token" value="([^"]+)"', html, re.S).group(1)
    forged = client.post(_studio_url(occ), {"op": "apply", "import": str(imp.pk), "state_token": "forged"})
    assert forged.status_code == 400 and Registration.objects.get(pk=reg.pk).attendance == "unknown"
    ok = client.post(_studio_url(occ), {"op": "apply", "import": str(imp.pk), "state_token": apply_token})
    assert ok.status_code == 302 and Registration.objects.get(pk=reg.pk).attendance == "attended"
    replay = client.post(_studio_url(occ), {"op": "apply", "import": str(imp.pk), "state_token": apply_token})
    assert replay.status_code == 400
    html = client.get(ok["Location"]).content.decode()
    row = imp.rows.get(match="unmatched")
    ignore_token = re.search(rf'name="op" value="ignore">.*?name="row" value="{row.pk}">.*?name="state_token" value="([^"]+)"',
                             html, re.S).group(1)
    assert client.post(_studio_url(occ), {"op": "ignore", "import": str(imp.pk), "row": str(row.pk),
                                          "state_token": ignore_token}).status_code == 302
    assert imp.rows.get(pk=row.pk).match == "ignored"
    html = client.get(f"{_studio_url(occ)}?import={imp.pk}").content.decode()
    ns_token = re.search(r'name="op" value="no_shows">.*?name="state_token" value="([^"]+)"', html, re.S).group(1)
    assert client.post(_studio_url(occ), {"op": "no_shows", "import": str(imp.pk), "state_token": ns_token}).status_code == 302
    imp.refresh_from_db()
    assert imp.no_show_count == 1
    too_big = SimpleUploadedFile("big.csv", b"email\n" + b"x" * pipeline.MAX_FILE_BYTES, content_type="text/csv")
    assert client.post(_studio_url(occ), {"op": "upload", "file": too_big}).status_code == 400


def test_studio_pull_button_and_csrf(client):
    from django.test import Client

    occ = _ended_occurrence("st3")
    client.force_login(_user("root", superuser=True))
    html = client.get(_studio_url(occ)).content.decode()
    assert "not linked" in html and 'value="pull"' not in html
    _zoom_linked(occ)
    html = client.get(_studio_url(occ)).content.decode()
    assert 'value="pull"' in html and "client-secret-xyz" not in html and "credentials redacted" in html
    assert f"past_webinars/{ZOOM_ID}/participants" in html and "Bearer" not in html
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(_user("root2", superuser=True))
    assert csrf_client.post(_studio_url(occ), {"op": "pull"}).status_code == 403
    assert not AttendanceImport.objects.exists()
    hosting = client.get(f"/studio/sessions/{occ.pk}/hosting").content.decode()
    assert _studio_url(occ) in hosting


# ---------------------------------------------------------------- retention


def test_purge_blanks_rows_on_erasure_and_after_retention(db):
    occ = _ended_occurrence("pg1")
    reg = _reg(occ, "erase.me@example.com")
    imp = pipeline.create_file_import(occ, pipeline.normalise_rows([
        {"email": "erase.me@example.com", "name": "Erase Me", "external_id": "ext-1"},
        {"email": "keep@example.com", "name": "Keep"}]))
    call_command("purge_personal_data", person=str(reg.person_id))
    erased = imp.rows.get(registration=reg)
    assert erased.email_key == "" and erased.email_masked == "" and erased.display_name == "" and erased.external_id == ""
    assert erased.match == "matched"
    kept = imp.rows.get(match="unmatched")
    assert kept.email_key == "keep@example.com" and kept.display_name == "Keep"
    old = timezone.now() - timedelta(days=40)
    AttendanceImportRow.objects.filter(pk=kept.pk).update(created_at=old)
    call_command("purge_personal_data")
    kept.refresh_from_db()
    assert kept.email_key == "" and kept.email_masked == "" and kept.display_name == ""
    blob = json.dumps(list(AuditLog.objects.values_list("metadata", flat=True)))
    assert "erase.me" not in blob and "keep@" not in blob
