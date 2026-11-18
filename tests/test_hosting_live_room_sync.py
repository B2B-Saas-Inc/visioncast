"""A self-hosted live room's status, mirrored onto its session, and its recordings pulled in
(webinars/hosting/room_status.py, docs/hosting.md). The live service is a fake transport here."""
import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from tests.test_hosting_livekit import HOST, Service, _account, _linked
from tests.test_waiting_room import room_occurrence
from webinars.hosting import base, get_adapter, room_status, service
from webinars.models import AuditLog, HostedSession, Occurrence, OutboxMessage

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def allowlisted(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = [HOST]


@pytest.fixture(autouse=True)
def fresh_deleted_poll():
    room_status._deleted_next.clear()


class Live(Service):
    """The fake service, plus the room's phase, Reopen and publishing recordings."""

    def __init__(self):
        super().__init__()
        self.phase = "not_live"
        self.local = []
        self.published = {}
        self.deleted = {}  # room id -> deletedAt (ISO)

    def __call__(self, method, url, body, headers, *, allowed_hosts):
        path = url.split(HOST, 1)[1] if HOST in url else ""
        parts = path.split("/")
        if self.fail is None and method == "GET" and path.endswith("/deleted-rooms"):
            self.calls.append({"method": method, "url": url})
            return 200, json.dumps({"rooms": [{"id": k, "deletedAt": v} for k, v in self.deleted.items()]}).encode(), None, False
        if self.fail is None and method == "GET" and len(parts) == 4 and parts[2] == "rooms" and parts[3] in self.deleted:
            self.calls.append({"method": method, "url": url})
            return 410, json.dumps({"error": "deleted", "deletedAt": self.deleted[parts[3]]}).encode(), None, False
        if self.fail is None and method == "GET" and len(parts) == 4 and parts[2] == "rooms":
            self.calls.append({"method": method, "url": url})
            return 200, json.dumps({"id": parts[3], "slug": "s", "phase": self.phase, "liveSince": None}).encode(), None, False
        if self.fail is None and path.endswith("/reopen"):
            self.calls.append({"method": method, "url": url})
            if self.phase == "live":
                return 409, b'{"error":"on_air"}', None, False
            self.phase = "not_live"
            return 200, b'{"phase":"not_live"}', None, False
        if self.fail is None and (path.endswith("/publish") or path.endswith("/unpublish")):
            self.calls.append({"method": method, "url": url})
            egress = parts[-2]
            if path.endswith("/unpublish"):
                self.published.pop(egress, None)
                return 200, b"{}", None, False
            if not any(r.get("egressId") == egress and r.get("status") == "complete" for r in self.recordings):
                return 404, b'{"error":"not_found"}', None, False
            self.published[egress] = f"https://{HOST}/watch/{egress.lower()}tok"
            return 200, json.dumps({"watchUrl": self.published[egress], "videoUrl": self.published[egress] + "/video.mp4"}).encode(), None, False
        if self.fail is None and path.endswith("/recordings"):
            self.calls.append({"method": method, "url": url})
            rows = [{**r, "watchUrl": self.published.get(r.get("egressId"))} for r in self.recordings]
            return 200, json.dumps({"recordings": rows, "local": self.local}).encode(), None, False
        return super().__call__(method, url, body, headers, allowed_hosts=allowed_hosts)


def _session(starts_in=timedelta(hours=-1)):
    occ = room_occurrence(starts_in=starts_in, join_url="")
    session, _ = _linked(occ)
    return occ, session


# ---- status sync rules


def test_ended_completes_a_session_whose_start_has_passed_and_reopen_schedules_it_again():
    occ, session = _session()
    live = Live()
    live.phase = "live"
    assert room_status.sync(occ, transport=live) == {"phase": "live", "status": "scheduled", "changed": ""}
    assert room_status.label(HostedSession.objects.get(pk=session.pk)) == "Live now"
    live.phase = "ended"
    assert room_status.sync(occ, transport=live)["changed"] == "completed"
    occ.refresh_from_db()
    assert occ.status == Occurrence.Status.COMPLETED
    assert room_status.label(HostedSession.objects.get(pk=session.pk)) == "Ended in the live room"
    # Repeating it changes nothing.
    assert room_status.sync(occ, transport=live)["changed"] == ""
    done = room_status.reopen(occ, transport=live)
    assert done == {"phase": "not_live", "status": "scheduled", "changed": "scheduled"}
    occ.refresh_from_db()
    assert occ.status == Occurrence.Status.SCHEDULED
    assert AuditLog.objects.filter(action="hosting.room_reopened").count() == 1
    assert AuditLog.objects.filter(action="hosting.room_status_synced").count() >= 3


def test_a_room_ended_before_the_start_time_is_shown_but_closes_nothing():
    occ, session = _session(starts_in=timedelta(hours=5))
    live = Live()
    live.phase = "ended"
    assert room_status.sync(occ, transport=live)["changed"] == ""
    occ.refresh_from_db()
    assert occ.status == Occurrence.Status.SCHEDULED
    assert HostedSession.objects.get(pk=session.pk).room_phase == "ended"


def test_cancelled_and_completed_by_hand_are_never_touched():
    occ, session = _session()
    Occurrence.objects.filter(pk=occ.pk).update(status=Occurrence.Status.COMPLETED)
    live = Live()
    live.phase = "not_live"
    room_status.apply(session.pk, occ.pk, "not_live")
    occ.refresh_from_db()
    assert occ.status == Occurrence.Status.COMPLETED, "the sync didn't complete it, so it doesn't undo it"
    other, other_session = _session()
    Occurrence.objects.filter(pk=other.pk).update(status=Occurrence.Status.CANCELLED)
    room_status.apply(other_session.pk, other.pk, "ended")
    other.refresh_from_db()
    assert other.status == Occurrence.Status.CANCELLED


def test_reopen_refuses_while_on_the_air():
    from django.core.exceptions import ValidationError

    occ, _ = _session()
    live = Live()
    live.phase = "live"
    with pytest.raises(ValidationError) as err:
        room_status.reopen(occ, transport=live)
    assert "on the air" in str(err.value)


def test_sweep_polls_due_sessions_backs_off_a_dead_server_and_keeps_going(monkeypatch):
    dead, dead_session = _session()
    good, good_session = _session()
    far, far_session = _session(starts_in=timedelta(days=5))
    live = Live()
    live.phase = "ended"

    def transport(method, url, body, headers, *, allowed_hosts):
        if f"vc-{dead.pk}" in url:
            raise TimeoutError()
        return live(method, url, body, headers, allowed_hosts=allowed_hosts)

    assert room_status.sweep(transport=transport) == 1
    good.refresh_from_db()
    assert good.status == Occurrence.Status.COMPLETED
    dead_session.refresh_from_db()
    assert dead_session.room_phase_attempts == 1 and dead_session.room_phase_next_at > timezone.now() + timedelta(seconds=90)
    assert HostedSession.objects.get(pk=far_session.pk).room_phase_next_at is None, "too far ahead to ask about"
    # Asked again only when due.
    calls = len(live.calls)
    assert room_status.sweep(transport=transport) == 0
    assert len(live.calls) == calls


def test_the_worker_runs_the_status_sweep(monkeypatch):
    from webinars.management.commands import run_worker

    ran = []
    monkeypatch.setattr(room_status, "sweep", lambda: ran.append(1) or 0)
    run_worker._side_sweeps()
    assert ran == [1]


def test_adapter_rejects_an_unknown_phase():
    occ, session = _session()
    live = Live()
    live.phase = "melting"
    outcome = get_adapter("livekit").fetch_room({"service_host": HOST}, {"service_secret": "s" * 40}, session.external_id, transport=live)
    assert outcome.kind == "failed"


# ---- recordings


REC = {"egressId": "EG_abc", "status": "complete", "durationSeconds": 120, "sizeBytes": 9, "filename": "recordings/x/a.mp4", "startedAt": 1,
       "downloadUrl": f"https://{HOST}/files/sig.a", "publicUrl": None,
       "captions": [{"speaker": "Everyone", "kind": "srt", "filename": "a.srt", "url": f"https://{HOST}/files/sig.srt"}]}


def test_recordings_carry_signed_links_on_the_service_host_only():
    occ, _ = _session()
    live = Live()
    live.recordings = [REC, {**REC, "egressId": "EG_bad/../x", "downloadUrl": "https://evil.example.com/a.mp4"},
                       {**REC, "egressId": "EG_js", "downloadUrl": "javascript:alert(1)"}]
    live.local = [{"id": "l1", "name": "Maya", "sizeBytes": 5, "startedAt": 2, "url": f"https://{HOST}/files/sig.l1"},
                  {"id": "l2", "name": "Gus", "url": "https://evil.example.com/l2"}, "nonsense"]
    files = service.fetch_recording_files(occ, transport=live)
    assert [r["downloadUrl"] for r in files["recordings"]] == [f"https://{HOST}/files/sig.a", "", ""]
    assert [r["egressId"] for r in files["recordings"]] == ["EG_abc", "", "EG_js"]
    assert files["recordings"][0]["captions"][0]["url"] == f"https://{HOST}/files/sig.srt"
    assert files["local"] == [{"name": "Maya", "sizeBytes": 5, "startedAt": 2, "url": f"https://{HOST}/files/sig.l1"}]
    assert service.fetch_recordings(occ, transport=live)[0]["egressId"] == "EG_abc", "the old call still answers a list"


@pytest.fixture
def admin_client(client, django_user_model):
    user = django_user_model.objects.create_superuser("root", "root@example.com", "x" * 20)
    client.force_login(user)
    return client


def _url(occ):
    return reverse("studio:session-hosting", kwargs={"pk": occ.pk})


def test_publish_makes_it_the_sessions_recording_the_usual_way_and_unpublish_takes_it_down(admin_client, monkeypatch):
    from tests.conftest import make_destination, make_person, make_registration

    make_destination(event_types=("recording.published",))
    occ, _ = _session()
    make_registration(occ, make_person(email="ada@example.com"))
    live = Live()
    live.recordings = [REC]
    monkeypatch.setattr(base, "default_transport", live)
    page = admin_client.post(_url(occ), {"op": "recordings"})
    assert b"Publish as this session&#x27;s recording" in page.content or b"Publish as this session's recording" in page.content
    assert f"https://{HOST}/files/sig.a?download=1".encode() in page.content
    page = admin_client.post(_url(occ), {"op": "recording_publish", "recording": "EG_abc"})
    assert page.status_code == 200, page.content
    occ.refresh_from_db()
    assert occ.recording_url == f"https://{HOST}/watch/eg_abctok" and occ.recording_published_at is not None
    # The same path as pasting a link: registrants are told, and destinations get recording.published.
    assert OutboxMessage.objects.filter(event_type="recording.published").count() == 1
    assert AuditLog.objects.filter(action="recording.published").exists()
    assert b"Unpublish" in page.content
    page = admin_client.post(_url(occ), {"op": "recording_unpublish", "recording": "EG_abc"})
    assert page.status_code == 200, page.content
    occ.refresh_from_db()
    assert occ.recording_url == "" and occ.recording_published_at is None
    assert AuditLog.objects.filter(action="recording.unpublished").exists()
    assert "EG_abc" not in live.published


def test_only_administrators_publish_or_reopen(client, django_user_model, monkeypatch):
    from django.contrib.auth.models import Permission

    occ, _ = _session()
    live = Live()
    live.recordings = [REC]
    monkeypatch.setattr(base, "default_transport", live)
    editor = django_user_model.objects.create_user("ed", "ed@example.com", "x" * 20, is_staff=True)
    editor.user_permissions.add(*Permission.objects.filter(codename__in=["view_occurrence", "change_occurrence"]))
    client.force_login(editor)
    for op in ("recording_publish", "recording_unpublish", "room_reopen"):
        assert client.post(_url(occ), {"op": op, "recording": "EG_abc"}).status_code == 403, op
    occ.refresh_from_db()
    assert occ.recording_url == ""


def test_hosting_page_syncs_shows_the_phase_and_offers_reopen(admin_client, monkeypatch):
    occ, _ = _session()
    live = Live()
    live.phase = "ended"
    monkeypatch.setattr(base, "default_transport", live)
    page = admin_client.get(_url(occ))
    assert b"Ended in the live room" in page.content and b"Reopen the live room" in page.content
    occ.refresh_from_db()
    assert occ.status == Occurrence.Status.COMPLETED
    page = admin_client.post(_url(occ), {"op": "room_reopen"})
    assert b"Reopened" in page.content
    occ.refresh_from_db()
    assert occ.status == Occurrence.Status.SCHEDULED


def test_a_dead_live_server_doesnt_break_the_hosting_page(admin_client, monkeypatch):
    occ, _ = _session()
    live = Live()
    monkeypatch.setattr(base, "default_transport", live)
    live.fail = TimeoutError()
    assert admin_client.get(_url(occ)).status_code == 200


# ---- the join URL isn't asked for when the live room hands out links


def test_session_and_event_pages_dont_ask_a_live_room_for_a_join_url(admin_client, monkeypatch):
    occ, _ = _session(starts_in=timedelta(hours=3))
    monkeypatch.setattr(base, "default_transport", Live())
    page = admin_client.get(reverse("studio:session", kwargs={"pk": occ.pk})).content.decode()
    assert "editable until the last minute" not in page
    assert "Each person gets their own link to the live room" in page
    assert "add the join URL" not in page
    page = admin_client.get(reverse("studio:event-edit", kwargs={"pk": occ.event_id})).content.decode()
    assert "needs a join URL" not in page and "on, live room" in page


def test_a_session_without_a_live_room_still_asks_for_its_join_url(admin_client):
    occ = room_occurrence(starts_in=timedelta(hours=3), join_url="")
    page = admin_client.get(reverse("studio:session", kwargs={"pk": occ.pk})).content.decode()
    assert "editable until the last minute" in page
    page = admin_client.get(reverse("studio:event-edit", kwargs={"pk": occ.event_id})).content.decode()
    assert "needs a join URL" in page


def test_a_live_room_whose_account_is_off_is_not_ready():
    # Each person's link comes from the account: turned off, the doors stay shut.
    occ = room_occurrence(starts_in=timedelta(hours=3), join_url="")
    account = _account()
    _linked(occ, account)
    account.enabled = False
    account.save()
    assert service.uses_personal_links(occ) and not service.has_join_destination(occ)


# ---- a room deleted on the live server unlinks its session


def _iso(at):
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_room_deleted_on_the_live_server_unlinks_its_session_and_a_new_room_can_be_made():
    occ, session = _session()
    live = Live()
    live.deleted[session.external_id] = _iso(timezone.now())
    assert room_status.sync(occ, transport=live) == {"phase": "", "status": "scheduled", "changed": "unlinked"}
    session.refresh_from_db()
    assert session.state == HostedSession.State.DETACHED and session.room_phase == ""
    assert "deleted on the live server" in session.last_error
    occ.refresh_from_db()
    assert occ.status == Occurrence.Status.SCHEDULED, "the event's session stays as it was"
    assert AuditLog.objects.filter(action="hosting.room_deleted").count() == 1
    assert not any(c["method"] not in ("GET",) for c in live.calls), "nothing on the live server is changed"
    # The Hosting page offers a new room, and creating it links the session again.
    service.execute(occ, session.account, "create", transport=live)
    session.refresh_from_db()
    assert session.state == HostedSession.State.LINKED


def test_the_worker_unlinks_deleted_rooms_far_ahead_too_but_not_a_room_linked_again_since():
    near, near_session = _session()
    far, far_session = _session(starts_in=timedelta(days=30))
    again, again_session = _session(starts_in=timedelta(days=30))
    kept, kept_session = _session(starts_in=timedelta(days=30))
    live = Live()
    now = timezone.now()
    live.deleted = {
        # Deleted on the live server after they were linked here.
        near_session.external_id: _iso(now + timedelta(seconds=2)),
        far_session.external_id: _iso(now + timedelta(seconds=2)),
        # Deleted, then linked again from here: a new room with the same address.
        again_session.external_id: _iso(now - timedelta(days=1)),
        "vc-not-ours": _iso(now),
    }
    assert room_status.sweep(transport=live) == 2
    states = {s.pk: s.state for s in HostedSession.objects.all()}
    assert states[near_session.pk] == states[far_session.pk] == HostedSession.State.DETACHED
    assert states[again_session.pk] == states[kept_session.pk] == HostedSession.State.LINKED
    # Asked again only every couple of minutes.
    asked = sum(1 for c in live.calls if c["url"].endswith("/deleted-rooms"))
    room_status.sweep(transport=live)
    assert sum(1 for c in live.calls if c["url"].endswith("/deleted-rooms")) == asked


def test_a_live_server_that_cant_list_deleted_rooms_changes_nothing():
    _occ, session = _session(starts_in=timedelta(days=30))
    live = Live()
    live.fail = TimeoutError()
    assert room_status.sweep(transport=live) == 0
    assert HostedSession.objects.get(pk=session.pk).state == HostedSession.State.LINKED


def test_the_hosting_page_says_the_room_was_deleted_and_offers_a_new_one(admin_client, monkeypatch):
    occ, session = _session()
    live = Live()
    live.deleted[session.external_id] = _iso(timezone.now())
    monkeypatch.setattr(base, "default_transport", live)
    page = admin_client.get(_url(occ))
    assert page.status_code == 200
    assert b"The room was deleted on the live server" in page.content
    assert b"Reopen the live room" not in page.content
    assert HostedSession.objects.get(pk=session.pk).state == HostedSession.State.DETACHED
