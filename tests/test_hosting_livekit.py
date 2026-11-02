"""LiveKit hosting adapter (docs/hosting.md, live/README.md).

The service is replaced by a fake transport here. ``live/tests/e2e/`` covers the service
against a real LiveKit server (docs/validation.md).
"""
import json
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.conftest import make_person, make_registration
from tests.test_waiting_room import registered_viewer, room_occurrence
from webinars import attendance_import, crypto, waiting_room
from webinars.hosting import base, get_adapter, service
from webinars.hosting.livekit import room_id
from webinars.models import HostedSession, HostingAccount, ProviderRegistrant

pytestmark = pytest.mark.django_db

HOST = "live.example.com"
SECRET = "s" * 40


@pytest.fixture(autouse=True)
def allowlisted(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = [HOST]


class Service:
    """Answers like visioncast-live and records what it was asked."""

    def __init__(self):
        self.calls = []
        self.fail = None
        self.attendance_pages = [{"rows": [], "complete": True, "next": None}]
        self.recordings = []
        self.crew = []
        self.engagement = {}
        self.phase = "ended"
        self.settings = {"settings": {"chatEnabled": True}}
        self.destinations = []
        self.brand = {"accent": ""}

    def __call__(self, method, url, body, headers, *, allowed_hosts):
        self.calls.append({"method": method, "url": url, "body": json.loads(body) if body else None,
                           "headers": headers, "allowed": allowed_hosts})
        if self.fail is not None:
            if isinstance(self.fail, Exception):
                raise self.fail
            return self.fail, b"", None, False
        path = url.split(HOST, 1)[1]
        if method == "PUT" and path.count("/") == 3:
            room = path.rsplit("/", 1)[1]
            return 200, json.dumps({"id": room, "slug": room}).encode(), None, False
        if "/crew" in path:
            room = path.split("/")[3]
            if method == "GET":
                return 200, json.dumps({"crew": self.crew}).encode(), None, False
            assert method in ("GET", "POST"), "outbound calls don't allow anything else here"
            if path.endswith("/remove"):
                self.crew = [m for m in self.crew if m["id"] != path.split("/")[-2]]
                return 200, b'{"ok":true}', None, False
            if path.endswith("/new-link"):
                member = path.split("/")[-2]
                return 200, json.dumps({"id": member, "url": f"https://{HOST}/join/x.{room}.{member}_new.sig"}).encode(), None, False
            sent = json.loads(body)
            member = {"id": sent["key"].replace("-", "") or f"m{len(self.crew)}", "role": sent["role"], "name": sent["name"], "title": sent["title"],
                      "url": f"https://{HOST}/join/{sent['role'][0]}.{room}.m{len(self.crew)}_n.sig"}
            self.crew.append(member)
            return 200, json.dumps(member).encode(), None, False
        if "/engagement" in path:
            return 200, json.dumps({"phase": self.phase, "people": self.engagement, "next": None}).encode(), None, False
        if path.endswith("/forget"):
            return 200, b'{"ok":true}', None, False
        if path.endswith("/settings"):
            if method == "PATCH":
                self.settings.update(json.loads(body))
            return 200, json.dumps(self.settings).encode(), None, False
        if path.endswith("/destinations"):
            if method == "PUT":
                self.destinations = [{**d, "key": "", "masked": "****"} for d in json.loads(body)["destinations"]]
            return 200, json.dumps({"destinations": self.destinations}).encode(), None, False
        if path.endswith("/brand"):
            if method == "PUT":
                self.brand = json.loads(body)
            return 200, json.dumps(self.brand).encode(), None, False
        if path.endswith("/tickets"):
            identity = json.loads(body)["identity"]
            return 200, json.dumps({"id": identity, "joinUrl": f"https://{HOST}/join/a.room.{identity}.sig"}).encode(), None, False
        if "/attendance" in path:
            page = self.attendance_pages[sum(1 for c in self.calls if "/attendance" in c["url"]) - 1]
            return 200, json.dumps(page).encode(), None, False
        if path.endswith("/recordings"):
            return 200, json.dumps({"recordings": self.recordings}).encode(), None, False
        return 404, b"", None, False


def _account(enabled=True, config=None):
    account = HostingAccount.objects.create(provider="livekit", name=f"lk-{uuid.uuid4().hex[:6]}", enabled=enabled,
                                            config=config if config is not None else {"service_host": HOST})
    account.credentials_encrypted = crypto.encrypt_secret(json.dumps({"service_secret": SECRET}))
    account.save()
    return account


def _linked(occ, account=None):
    fake = Service()
    service.execute(occ, account or _account(), "create", transport=fake)
    session = HostedSession.objects.get(occurrence=occ)
    assert session.state == HostedSession.State.LINKED, session.last_error
    return session, fake


def test_described_as_self_hosted_with_room_and_recordings():
    described = get_adapter("livekit").describe()
    assert described["contract"] == "self-hosted"
    assert described["capabilities"]["hostRoom"] and described["capabilities"]["recordings"]
    assert described["capabilities"]["registerAttendees"] and not described["capabilities"]["attach"]


def test_a_typed_host_must_be_on_the_allowlist(settings):
    adapter = get_adapter("livekit")
    assert adapter.validate_config({"service_host": HOST}) == {}
    settings.WEBHOOK_HOST_ALLOWLIST = []
    errors = adapter.validate_config({"service_host": HOST})
    assert HOST in errors["service_host"][0]
    assert "service_host" in adapter.validate_config({"service_host": "http://evil.example/"})
    assert "service_host" in adapter.validate_config({})


def test_a_host_removed_from_the_allowlist_sends_nothing(settings):
    occ = room_occurrence(join_url="")
    account = _account()
    settings.WEBHOOK_HOST_ALLOWLIST = []
    fake = Service()
    outcome = get_adapter("livekit").run("create", occ, account.config, {"service_secret": SECRET}, transport=fake)
    assert outcome.kind == "failed" and not fake.calls
    assert service.preview(occ, account, "create")["problems"]


def test_create_puts_the_room_and_keeps_the_host_link_private():
    occ = room_occurrence(join_url="")
    session, fake = _linked(occ)
    call = fake.calls[0]
    assert call["method"] == "PUT" and call["url"] == f"https://{HOST}/v1/rooms/{room_id(occ)}"
    assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert call["allowed"] == frozenset({HOST})
    assert call["body"]["title"] == occ.event.public_title
    assert call["body"]["autoRecord"] is False
    assert session.state == HostedSession.State.LINKED and session.external_id == room_id(occ)
    assert not session.host_url_encrypted, "there's no shared host link to store"
    occ.refresh_from_db()
    assert occ.join_url == ""
    assert "/join/" not in json.dumps(service.serialize_session(session))


def test_preview_never_contains_the_secret():
    occ = room_occurrence(join_url="")
    assert SECRET not in json.dumps(service.preview(occ, _account(), "create"), default=str)


def test_a_timeout_on_create_is_a_plain_failure_to_retry():
    occ = room_occurrence(join_url="")
    fake = Service()
    fake.fail = TimeoutError()
    account = _account()
    service.execute(occ, account, "create", transport=fake)
    assert HostedSession.objects.get(occurrence=occ).state == HostedSession.State.FAILED
    service.execute(occ, account, "create", transport=Service())
    assert HostedSession.objects.get(occurrence=occ).state == HostedSession.State.LINKED


@pytest.mark.parametrize("answer", [
    {"id": "other-room", "hostUrl": f"https://{HOST}/join/x"},
    {"id": None, "hostUrl": f"https://{HOST}/join/x"},
])
def test_create_refuses_an_unexpected_room(answer):
    occ = room_occurrence(join_url="")
    outcome = get_adapter("livekit").run(
        "create", occ, {"service_host": HOST}, {"service_secret": SECRET},
        transport=lambda *a, **k: (200, json.dumps(answer).encode(), None, False))
    assert outcome.kind == "failed"


def test_a_crew_link_on_another_host_is_refused():
    answer = {"id": "m1", "url": "https://elsewhere.example/join/x"}
    outcome = get_adapter("livekit").crew_add(
        {"service_host": HOST}, {"service_secret": SECRET}, "room", role="guest", name="Gus",
        transport=lambda *a, **k: (200, json.dumps(answer).encode(), None, False))
    assert outcome.kind == "failed"


# ---- personal links


def _open_room():
    return room_occurrence(starts_in=timedelta(minutes=5), join_url="")


def _viewer(occ):
    reg, _token, cap = registered_viewer(occ)
    return reg, cap


def test_sweep_asks_for_a_personal_link_and_sends_only_the_registration_id():
    occ = room_occurrence(join_url="")
    _linked(occ)
    reg = make_registration(occ, make_person("ada@example.com", first_name="Ada", last_name="Lovelace"))
    fake = Service()
    assert service.sweep_registrants(transport=fake) == 1
    assert fake.calls[0]["body"] == {"identity": str(reg.pk)}
    assert "lovelace" not in json.dumps(fake.calls[0]["body"]).lower()
    pr = ProviderRegistrant.objects.get(registration=reg)
    assert pr.state == ProviderRegistrant.State.LINKED and pr.external_id == str(reg.pk)
    assert service.attendee_join_url(occ, reg).startswith(f"https://{HOST}/join/a.")


def test_doors_open_without_a_shared_link_and_the_door_asks_for_a_missing_link(monkeypatch):
    occ = _open_room()
    session, _ = _linked(occ)
    assert service.has_join_destination(occ)
    reg, cap = _viewer(occ)
    fake = Service()
    monkeypatch.setattr(base, "default_transport", fake)
    admission = waiting_room.admit(occ, cap)
    assert admission.admitted and admission.join_url.startswith(f"https://{HOST}/join/a.")
    assert [c["body"] for c in fake.calls] == [{"identity": str(reg.pk)}]
    # Next time the stored link is used.
    waiting_room.admit(occ, cap)
    assert len(fake.calls) == 1


def test_the_request_is_made_before_the_admission_locks(monkeypatch):
    from django.db import connection

    occ = _open_room()
    _linked(occ)
    _, cap = _viewer(occ)
    depth_at_call = []
    fake = Service()

    def watched(*a, **k):
        depth_at_call.append(len(connection.savepoint_ids))
        return fake(*a, **k)

    monkeypatch.setattr(base, "default_transport", watched)
    before = len(connection.savepoint_ids)
    assert waiting_room.admit(occ, cap).admitted
    assert depth_at_call == [before]


def test_an_outage_at_the_door_denies_then_spaced_retries_recover(monkeypatch):
    occ = _open_room()
    _linked(occ)
    reg, cap = _viewer(occ)
    down = Service()
    down.fail = 503
    monkeypatch.setattr(base, "default_transport", down)
    assert waiting_room.admit(occ, cap).reason == "no_link"
    assert len(down.calls) == 1
    pr = ProviderRegistrant.objects.get(registration=reg)
    assert pr.state == ProviderRegistrant.State.FAILED
    # A refresh right away starts no second request.
    assert waiting_room.admit(occ, cap).reason == "no_link"
    assert len(down.calls) == 1
    # Later, even past the worker's attempt limit, the door asks again.
    ProviderRegistrant.objects.filter(pk=pr.pk).update(
        attempts=service.MAX_REGISTRANT_ATTEMPTS, updated_at=timezone.now() - timedelta(minutes=5))
    up = Service()
    monkeypatch.setattr(base, "default_transport", up)
    assert waiting_room.admit(occ, cap).admitted
    assert len(up.calls) == 1


def test_the_sweep_stops_at_the_attempt_limit():
    occ = room_occurrence(join_url="")
    _linked(occ)
    reg = make_registration(occ, make_person("ada@example.com"))
    down = Service()
    down.fail = 503
    for _ in range(service.MAX_REGISTRANT_ATTEMPTS + 2):
        service.sweep_registrants(transport=down)
    assert len(down.calls) == service.MAX_REGISTRANT_ATTEMPTS
    assert ProviderRegistrant.objects.get(registration=reg).state == ProviderRegistrant.State.FAILED


def test_a_lost_attempt_retries_without_a_person():
    occ = room_occurrence(join_url="")
    session, _ = _linked(occ)
    reg = make_registration(occ, make_person("ada@example.com"))
    pr = ProviderRegistrant.objects.create(registration=reg, session=session, state=ProviderRegistrant.State.PENDING,
                                           attempts=1, event_account_id=session.account_id,
                                           event_external_id=session.external_id)
    ProviderRegistrant.objects.filter(pk=pr.pk).update(updated_at=timezone.now() - timedelta(hours=2))
    service.recover_stale_operations()
    pr.refresh_from_db()
    assert pr.state == ProviderRegistrant.State.FAILED
    assert service.sweep_registrants(transport=Service()) == 1
    pr.refresh_from_db()
    assert pr.state == ProviderRegistrant.State.LINKED


def test_a_link_from_another_room_is_refused_and_asked_for_again():
    occ = room_occurrence(join_url="")
    session, _ = _linked(occ)
    reg = make_registration(occ, make_person("ada@example.com"))
    pr = ProviderRegistrant.objects.create(
        registration=reg, session=session, state=ProviderRegistrant.State.LINKED, attempts=2,
        event_account_id=session.account_id, event_external_id="vc-another-room",
        join_url_encrypted=crypto.encrypt_secret(f"https://{HOST}/join/a.old.x.sig"))
    assert service.attendee_join_url(occ, reg) is None
    service.flag_unbound_registrants()
    pr.refresh_from_db()
    assert pr.state == ProviderRegistrant.State.FAILED and not pr.join_url_encrypted
    assert (pr.event_account_id, pr.event_external_id) == (session.account_id, session.external_id)
    service.sweep_registrants(transport=Service())
    assert service.attendee_join_url(occ, reg).startswith(f"https://{HOST}/join/a.")


def test_a_stored_link_on_another_host_is_refused():
    occ = room_occurrence(join_url="")
    session, _ = _linked(occ)
    reg = make_registration(occ, make_person("ada@example.com"))
    ProviderRegistrant.objects.create(
        registration=reg, session=session, state=ProviderRegistrant.State.LINKED,
        event_account_id=session.account_id, event_external_id=session.external_id,
        join_url_encrypted=crypto.encrypt_secret("https://elsewhere.example/join/a.x.y.z"))
    assert service.attendee_join_url(occ, reg) is None


# ---- attendance


def _row(reg_id, seconds=600):
    return {"externalId": str(reg_id), "joinedAt": "2026-10-31T10:00:00Z", "leftAt": "2026-10-31T10:10:00Z", "durationSeconds": seconds}


def test_attendance_matches_registrations_by_id_with_no_email_involved():
    occ = room_occurrence(join_url="")
    session, _ = _linked(occ)
    reg = make_registration(occ, make_person("ada@example.com"))
    service.sweep_registrants(transport=Service())
    fake = Service()
    fake.attendance_pages = [{"rows": [_row(reg.pk)], "complete": True, "next": None}]
    imp = attendance_import.pull_from_provider(occ, transport=fake)
    assert imp.state != "failed", imp.error
    row = imp.rows.get()
    assert row.match == "matched" and row.registration_id == reg.pk
    assert not row.email_key
    assert imp.source == "livekit"


def test_attendance_follows_pages_and_reports_a_partial_list():
    adapter = get_adapter("livekit")
    occ = room_occurrence(join_url="")
    fake = Service()
    fake.attendance_pages = [
        {"rows": [_row("a")], "complete": False, "next": "a"},
        {"rows": [_row("b")], "complete": False, "next": None},
    ]
    outcome = adapter.fetch_attendance(occ, {"service_host": HOST}, {"service_secret": SECRET}, "vc-room", transport=fake)
    assert [r["external_id"] for r in outcome.data["rows"]] == ["a", "b"]
    assert outcome.data["complete"] is False
    assert fake.calls[1]["url"].endswith("/attendance?after=a")


def test_attendance_is_marked_partial_at_the_page_limit():
    adapter = get_adapter("livekit")
    occ = room_occurrence(join_url="")
    answer = json.dumps({"rows": [_row("a")], "complete": True, "next": "a"}).encode()
    outcome = adapter.fetch_attendance(occ, {"service_host": HOST}, {"service_secret": SECRET}, "vc-room",
                                       transport=lambda *a, **k: (200, answer, None, False))
    assert outcome.data["complete"] is False


def test_bad_attendance_rows_are_dropped():
    adapter = get_adapter("livekit")
    occ = room_occurrence(join_url="")
    answer = {"rows": ["x", {"externalId": "../bad"}, {"externalId": "ok", "joinedAt": "<script>", "durationSeconds": True}],
              "complete": True}
    outcome = adapter.fetch_attendance(occ, {"service_host": HOST}, {"service_secret": SECRET}, "vc-room",
                                       transport=lambda *a, **k: (200, json.dumps(answer).encode(), None, False))
    assert outcome.data["rows"] == [{"external_id": "ok", "email": "", "name": "", "joined_at": "", "left_at": "", "duration_seconds": None}]


def test_recordings_only_link_to_https():
    occ = room_occurrence(join_url="")
    session, _ = _linked(occ)
    fake = Service()
    fake.recordings = [
        {"status": "complete", "durationSeconds": 120, "sizeBytes": 10, "filename": "a.mp4", "publicUrl": "https://cdn.example.com/a.mp4"},
        {"status": "complete", "durationSeconds": 60, "sizeBytes": 10, "filename": "b.mp4", "publicUrl": "javascript:alert(1)"},
    ]
    recordings = service.fetch_recordings(occ, transport=fake)
    assert [r["publicUrl"] for r in recordings] == ["https://cdn.example.com/a.mp4", ""]


def test_other_providers_have_no_rooms_or_recordings():
    from django.core.exceptions import ValidationError

    occ = room_occurrence(join_url="https://zoom.us/j/1")
    account = HostingAccount.objects.create(provider="zoom", name="z", enabled=True)
    HostedSession.objects.create(occurrence=occ, account=account, provider="zoom", mode="provisioned", state="linked",
                                 external_id="1", host_url_encrypted=crypto.encrypt_secret("https://zoom.us/s/1"))
    with pytest.raises(ValidationError):
        service.host_room_link(occ, user=None)
    with pytest.raises(ValidationError):
        service.fetch_recordings(occ)


# ---- studio


@pytest.fixture
def admin_client(client, django_user_model):
    user = django_user_model.objects.create_superuser("root", "root@example.com", "x" * 20)
    client.force_login(user)
    return client


def _url(occ):
    from django.urls import reverse

    return reverse("studio:session-hosting", kwargs={"pk": occ.pk})


def test_studio_gives_each_person_their_own_host_link_and_audits_it(admin_client, monkeypatch):
    from webinars.models import AuditLog

    occ = room_occurrence(join_url="")
    _linked(occ)
    fake = Service()
    monkeypatch.setattr(base, "default_transport", fake)
    page = admin_client.get(_url(occ))
    assert b"Open the studio as a host" in page.content
    assert b"/join/h." not in page.content
    response = admin_client.post(_url(occ), {"op": "host_room"})
    assert response.status_code == 302 and response["Location"].startswith(f"https://{HOST}/join/h.")
    sent = next(c["body"] for c in fake.calls if c["url"].endswith("/crew") and c["method"] == "POST")
    assert sent["role"] == "host" and sent["key"].startswith("studio-user-") and sent["name"] == "root"
    assert AuditLog.objects.filter(action="hosting.host_link_used").exists()


def test_studio_manages_the_crew_person_by_person(admin_client, monkeypatch):
    from webinars.models import AuditLog

    occ = room_occurrence(join_url="")
    _linked(occ)
    fake = Service()
    monkeypatch.setattr(base, "default_transport", fake)
    added = admin_client.post(_url(occ), {"op": "crew_add", "role": "guest", "name": "Gus Guest", "title": "VP"})
    assert added.status_code == 200 and f"https://{HOST}/join/g.".encode() in added.content and b"Gus Guest's own link" in added.content
    assert "no-store" in added["Cache-Control"]
    assert AuditLog.objects.filter(action="hosting.crew_added").exists()
    page = admin_client.get(_url(occ))
    assert b"Gus Guest" in page.content and b"/join/g." not in page.content, "links show only when asked for"
    member = fake.crew[0]["id"]
    shown = admin_client.post(_url(occ), {"op": "crew_show", "member": member})
    assert f"https://{HOST}/join/g.".encode() in shown.content
    replaced = admin_client.post(_url(occ), {"op": "crew_new_link", "member": member, "name": "Gus Guest"})
    assert b"_new.sig" in replaced.content
    removed = admin_client.post(_url(occ), {"op": "crew_remove", "member": member})
    assert removed.status_code == 200 and fake.crew == []
    assert admin_client.post(_url(occ), {"op": "crew_add", "role": "attendee", "name": "x"}).status_code == 400
    assert admin_client.post(_url(occ), {"op": "crew_add", "role": "guest", "name": " "}).status_code == 400
    assert admin_client.post(_url(occ), {"op": "crew_remove", "member": "../../etc"}).status_code == 400


def test_studio_refuses_a_crew_change_when_the_session_has_no_room(admin_client):
    occ = room_occurrence(join_url="")
    assert admin_client.post(_url(occ), {"op": "crew_add", "role": "guest", "name": "Gus"}).status_code == 400


def test_studio_editors_open_the_room_but_dont_get_crew_links(client, django_user_model, monkeypatch):
    from django.contrib.auth.models import Group

    from webinars.roles import EDITOR

    occ = room_occurrence(join_url="")
    _linked(occ)
    user = django_user_model.objects.create_user("ed", "ed@example.com", "x" * 20, is_staff=True)
    user.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(user)
    fake = Service()
    monkeypatch.setattr(base, "default_transport", fake)
    assert client.post(_url(occ), {"op": "host_room"}).status_code == 302
    assert client.post(_url(occ), {"op": "crew_add", "role": "guest", "name": "Gus"}).status_code == 403
    assert client.post(_url(occ), {"op": "crew_remove", "member": "m0"}).status_code == 403
    assert b"Add to the crew" not in client.get(_url(occ)).content


def test_studio_hides_panels_that_dont_apply(admin_client, monkeypatch):
    occ = room_occurrence(join_url="")
    _linked(occ)
    monkeypatch.setattr(base, "default_transport", Service())
    page = admin_client.get(_url(occ)).content
    assert b"Manual link (StreamYard)" not in page and b"Attach an existing" not in page
    assert b"Personal, one per registrant" in page


def test_studio_defaults_to_an_update_once_the_room_exists(admin_client, monkeypatch):
    occ = room_occurrence(join_url="")
    _linked(occ)
    monkeypatch.setattr(base, "default_transport", Service())
    page = admin_client.get(_url(occ)).content.decode()
    assert 'value="update" checked' in page and 'value="create" checked' not in page


def test_studio_account_form_offers_recording_as_a_choice(admin_client):
    from django.urls import reverse

    page = admin_client.get(reverse("studio:hosting-account-new") + "?provider=livekit").content.decode()
    assert '<select class="select" id="config-auto_record"' in page
    assert "Start when the session goes live" in page


# ---- the live server's address and settings


def test_the_room_gets_a_clean_address_from_the_event():
    occ = room_occurrence(join_url="")
    _, fake = _linked(occ)
    assert fake.calls[0]["body"]["slug"] == occ.event.slug.lower().replace("_", "-")
    assert fake.calls[0]["body"]["entryUrl"] == f"https://webinars.example.com/w/{occ.waiting_room_code}/"


def test_an_event_with_several_sessions_tells_them_apart_by_date():
    from tests.conftest import make_occurrence
    from webinars.hosting.livekit import room_slug

    occ = room_occurrence(join_url="")
    only = room_slug(occ)
    make_occurrence(occ.event, starts_at=occ.starts_at + timedelta(days=7))
    assert room_slug(occ) == f"{only}-{occ.starts_at:%Y-%m-%d-%H%M}"


def test_stream_keys_and_brand_are_not_kept_in_visioncast():
    fields = {f.name for f in (*get_adapter("livekit").credential_fields, *get_adapter("livekit").config_fields)}
    assert not {name for name in fields if "brand" in name or name.endswith(("_key", "_url"))}


def test_administrators_are_signed_in_to_the_live_settings_from_a_session(admin_client, monkeypatch):
    from webinars.models import AuditLog

    class Link(Service):
        def __call__(self, method, url, body, headers, *, allowed_hosts):
            if url.endswith("/v1/admin-link"):
                self.calls.append({"url": url, "body": json.loads(body)})
                return 200, json.dumps({"url": f"https://{HOST}/login/link/abc"}).encode(), None, False
            return super().__call__(method, url, body, headers, allowed_hosts=allowed_hosts)

    occ = room_occurrence(join_url="")
    session, _ = _linked(occ)
    fake = Link()
    monkeypatch.setattr(base, "default_transport", fake)
    page = admin_client.get(_url(occ)).content.decode()
    assert "Broadcast and brand settings" in page and "dest-youtube" not in page
    response = admin_client.post(_url(occ), {"op": "live_settings"})
    assert response.status_code == 302 and response["Location"] == f"https://{HOST}/login/link/abc"
    assert fake.calls[-1]["body"] == {"room": session.external_id, "email": "root@example.com", "name": "root"}
    assert AuditLog.objects.filter(action="hosting.live_settings_opened").exists()


def test_account_page_links_to_the_live_settings_only_for_this_provider(admin_client):
    from django.urls import reverse

    account = _account()
    page = admin_client.get(reverse("studio:hosting-account-edit", kwargs={"pk": account.pk})).content.decode()
    assert "Open live settings" in page and "Brand and stream keys" in page
    zoom = admin_client.get(reverse("studio:hosting-account-new") + "?provider=zoom").content.decode()
    assert "Open live settings" not in zoom


def test_editors_dont_get_the_live_settings(client, django_user_model):
    from django.contrib.auth.models import Group

    from webinars.roles import EDITOR

    occ = room_occurrence(join_url="")
    _linked(occ)
    user = django_user_model.objects.create_user("ed3", "ed3@example.com", "x" * 20, is_staff=True)
    user.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(user)
    assert client.post(_url(occ), {"op": "live_settings"}).status_code == 403


def test_a_settings_link_on_another_host_is_refused():
    outcome = get_adapter("livekit").fetch_settings_link({"service_host": HOST}, {"service_secret": SECRET}, "", email="a@example.com", transport=lambda *a, **k: (200, json.dumps({"url": "https://elsewhere.example/x"}).encode(), None, False))
    assert outcome.kind == "failed"


def test_recordings_list_their_caption_files_with_safe_links_only():
    occ = room_occurrence(join_url="")
    _linked(occ)
    fake = Service()
    fake.recordings = [{"status": "complete", "durationSeconds": 60, "sizeBytes": 1, "filename": "a.mp4", "publicUrl": "https://cdn.example.com/a.mp4",
                        "captions": [{"speaker": "Maya", "kind": "srt", "url": "https://cdn.example.com/a.maya.srt"}, {"speaker": "Gus", "kind": "x", "url": "javascript:alert(1)"}, "nonsense"]}]
    captions = service.fetch_recordings(occ, transport=fake)[0]["captions"]
    assert captions == [{"speaker": "Maya", "kind": "srt", "url": "https://cdn.example.com/a.maya.srt"}, {"speaker": "Gus", "kind": "txt", "url": ""}]



# ---- what people did comes back


def _registered(occ, email="ada@example.com"):
    from tests.conftest import make_person, make_registration

    return make_registration(occ, make_person(email=email))


ROW = {"chatMessages": 1, "chat": [{"at": 1, "text": "Hi"}], "questions": [], "polls": [{"question": "Q", "answer": "A"}], "survey": {"Rate": "5"}}


def test_engagement_comes_back_once_and_goes_only_to_destinations_that_ask(monkeypatch):
    from django.utils import timezone
    from tests.conftest import make_destination
    from webinars.hosting import engagement
    from webinars.models import OutboxMessage

    occ = room_occurrence(join_url="")
    _linked(occ)
    registration = _registered(occ)
    with_group = make_destination(event_types=("engagement.recorded",), allowed_groups=("engagement",))
    without = make_destination(event_types=("engagement.recorded",), allowed_groups=("registration",))
    fake = Service()
    fake.engagement = {str(registration.pk): ROW, "not-a-registration": ROW, "99999999-9999-4999-8999-999999999999": ROW}
    assert engagement.sync(occ, transport=fake) == {"people": 3, "changed": 1, "final": False}
    registration.refresh_from_db()
    assert registration.engagement["chat"][0]["text"] == "Hi"
    messages = {m.destination_id: m.payload for m in OutboxMessage.objects.filter(event_type="engagement.recorded")}
    assert messages[with_group.pk]["engagement"]["survey"] == {"Rate": "5"}
    assert "engagement" not in messages[without.pk], "personal, so only when the destination lists it"
    assert engagement.sync(occ, transport=fake)["changed"] == 0, "repeating sends nothing new"
    assert OutboxMessage.objects.filter(event_type="engagement.recorded").count() == 2
    assert engagement.summary(occ) == {"people": 1, "chatMessages": 1, "questions": 0, "pollVotes": 1, "surveyAnswers": 1}
    # Surveys come after the end, so it keeps asking for a day, then stops.
    session = HostedSession.objects.get(occurrence=occ)
    assert session.engagement_synced_at is None and session.engagement_ended_at <= timezone.now()
    HostedSession.objects.filter(pk=session.pk).update(engagement_ended_at=timezone.now() - engagement.SURVEY_WINDOW)
    assert engagement.sync(occ, transport=fake)["final"] is True
    assert HostedSession.objects.get(occurrence=occ).engagement_synced_at is not None


def test_engagement_from_another_session_is_not_taken():
    from webinars.hosting import engagement

    occ, other = room_occurrence(join_url=""), room_occurrence(join_url="")
    _linked(occ)
    stray = _registered(other)
    fake = Service()
    fake.engagement = {str(stray.pk): ROW}
    assert engagement.sync(occ, transport=fake)["changed"] == 0
    stray.refresh_from_db()
    assert stray.engagement == {}


def test_engagement_rows_are_cut_down_to_what_is_expected():
    fake = Service()
    fake.engagement = {"a": {"chatMessages": "lots", "chat": [{"text": "x" * 5000, "evil": {"nested": 1}}, "junk"], "extra": "dropped", "survey": "no"}}
    data = get_adapter("livekit").fetch_engagement({"service_host": HOST}, {"service_secret": SECRET}, "room", transport=fake).data
    assert data["people"]["a"] == {"chatMessages": 0, "chat": [{"text": "x" * 1000}], "questions": [], "polls": [], "survey": {}}


def test_the_worker_brings_back_sessions_that_are_over_and_only_those(monkeypatch):
    from django.utils import timezone
    from webinars.hosting import engagement

    over = room_occurrence(starts_in=timedelta(hours=-3), join_url="")
    still_on = room_occurrence(starts_in=timedelta(minutes=-5), join_url="")
    for occ in (over, still_on):
        _linked(occ)
    registration = _registered(over)
    fake = Service()
    fake.engagement = {str(registration.pk): ROW}
    assert over.ends_at < timezone.now() - engagement.SETTLE
    assert still_on.ends_at > timezone.now() - engagement.SETTLE
    assert engagement.sweep(transport=fake) == 0, "not final yet, so the worker can rest"
    registration.refresh_from_db()
    assert registration.engagement == ROW
    assert HostedSession.objects.get(occurrence=still_on).engagement_next_at is None
    calls = len(fake.calls)
    assert engagement.sweep(transport=fake) == 0
    assert len(fake.calls) == calls, "it isn't asked again straight away"


def test_a_dead_server_is_asked_less_often_and_never_holds_up_others(monkeypatch):
    from django.utils import timezone
    from webinars.hosting import engagement

    dead = []
    for _ in range(6):
        occ = room_occurrence(starts_in=timedelta(hours=-3), join_url="")
        _linked(occ)
        dead.append(occ)
    late = room_occurrence(starts_in=timedelta(hours=-2), join_url="")
    _linked(late)
    fake = Service()
    fake.fail = 500
    engagement.sweep(limit=5, transport=fake)
    assert sum(HostedSession.objects.filter(occurrence__in=dead).values_list("engagement_attempts", flat=True)) == 5
    fake.fail = None
    engagement.sweep(limit=5, transport=fake)
    assert HostedSession.objects.get(occurrence=late).engagement_next_at is not None, "the next session gets its turn"
    HostedSession.objects.update(engagement_attempts=engagement.MAX_ATTEMPTS - 1, engagement_next_at=timezone.now() - timedelta(minutes=1))
    fake.fail = 500
    HostedSession.objects.exclude(occurrence=dead[0]).update(engagement_synced_at=timezone.now())
    engagement.sweep(limit=1, transport=fake)
    assert HostedSession.objects.get(occurrence=dead[0]).engagement_synced_at is not None, "given up on in the end"


def test_a_cancelled_session_or_a_disabled_account_is_skipped():
    from webinars.hosting import engagement
    from webinars.models import Occurrence

    cancelled = room_occurrence(starts_in=timedelta(hours=-3), join_url="")
    _linked(cancelled)
    Occurrence.objects.filter(pk=cancelled.pk).update(status=Occurrence.Status.CANCELLED)
    fake = Service()
    engagement.sweep(transport=fake)
    assert not any("/engagement" in c["url"] for c in fake.calls)


def test_someone_erased_stays_erased_when_it_comes_back():
    from webinars.hosting import engagement
    from webinars.management.commands.purge_personal_data import erase_person

    occ = room_occurrence(join_url="")
    _, _unused = _linked(occ)
    registration = _registered(occ)
    erase_person(registration.person_id)
    fake = Service()
    fake.engagement = {str(registration.pk): ROW}
    assert engagement.sync(occ, transport=fake)["changed"] == 0
    registration.refresh_from_db()
    assert registration.engagement == {}


def test_erasing_also_goes_to_the_live_server(monkeypatch):
    from webinars.management.commands.purge_personal_data import erase_person

    occ = room_occurrence(join_url="")
    _linked(occ)
    registration = _registered(occ)
    fake = Service()
    monkeypatch.setattr(base, "default_transport", fake)
    erase_person(registration.person_id)
    assert any(c["url"].endswith(f"/people/{registration.pk}/forget") and c["method"] == "POST" for c in fake.calls)


def test_erasing_with_the_live_server_off_says_so_and_can_be_done_again(monkeypatch):
    from webinars.management.commands.purge_personal_data import erase_person

    occ = room_occurrence(join_url="")
    _linked(occ)
    registration = _registered(occ)

    def server_off(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(base, "default_transport", server_off)
    assert erase_person(registration.person_id)["live_servers_not_reached"] == 1
    fake = Service()
    monkeypatch.setattr(base, "default_transport", fake)
    assert erase_person(registration.person_id)["live_servers_not_reached"] == 0
    assert any(c["url"].endswith(f"/people/{registration.pk}/forget") for c in fake.calls)


def test_erasing_a_person_erases_what_they_did():
    from webinars.management.commands.purge_personal_data import erase_person

    occ = room_occurrence(join_url="")
    registration = _registered(occ)
    registration.engagement = ROW
    registration.save()
    erase_person(registration.person_id)
    registration.refresh_from_db()
    assert registration.engagement == {}


# ---- over the API


@pytest.fixture
def provision_key():
    from tests.conftest import make_api_key

    return make_api_key(("hosting_provision",), name="prov")


def _api(client, method, path, key, body=None):
    extra = {"HTTP_AUTHORIZATION": f"Bearer {key}"}
    return getattr(client, method)(f"/api/v1/manage{path}", data=json.dumps(body) if body is not None else None, content_type="application/json", **extra)


def test_crew_over_rest_gives_the_link_once_and_lists_without_it(client, provision_key, monkeypatch):
    occ = room_occurrence(join_url="")
    _linked(occ)
    fake = Service()
    monkeypatch.setattr(base, "default_transport", fake)
    added = _api(client, "post", f"/occurrences/{occ.pk}/live/crew", provision_key, {"role": "guest", "name": "Gus"})
    assert added.status_code == 201 and added.json()["url"].startswith(f"https://{HOST}/join/g.")
    listed = _api(client, "get", f"/occurrences/{occ.pk}/live/crew", provision_key)
    assert listed.status_code == 200 and "url" not in listed.json()["results"][0]
    assert b"/join/" not in listed.content
    member = listed.json()["results"][0]["id"]
    assert _api(client, "post", f"/occurrences/{occ.pk}/live/crew/{member}/new-link", provision_key, {}).status_code == 200
    from tests.conftest import make_api_key

    assert _api(client, "get", f"/occurrences/{occ.pk}/live/crew", make_api_key(("config_read",), name="ro")).status_code == 403, "names are personal"
    assert _api(client, "delete", f"/occurrences/{occ.pk}/live/crew/{member}", provision_key).status_code == 204
    assert fake.crew == []
    assert _api(client, "post", f"/occurrences/{occ.pk}/live/crew", provision_key, {"role": "boss", "name": "Gus"}).status_code == 400
    assert _api(client, "delete", f"/occurrences/{occ.pk}/live/crew/..%2Fx", provision_key).status_code in (400, 404)


def test_live_settings_destinations_and_engagement_over_rest(client, provision_key, monkeypatch):
    from webinars.models import AuditLog

    occ = room_occurrence(join_url="")
    _linked(occ)
    fake = Service()
    monkeypatch.setattr(base, "default_transport", fake)
    url = f"/occurrences/{occ.pk}/live"
    assert _api(client, "get", f"{url}/settings", provision_key).json()["settings"] == {"chatEnabled": True}
    patched = _api(client, "patch", f"{url}/settings", provision_key, {"settings": {"chatEnabled": False}, "survey": None})
    assert patched.status_code == 200 and patched.json()["settings"] == {"chatEnabled": False}
    assert _api(client, "patch", f"{url}/settings", provision_key, {"unknown": 1}).status_code == 400
    sent = {"destinations": [{"platform": "youtube", "server": "rtmp://a/b", "key": "secret-stream-key", "armed": True}]}
    written = _api(client, "patch", f"{url}/destinations", provision_key, sent)
    assert written.status_code == 200 and b"secret-stream-key" not in written.content
    assert not AuditLog.objects.filter(action="hosting.live_destinations_changed", metadata__icontains="secret-stream-key").exists()
    assert _api(client, "post", f"/occurrences/{occ.pk}/engagement", provision_key, {}).json()["final"] is False, "surveys can still come in"
    assert _api(client, "get", f"/occurrences/{occ.pk}/engagement", provision_key).json()["people"] == 0


def test_brand_over_rest_needs_the_admin_scope(client, provision_key, monkeypatch):
    from tests.conftest import make_api_key

    occ = room_occurrence(join_url="")
    session, _ = _linked(occ)
    fake = Service()
    monkeypatch.setattr(base, "default_transport", fake)
    path = f"/hosting/accounts/{session.account_id}/live/brand"
    assert _api(client, "get", path, provision_key).status_code == 403
    admin = make_api_key(("hosting_admin",), name="adm")
    fake.brand = {"accent": "", "stage": "#000000", "mode": "dark", "font": "serif"}
    written = _api(client, "patch", path, admin, {"accent": "#112233"})
    assert written.status_code == 200
    assert fake.brand == {"accent": "#112233", "stage": "#000000", "mode": "dark", "font": "serif"}, "the rest of the theme stays"


def test_mcp_lists_the_live_tools_and_keeps_links_out(client, provision_key, monkeypatch):
    from tests.test_mcp import rpc, tool_names

    occ = room_occurrence(join_url="")
    _linked(occ)
    fake = Service()
    monkeypatch.setattr(base, "default_transport", fake)
    names = tool_names(client, provision_key)
    assert {"list_session_crew", "add_session_crew", "remove_session_crew", "get_session_live_settings", "update_session_live_settings",
            "get_session_live_destinations", "update_session_live_destinations", "get_session_engagement", "pull_session_engagement"} <= names
    result = rpc(client, provision_key, "tools/call", {"name": "add_session_crew", "arguments": {"occurrenceId": str(occ.pk), "role": "guest", "name": "Gus"}})
    assert "/join/" not in json.dumps(result.json())
    listed = rpc(client, provision_key, "tools/call", {"name": "list_session_crew", "arguments": {"occurrenceId": str(occ.pk)}})
    assert "/join/" not in json.dumps(listed.json()) and "Gus" in json.dumps(listed.json())
    dry = rpc(client, provision_key, "tools/call", {"name": "remove_session_crew", "arguments": {"occurrenceId": str(occ.pk), "memberId": fake.crew[0]["id"]}})
    assert fake.crew and "confirmToken" in json.dumps(dry.json()), "gated: a dry run removes nobody"


# ---- the deployment's own brand


def _png(size=(4, 4)):
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGBA", size, (10, 20, 30, 255)).save(out, format="PNG")
    return out.getvalue()


def test_brand_shows_across_the_studio_and_goes_to_the_live_server(admin_client, monkeypatch):
    from django.core.cache import cache
    from django.core.files.uploadedfile import SimpleUploadedFile

    cache.clear()
    occ = room_occurrence(join_url="")
    _linked(occ)
    fake = Service()
    calls = []

    def recording(method, url, body, headers, *, allowed_hosts):
        calls.append((method, url))
        if url.endswith("/brand/logo"):
            return 200, b"{}", None, False
        return fake(method, url, body, headers, allowed_hosts=allowed_hosts)

    monkeypatch.setattr(base, "default_transport", recording)
    page = admin_client.get("/studio/brand/")
    assert b"VisionCast" in page.content
    response = admin_client.post("/studio/brand/", {"name": "Acme Events", "accent": "#123456", "logo": SimpleUploadedFile("l.png", _png(), "image/png")})
    assert response.status_code == 200 and b"Saved" in response.content, response.content[:400]
    home = admin_client.get("/studio/brand/")
    assert b"Acme Events</title>" in home.content.replace(b"\n", b"") or b"Acme Events" in home.content
    assert b"visioncast-mark" not in home.content
    assert fake.brand["accent"] == "#123456"
    assert any(url.endswith("/brand/logo") and method == "PUT" for method, url in calls)
    logo = admin_client.get("/brand/logo.png")
    assert logo.status_code == 200 and logo["Content-Type"] == "image/png" and logo["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert b"--coral-600:#123456" in admin_client.get("/brand/brand.css").content
    cache.clear()


def test_brand_refuses_what_could_harm_or_cant_be_read(admin_client, client, django_user_model):
    from django.core.cache import cache
    from django.core.files.uploadedfile import SimpleUploadedFile

    cache.clear()
    too_light = admin_client.post("/studio/brand/", {"name": "A", "accent": "#fefefe"})
    assert too_light.status_code == 400
    # The person is told what's wrong, at the top and beside the field.
    assert b'class="error-summary"' in too_light.content and b'id="brand-accent-err"' in too_light.content and b'value="#fefefe"' in too_light.content
    assert b'aria-invalid="true"' in too_light.content
    script = admin_client.post("/studio/brand/", {"accent": "red;}body{display:none"})
    assert script.status_code == 400 and b"body{display" not in script.content.split(b"error-summary")[0]
    svg = admin_client.post("/studio/brand/", {"logo": SimpleUploadedFile("x.svg", b"<svg onload=alert(1)>", "image/svg+xml")})
    assert svg.status_code == 400 and b'id="brand-logo-err"' in svg.content
    fake_png = admin_client.post("/studio/brand/", {"logo": SimpleUploadedFile("x.png", b"\x89PNG\r\n\x1a\n<script>", "image/png")})
    assert fake_png.status_code == 400
    editor = django_user_model.objects.create_user("eddie", "e@example.com", "x" * 20)
    from django.contrib.auth.models import Group

    from webinars.roles import apply_roles

    apply_roles()
    editor.groups.add(Group.objects.get(name="Editor"))
    client.force_login(editor)
    assert client.post("/studio/brand/", {"name": "Hacked"}).status_code == 403
    cache.clear()


def test_a_small_file_claiming_a_huge_image_is_refused_before_decoding():
    from django.core.exceptions import ValidationError

    from webinars.brand import clean_logo

    with pytest.raises(ValidationError):
        clean_logo(_png((5000, 5000)))


def test_a_stored_png_is_rebuilt_from_pixels_so_trailing_data_is_gone():
    from webinars.brand import clean_logo

    assert b"<script>" not in clean_logo(_png() + b"<script>alert(1)</script>")


def test_connect_live_server_takes_its_secret_from_the_environment(monkeypatch):
    from django.core.management import CommandError, call_command

    with pytest.raises(CommandError):
        monkeypatch.delenv("LIVE_SERVICE_SECRET", raising=False)
        call_command("connect_live_server", host=HOST)
    monkeypatch.setenv("LIVE_SERVICE_SECRET", SECRET)
    call_command("connect_live_server", host=HOST)
    HostingAccount.objects.filter(config__service_host=HOST).update(enabled=False, config={"service_host": HOST, "auto_record": "yes"})
    call_command("connect_live_server", host=HOST)
    account = HostingAccount.objects.get(provider="livekit", config__service_host=HOST)
    assert account.enabled is False and account.config["auto_record"] == "yes", "running it again changes nothing else"
