"""Automatic provisioning: the hosting account marked ``auto_provision`` creates a provider
session, and opens the lobby, for every new session; a reschedule is sent on
(docs/hosting.md → Automatic rooms)."""
import json
import uuid

import pytest
from django.core.exceptions import ValidationError

from tests.conftest import make_event, make_form, make_occurrence, make_registration
from tests.test_hosting_livekit import HOST, SECRET, Service
from tests.test_waiting_room import room_occurrence
from webinars import crypto, services
from webinars.hosting import service
from webinars.hosting.livekit import room_id
from webinars.models import HostedSession, HostingAccount, Occurrence

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def allowlisted(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = [HOST]


def _account(auto=True, enabled=True, provider="livekit"):
    config = {"service_host": HOST} if provider == "livekit" else {}
    account = HostingAccount.objects.create(provider=provider, name=f"a-{uuid.uuid4().hex[:6]}", enabled=enabled,
                                            auto_provision=auto, config=config)
    account.credentials_encrypted = crypto.encrypt_secret(json.dumps({"service_secret": SECRET}))
    account.save()
    return account


def _new_session(event, fake, capture, start="2030-05-05T14:00"):
    """Create a session the way the studio, API and MCP do, letting on-commit hooks run."""
    with capture(execute=True):
        occ = services.save_occurrence({"timezone": "America/New_York", "local_start": start}, event=event)
    return Occurrence.objects.get(pk=occ.pk)


def test_only_one_account_is_automatic_and_it_must_be_enabled_and_able_to_create():
    first = _account()
    second, _ = service.save_account(provider="livekit", name="two", config={"service_host": HOST},
                                     posted_secrets={"service_secret": SECRET}, enabled=True, auto_provision=True)
    first.refresh_from_db()
    assert second.auto_provision and not first.auto_provision
    assert service.auto_account() == second
    with pytest.raises(ValidationError) as err:
        service.save_account(provider="livekit", name="off", config={"service_host": HOST},
                             posted_secrets={"service_secret": SECRET}, enabled=False, auto_provision=True)
    assert "auto_provision" in err.value.message_dict
    with pytest.raises(ValidationError) as err:
        service.save_account(provider="streamyard", name="sy", config={}, posted_secrets={}, enabled=True,
                             auto_provision=True)
    assert "auto_provision" in err.value.message_dict
    # Updating without the flag keeps it.
    kept, _ = service.save_account(second, provider="livekit", name="two again", config={"service_host": HOST},
                                   posted_secrets={}, enabled=True)
    assert kept.auto_provision
    assert service.serialize_account(kept)["autoProvision"] is True


def test_a_new_session_gets_its_room_and_lobby(django_capture_on_commit_callbacks, monkeypatch):
    _account()
    fake = Service()
    monkeypatch.setattr("webinars.hosting.base.default_transport", fake)
    occ = _new_session(make_event(make_form(), slug="auto"), fake, django_capture_on_commit_callbacks)
    session = HostedSession.objects.get(occurrence=occ)
    assert session.state == HostedSession.State.LINKED and session.mode == HostedSession.Mode.PROVISIONED
    assert session.external_id == room_id(occ)
    assert fake.calls[0]["method"] == "PUT" and fake.calls[0]["url"] == f"https://{HOST}/v1/rooms/{room_id(occ)}"
    assert occ.waiting_room_enabled and occ.waiting_room_code, "the lobby link is live at once"


def test_nothing_happens_without_an_automatic_account(django_capture_on_commit_callbacks, monkeypatch):
    _account(auto=False)
    fake = Service()
    monkeypatch.setattr("webinars.hosting.base.default_transport", fake)
    occ = _new_session(make_event(make_form(), slug="manual"), fake, django_capture_on_commit_callbacks)
    assert not HostedSession.objects.filter(occurrence=occ).exists() and not fake.calls
    assert not occ.waiting_room_enabled


def test_a_session_created_with_its_own_join_link_is_left_alone(django_capture_on_commit_callbacks, monkeypatch):
    _account()
    fake = Service()
    monkeypatch.setattr("webinars.hosting.base.default_transport", fake)
    with django_capture_on_commit_callbacks(execute=True):
        occ = services.save_occurrence({"timezone": "UTC", "local_start": "2030-06-06T10:00",
                                        "join_url": "https://zoom.us/j/123"}, event=make_event(make_form(), slug="own"))
    assert not HostedSession.objects.filter(occurrence=occ).exists() and not fake.calls


def test_a_provider_failure_is_recorded_not_raised(django_capture_on_commit_callbacks, monkeypatch):
    _account()
    fake = Service()
    fake.fail = 503
    monkeypatch.setattr("webinars.hosting.base.default_transport", fake)
    occ = _new_session(make_event(make_form(), slug="down"), fake, django_capture_on_commit_callbacks)
    session = HostedSession.objects.get(occurrence=occ)
    assert session.state in (HostedSession.State.FAILED, HostedSession.State.NEEDS_RECONCILIATION)
    assert occ.waiting_room_enabled, "the lobby still opens; the room is retried from the session page"


def test_a_reschedule_is_sent_on_for_automatic_sessions(django_capture_on_commit_callbacks, monkeypatch):
    _account()
    fake = Service()
    monkeypatch.setattr("webinars.hosting.base.default_transport", fake)
    occ = _new_session(make_event(make_form(), slug="move"), fake, django_capture_on_commit_callbacks)
    calls_before = len(fake.calls)
    with django_capture_on_commit_callbacks(execute=True):
        services.save_occurrence({"local_start": "2030-05-06T15:00"}, instance=occ)
    session = HostedSession.objects.get(occurrence=occ)
    assert session.state == HostedSession.State.LINKED, session.last_error
    assert len(fake.calls) == calls_before + 1 and fake.calls[-1]["method"] == "PUT"


def test_a_reschedule_of_a_hand_linked_session_only_marks_it(django_capture_on_commit_callbacks, monkeypatch):
    account = _account(auto=False)
    fake = Service()
    monkeypatch.setattr("webinars.hosting.base.default_transport", fake)
    occ = room_occurrence(join_url="")
    service.execute(occ, account, "create", transport=fake)
    calls_before = len(fake.calls)
    with django_capture_on_commit_callbacks(execute=True):
        services.save_occurrence({"local_start": "2030-05-06T15:00"}, instance=occ)
    assert HostedSession.objects.get(occurrence=occ).state == HostedSession.State.OUT_OF_DATE
    assert len(fake.calls) == calls_before


# ---- deleting sessions and events ------------------------------------------------------------


def test_delete_session_refuses_registrations_and_orders_then_deletes():
    occ = room_occurrence(join_url="")
    make_registration(occ)
    with pytest.raises(ValidationError):
        services.delete_occurrence(occ)
    empty = room_occurrence(join_url="")
    services.delete_occurrence(empty)
    assert not Occurrence.objects.filter(pk=empty.pk).exists()


def test_delete_event_any_visibility_without_registrations():
    from webinars.models import Event

    event = make_event(make_form(), slug="pub-del", visibility="public")
    services.save_occurrence({"timezone": "UTC", "local_start": "2030-01-01T10:00"}, event=event)
    services.delete_event(event)
    assert not Event.objects.filter(pk=event.pk).exists()
    busy = make_event(make_form(version="af-busy"), slug="busy", visibility="public")
    make_registration(make_occurrence(busy))
    assert services.event_delete_problem(busy)
    with pytest.raises(ValidationError):
        services.delete_event(busy)


def test_studio_deletes_a_session_and_an_event(django_user_model):
    from django.test import Client

    from tests.test_studio_rbac import make_user
    from webinars.models import Event

    admin = make_user(django_user_model, "admin-del", "Administrator")
    c = Client()
    c.force_login(admin)
    event = make_event(make_form(version="st-del"), slug="st-del", visibility="public")
    occ = make_occurrence(event)
    page = c.get(f"/studio/sessions/{occ.pk}/")
    assert page.status_code == 200 and b"Delete session" in page.content
    assert c.post(f"/studio/sessions/{occ.pk}/delete").status_code == 302
    assert not Occurrence.objects.filter(pk=occ.pk).exists()
    busy = make_occurrence(event)
    make_registration(busy)
    page = c.get(f"/studio/sessions/{busy.pk}/")
    assert b"Cancel the session instead" in page.content
    c.post(f"/studio/sessions/{busy.pk}/delete")
    assert Occurrence.objects.filter(pk=busy.pk).exists()
    page = c.get(f"/studio/events/{event.pk}/")
    assert b"Delete event" in page.content and b"Cancel their sessions instead" in page.content
    c.post(f"/studio/events/{event.pk}/delete")
    assert Event.objects.filter(pk=event.pk).exists()
