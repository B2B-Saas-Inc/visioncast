from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from tests.conftest import JOIN_URL, WAITING_URL, make_event, make_occurrence
from webinars.models import Host, Topic
from webinars.scheduling import cancel_occurrence, reschedule_occurrence

pytestmark = pytest.mark.django_db


def _slugs(resp):
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == len(data["results"]) or data["count"] >= len(data["results"])
    return {e["slug"] for e in data["results"]}


def _no_links(resp):
    text = resp.content.decode()
    assert JOIN_URL not in text
    assert WAITING_URL not in text
    assert "joinUrl" not in text and "waitingRoomUrl" not in text


@pytest.fixture
def catalogue(form):
    ai = Topic.objects.create(name="AI", slug="ai")
    sec = Topic.objects.create(name="Security", slug="security")
    alice = Host.objects.create(name="Alice", slug="alice")
    bob = Host.objects.create(name="Bob", slug="bob")
    pub_ai = make_event(form, slug="pub-ai", public_title="Machine learning basics")
    pub_ai.topics.add(ai)
    pub_ai.hosts.add(alice)
    make_occurrence(pub_ai)
    pub_sec = make_event(form, slug="pub-sec", public_title="Zero trust networks")
    pub_sec.topics.add(sec)
    pub_sec.hosts.add(bob)
    make_occurrence(pub_sec)
    draft = make_event(form, slug="draft-ev", visibility="draft")
    draft.topics.add(ai)
    make_occurrence(draft)
    unlisted = make_event(form, slug="unlisted-ev", visibility="unlisted")
    make_occurrence(unlisted)
    recorded = make_event(form, slug="recorded-ev", public_title="Last year's keynote")
    make_occurrence(
        recorded,
        starts_at=timezone.now() - timedelta(days=30),
        recording_url="https://media.example.com/rec/1",
        recording_published_at=timezone.now() - timedelta(days=29),
    )
    return locals()


def test_list_only_public(client, catalogue):
    slugs = _slugs(client.get("/api/v1/events"))
    assert {"pub-ai", "pub-sec", "recorded-ev"} <= slugs
    assert "draft-ev" not in slugs
    assert "unlisted-ev" not in slugs


def test_list_filter_topic(client, catalogue):
    assert _slugs(client.get("/api/v1/events", {"topic": "ai"})) == {"pub-ai"}


def test_list_filter_host(client, catalogue):
    assert _slugs(client.get("/api/v1/events", {"host": "bob"})) == {"pub-sec"}


def test_list_search(client, catalogue):
    assert _slugs(client.get("/api/v1/events", {"q": "zero trust"})) == {"pub-sec"}


def test_list_when_recorded(client, catalogue):
    assert _slugs(client.get("/api/v1/events", {"when": "recorded"})) == {"recorded-ev"}


def test_list_when_upcoming_excludes_past_only(client, catalogue):
    slugs = _slugs(client.get("/api/v1/events", {"when": "upcoming"}))
    assert {"pub-ai", "pub-sec"} <= slugs
    assert "recorded-ev" not in slugs


def test_list_has_no_private_links(client, catalogue):
    _no_links(client.get("/api/v1/events"))


def test_detail_public_and_unlisted_ok_draft_404(client, catalogue):
    pub = client.get("/api/v1/events/pub-ai")
    assert pub.status_code == 200
    assert pub.json()["slug"] == "pub-ai"
    _no_links(pub)
    unl = client.get("/api/v1/events/unlisted-ev")
    assert unl.status_code == 200
    _no_links(unl)
    assert client.get("/api/v1/events/draft-ev").status_code == 404
    assert client.get("/api/v1/events/does-not-exist").status_code == 404


def test_detail_exposes_published_recording_only(client, catalogue):
    data = client.get("/api/v1/events/recorded-ev").json()
    assert "https://media.example.com/rec/1" in str(data)


def test_ics_contents(client, occurrence):
    resp = client.get(f"/api/v1/occurrences/{occurrence.id}/calendar.ics")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/calendar")
    body = resp.content.decode()
    assert "BEGIN:VCALENDAR\r\n" in body
    assert f"UID:{occurrence.ics_uid}" in body.replace("\r\n ", "")
    assert "SEQUENCE:0\r\n" in body
    assert "\n" not in body.replace("\r\n", "")
    assert JOIN_URL not in body and WAITING_URL not in body
    start = occurrence.starts_at.strftime("%Y%m%dT%H%M%SZ")
    assert f"DTSTART:{start}" in body


def test_ics_draft_404(client, form):
    occ = make_occurrence(make_event(form, slug="d", visibility="draft"))
    assert client.get(f"/api/v1/occurrences/{occ.id}/calendar.ics").status_code == 404


def test_ics_sequence_increments_after_reschedule(client, occurrence):
    uid = occurrence.ics_uid
    new_local = datetime.combine((timezone.now() + timedelta(days=30)).date(), datetime.min.time()).replace(hour=15)
    reschedule_occurrence(occurrence, local_start=new_local, tz_name="Europe/London")
    body = client.get(f"/api/v1/occurrences/{occurrence.id}/calendar.ics").content.decode()
    assert "SEQUENCE:1\r\n" in body
    assert f"UID:{uid}" in body.replace("\r\n ", "")


def test_ics_cancel(client, occurrence):
    cancel_occurrence(occurrence)
    body = client.get(f"/api/v1/occurrences/{occurrence.id}/calendar.ics").content.decode()
    assert "METHOD:CANCEL" in body
    assert "STATUS:CANCELLED" in body
    assert "SEQUENCE:1\r\n" in body


def test_healthz(client, db):
    resp = client.get("/healthz")
    assert resp.status_code == 200
