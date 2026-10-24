"""Continuation API (docs/continuation-api.md): read-only management scope, direct-mode
calendar route and the capability-authenticated "my registration" status endpoint."""
import json
import uuid

import pytest
from django.utils import timezone

from tests.conftest import (
    COOKIE,
    JOIN_URL,
    ORIGIN,
    WAITING_URL,
    cookie_value,
    make_api_key,
    make_event,
    make_occurrence,
    make_person,
    make_registration,
    payload,
)
from webinars.models import AttendanceEvent, Event, FormVersion, Host, IdempotencyRecord, Occurrence, Registration, Topic

pytestmark = pytest.mark.django_db

BASE = "/api/v1/manage"
SAME_ORIGIN = {"Origin": ORIGIN, "X-Webinar-Request": "1", "Sec-Fetch-Site": "same-origin"}
ROOM_CODE = "roomcode98765"


def call(client, method, path, key, body=None):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    fn = getattr(client, method.lower())
    if method in ("GET", "HEAD", "DELETE"):
        return fn(path, headers=headers)
    return fn(path, data=json.dumps(body if body is not None else {}), content_type="application/json", headers=headers)


def _text(resp):
    return resp.content.decode()


@pytest.fixture
def read_key(db):
    return make_api_key(("config_read",), name="read-only")


@pytest.fixture
def content_key(db):
    return make_api_key(("content_write",), name="cms")


@pytest.fixture
def room_occurrence(event):
    return make_occurrence(event, waiting_room_enabled=True, waiting_room_code=ROOM_CODE)


# ---------------------------------------------------------------------------
# Read-only management scope (config_read, GET only)
# ---------------------------------------------------------------------------


def _read_paths(event, occ):
    return [
        f"{BASE}/events",
        f"{BASE}/events/{event.pk}",
        f"{BASE}/occurrences/{occ.pk}",
        f"{BASE}/occurrences/{occ.pk}/waiting-room",
    ]


def test_read_only_key_can_read_without_private_links(client, event, room_occurrence, read_key):
    draft = make_event(event.form_version, slug="draft-one", visibility="draft")
    make_occurrence(draft)
    for path in _read_paths(event, room_occurrence):
        resp = call(client, "GET", path, read_key)
        assert resp.status_code == 200, (path, resp.content)
        assert resp["Cache-Control"] == "no-store"
        text = _text(resp)
        assert JOIN_URL not in text and WAITING_URL not in text, path
        assert ROOM_CODE not in text, path
    listed = call(client, "GET", f"{BASE}/events", read_key).json()
    assert {e["slug"] for e in listed["results"]} >= {"public-webinar", "draft-one"}
    occ = call(client, "GET", f"{BASE}/occurrences/{room_occurrence.pk}", read_key).json()
    assert occ["hasJoinUrl"] is True and occ["hasWaitingRoomUrl"] is True
    assert occ["waitingRoom"]["enabled"] is True and occ["waitingRoom"]["url"] is None
    room = call(client, "GET", f"{BASE}/occurrences/{room_occurrence.pk}/waiting-room", read_key).json()
    assert room["enabled"] is True and room["url"] is None


def test_content_write_read_unchanged(client, event, room_occurrence, content_key):
    occ = call(client, "GET", f"{BASE}/occurrences/{room_occurrence.pk}", content_key).json()
    assert occ["waitingRoom"]["url"].endswith(f"/w/{ROOM_CODE}/")
    assert JOIN_URL not in json.dumps(occ) and WAITING_URL not in json.dumps(occ)


def test_read_and_write_scopes_together_keep_room_url(client, event, room_occurrence):
    key = make_api_key(("config_read", "content_write"), name="both")
    room = call(client, "GET", f"{BASE}/occurrences/{room_occurrence.pk}/waiting-room", key).json()
    assert room["url"].endswith(f"/w/{ROOM_CODE}/")


def test_read_only_not_found(client, read_key):
    assert call(client, "GET", f"{BASE}/events/{uuid.uuid4()}", read_key).status_code == 404
    assert call(client, "GET", f"{BASE}/occurrences/{uuid.uuid4()}", read_key).status_code == 404


def test_read_requires_key(client, event, room_occurrence):
    for path in _read_paths(event, room_occurrence):
        resp = call(client, "GET", path, None)
        assert resp.status_code == 401, path
        assert resp.json()["error"]["code"] == "unauthorized"
        assert call(client, "GET", path, "wbk_deadbeef_not-a-real-key").status_code == 401, path


def test_revoked_read_key_denied(client, event, read_key):
    from webinars.models import ApiKey

    ApiKey.objects.update(revoked_at=timezone.now())
    assert call(client, "GET", f"{BASE}/events", read_key).status_code == 401


@pytest.mark.parametrize(
    "scopes", [("public_read",), ("reporting",), ("proxy",), ("register",), ("trusted",), ("attendance_write",),
               ("publication",), ("delivery_admin",), ("keys_admin",), ("team_admin",)]
)
def test_other_scopes_cannot_read_management(client, event, room_occurrence, scopes):
    key = make_api_key(scopes, name="other")
    for path in _read_paths(event, room_occurrence):
        resp = call(client, "GET", path, key)
        assert resp.status_code == 403, (scopes, path)
        assert resp.json()["error"]["code"] == "forbidden"


def test_read_only_key_cannot_write(client, form, event, room_occurrence, read_key):
    reg = make_registration(room_occurrence)
    before_event = Event.objects.get(pk=event.pk)
    before_occ = Occurrence.objects.get(pk=room_occurrence.pk)
    writes = [
        ("POST", f"{BASE}/events", {"internalName": "I", "slug": "nope", "publicTitle": "T", "formVersion": form.version}),
        ("PATCH", f"{BASE}/events/{event.pk}", {"publicTitle": "Hijacked"}),
        ("PATCH", f"{BASE}/events/{event.pk}", {"visibility": "draft"}),
        ("DELETE", f"{BASE}/events/{event.pk}", None),
        ("POST", f"{BASE}/events/{event.pk}/occurrences", {"timezone": "Europe/London", "localStart": "2030-01-01T10:00"}),
        ("PATCH", f"{BASE}/occurrences/{room_occurrence.pk}", {"joinUrl": "https://evil.example.org/x"}),
        ("POST", f"{BASE}/occurrences/{room_occurrence.pk}/cancel", {}),
        ("POST", f"{BASE}/occurrences/{room_occurrence.pk}/recording", {"recordingUrl": "https://media.example.org/r"}),
        ("PATCH", f"{BASE}/occurrences/{room_occurrence.pk}/waiting-room", {"admissionMode": "open"}),
        ("POST", f"{BASE}/registrations/{reg.pk}/attendance", {"kind": "attended"}),
        ("POST", f"{BASE}/hosts", {"name": "H", "slug": "h"}),
        ("POST", f"{BASE}/topics", {"name": "T", "slug": "t"}),
        ("POST", f"{BASE}/forms", {"version": "f9", "requiredFields": ["email"]}),
        ("PATCH", f"{BASE}/forms/{form.version}", {"isActive": False}),
        ("PUT", f"{BASE}/events", {}),
        ("HEAD", f"{BASE}/events", None),
    ]
    for method, path, body in writes:
        resp = call(client, method, path, read_key, body)
        assert resp.status_code == 403, (method, path, resp.status_code)
    after_event = Event.objects.get(pk=event.pk)
    after_occ = Occurrence.objects.get(pk=room_occurrence.pk)
    assert after_event.public_title == before_event.public_title and after_event.revision == before_event.revision
    assert after_event.visibility == before_event.visibility
    assert after_occ.status == before_occ.status and after_occ.join_url == JOIN_URL
    assert after_occ.admission_mode == before_occ.admission_mode and not after_occ.recording_url
    assert Event.objects.count() == 1 and Occurrence.objects.count() == 1
    assert not Host.objects.exists() and not Topic.objects.exists()
    assert FormVersion.objects.get(pk=form.pk).is_active is True
    assert not FormVersion.objects.filter(version="f9").exists()
    assert not AttendanceEvent.objects.exists()


def test_in_process_invoke_still_requires_write_scope(event):
    """MCP's in-process path checks ``view.scopes`` (the write scope) for every method."""
    from webinars.api import management
    from webinars.api.auth import authenticate_raw_key

    key = authenticate_raw_key(make_api_key(("config_read",), name="mcp-read"))
    status, body = management.invoke(management.event_detail, "GET", key, pk=event.pk)
    assert status == 403 and body["error"]["code"] == "forbidden"
    status, _ = management.invoke(management.event_detail, "PATCH", key, {"publicTitle": "x"}, pk=event.pk)
    assert status == 403
    assert management.event_detail.scopes == management.CONTENT


def test_read_only_projection_redacts_every_url_key():
    from webinars.api.management import _read_only_projection

    data = {"url": "a", "nested": [{"url": "b", "recordingUrl": "c"}], "waitingRoom": {"url": "d", "enabled": True}}
    assert _read_only_projection(data) == {
        "url": None, "nested": [{"url": None, "recordingUrl": "c"}], "waitingRoom": {"url": None, "enabled": True}
    }


# ---------------------------------------------------------------------------
# Direct-mode calendar route
# ---------------------------------------------------------------------------


def test_direct_calendar_matches_platform_ics(client, occurrence):
    resp = client.get("/webinar-api/calendar.ics", {"occurrenceId": str(occurrence.pk)})
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/calendar")
    assert resp["Content-Disposition"] == f'attachment; filename="{occurrence.event.slug}.ics"'
    reference = client.get(f"/api/v1/occurrences/{occurrence.pk}/calendar.ics")
    assert resp.content == reference.content
    text = _text(resp)
    assert JOIN_URL not in text and WAITING_URL not in text
    assert not resp.cookies


def test_direct_calendar_ignores_cookies_and_needs_no_origin(client, occurrence):
    client.cookies[COOKIE] = "anything"
    resp = client.get("/webinar-api/calendar.ics", {"occurrenceId": str(occurrence.pk).upper()},
                      headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 200
    assert COOKIE not in resp.cookies


@pytest.mark.parametrize(
    "query",
    [{}, {"occurrenceId": "not-a-uuid"}, {"occurrenceId": ""}, {"occurrenceId": "x", "extra": "1"}],
)
def test_direct_calendar_rejects_bad_query(client, occurrence, query):
    resp = client.get("/webinar-api/calendar.ics", query)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "validation_error"


def test_direct_calendar_rejects_extra_or_repeated_params(client, occurrence):
    occ = str(occurrence.pk)
    assert client.get(f"/webinar-api/calendar.ics?occurrenceId={occ}&x=1").status_code == 400
    assert client.get(f"/webinar-api/calendar.ics?occurrenceId={occ}&occurrenceId={occ}").status_code == 400


def test_direct_calendar_hides_drafts_and_unknown(client, form):
    draft = make_occurrence(make_event(form, slug="draft-cal", visibility="draft"))
    assert client.get("/webinar-api/calendar.ics", {"occurrenceId": str(draft.pk)}).status_code == 404
    assert client.get("/webinar-api/calendar.ics", {"occurrenceId": str(uuid.uuid4())}).status_code == 404


def test_direct_calendar_get_only(client, occurrence):
    resp = client.post(f"/webinar-api/calendar.ics?occurrenceId={occurrence.pk}", data="{}",
                       content_type="application/json", headers=SAME_ORIGIN)
    assert resp.status_code == 405


def _direct_register(client, body, **headers):
    base = dict(SAME_ORIGIN)
    base.update(headers)
    return client.post("/webinar-api/registrations", data=json.dumps(body), content_type="application/json", headers=base)


def test_direct_registration_ics_url_rewrite_can_be_disabled(client, settings, occurrence):
    settings.WEBINAR_DIRECT_ICS_REWRITE = False
    resp = _direct_register(client, payload(occurrence))
    assert resp.status_code == 201
    assert resp.json()["calendar"]["icsUrl"] == f"/api/v1/occurrences/{occurrence.pk}/calendar.ics"


def test_direct_registration_ics_url_rewritten_by_default(client, occurrence, proxy_key):
    body = payload(occurrence)
    resp = _direct_register(client, body)
    assert resp.status_code == 201
    expected = f"/webinar-api/calendar.ics?occurrenceId={occurrence.pk}"
    assert resp.json()["calendar"]["icsUrl"] == expected
    assert client.get(expected).status_code == 200
    # Idempotent replay is rewritten too; the stored replay body is not mutated.
    client.cookies[COOKIE] = cookie_value(resp)
    replay = _direct_register(client, body)
    assert replay.json()["calendar"]["icsUrl"] == expected
    for record in IdempotencyRecord.objects.all():
        assert "/webinar-api/" not in json.dumps(record.response_body)
    # Key-authenticated registration keeps the platform path (the Node proxy rewrites it).
    client.cookies.pop(COOKIE, None)
    other = make_occurrence(occurrence.event, days=20)
    api = client.post("/api/v1/registrations", data=json.dumps(payload(other, profile={"email": "other@example.com"})),
                      content_type="application/json", headers={"Authorization": f"Bearer {proxy_key}"})
    assert api.status_code == 201
    assert api.json()["calendar"]["icsUrl"] == f"/api/v1/occurrences/{other.pk}/calendar.ics"


def test_direct_ics_rewrite_drops_unexpected_links():
    from webinars.api.views import _direct_ics_url

    original = {"calendar": {"icsUrl": "https://evil.example.org/x.ics", "uid": "u"}}
    out = _direct_ics_url(original)
    assert "icsUrl" not in out["calendar"] and out["calendar"]["uid"] == "u"
    assert original["calendar"]["icsUrl"] == "https://evil.example.org/x.ics"
    assert _direct_ics_url({"error": {"code": "x"}}) == {"error": {"code": "x"}}


# ---------------------------------------------------------------------------
# My registration (capability cookie)
# ---------------------------------------------------------------------------

MY = "/webinar-api/my-registration"
STATUS_KEYS = {"occurrenceId", "recognized", "registered", "status"}
PII_MARKERS = ("ada.lovelace", "example.com\"", "Ada", "Lovelace", "grace@", "email", "phone", "firstName",
               "joinUrl", "waitingRoomUrl", "personId", "registrationId", "attendance", "consent")


def _my(client, occ, cookie=None, **headers):
    client.cookies.pop(COOKIE, None)
    if cookie is not None:
        client.cookies[COOKIE] = cookie
    base = dict(SAME_ORIGIN)
    base.pop("Origin")  # browsers send no Origin on same-origin GET
    base.update(headers)
    base = {k: v for k, v in base.items() if v is not None}
    return client.get(MY, {"occurrenceId": str(occ.pk)}, headers=base)


def _assert_no_pii(resp, *extra):
    text = _text(resp)
    for marker in PII_MARKERS + (JOIN_URL, WAITING_URL) + extra:
        assert marker not in text, marker


def test_my_registration_unrecognized(client, occurrence):
    resp = _my(client, occurrence)
    assert resp.status_code == 200
    assert resp["Cache-Control"] == "no-store"
    assert resp.json() == {"occurrenceId": str(occurrence.pk), "recognized": False, "registered": False, "status": None}


def test_my_registration_after_direct_registration(client, occurrence):
    reg_resp = _direct_register(client, payload(occurrence))
    token = cookie_value(reg_resp)
    reg = Registration.objects.get()
    resp = _my(client, occurrence, token)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == STATUS_KEYS | {"thankYou", "calendar"}
    assert data["recognized"] is True and data["registered"] is True and data["status"] == "registered"
    assert data["thankYou"] == {"mode": "message", "message": "Thanks for registering!"}
    assert data["calendar"]["icsUrl"] == f"/webinar-api/calendar.ics?occurrenceId={occurrence.pk}"
    assert data["calendar"]["uid"] == occurrence.ics_uid
    _assert_no_pii(resp, str(reg.pk), str(reg.person_id))
    assert not resp.cookies.get(COOKIE)  # never issues or rotates the capability


def test_my_registration_other_occurrence_same_shape(client, occurrence):
    token = cookie_value(_direct_register(client, payload(occurrence)))
    other = make_occurrence(occurrence.event, days=30)
    data = _my(client, other, token).json()
    assert data == {"occurrenceId": str(other.pk), "recognized": True, "registered": False, "status": None}


def test_my_registration_cancelled(client, occurrence):
    from webinars.identity import issue_capability

    person = make_person(email="grace@example.com", first_name="Grace")
    make_registration(occurrence, person, status=Registration.Status.CANCELLED)
    resp = _my(client, occurrence, issue_capability(person))
    assert resp.json() == {"occurrenceId": str(occurrence.pk), "recognized": True, "registered": False,
                           "status": "cancelled"}
    _assert_no_pii(resp)


def test_my_registration_revoked_or_erased_capability(client, occurrence):
    from webinars.identity import issue_capability
    from webinars.models import BrowserCapability

    person = make_person(email="grace@example.com")
    make_registration(occurrence, person)
    token = issue_capability(person)
    assert _my(client, occurrence, token).json()["registered"] is True
    BrowserCapability.objects.update(revoked_at=timezone.now())
    assert _my(client, occurrence, token).json()["recognized"] is False
    token2 = issue_capability(person)
    person.erased_at = timezone.now()
    person.save(update_fields=["erased_at"])
    data = _my(client, occurrence, token2).json()
    assert data["recognized"] is False and data["registered"] is False


def test_my_registration_does_not_reveal_unverified_registration(client, occurrence):
    """An unknown browser registering an existing email gets a decoy cookie; the status
    endpoint must not confirm that a registration exists for that email."""
    _direct_register(client, payload(occurrence))
    other = make_occurrence(occurrence.event, days=30)
    client.cookies.pop(COOKIE, None)
    second = _direct_register(client, payload(other))
    assert second.status_code == 201
    decoy = cookie_value(second)
    assert Registration.objects.filter(occurrence=other, identity_verified=False).exists()
    resp = _my(client, other, decoy)
    assert resp.json() == {"occurrenceId": str(other.pk), "recognized": False, "registered": False, "status": None}


def test_my_registration_ignores_identifiers_in_query(client, occurrence):
    person = make_person(email="grace@example.com")
    reg = make_registration(occurrence, person)
    for extra in ({"email": "grace@example.com"}, {"personId": str(person.pk)}, {"registrationId": str(reg.pk)}):
        resp = client.get(MY, {"occurrenceId": str(occurrence.pk), **extra}, headers={"X-Webinar-Request": "1"})
        assert resp.status_code == 400


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Webinar-Request": None},
        {"X-Webinar-Request": "0"},
        {"Origin": "https://evil.example.org"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_my_registration_csrf_and_origin_checks(client, occurrence, headers):
    token = cookie_value(_direct_register(client, payload(occurrence)))
    resp = _my(client, occurrence, token, **headers)
    assert resp.status_code == 403
    _assert_no_pii(resp)
    assert "registered" not in resp.json()


def test_my_registration_double_submit_token_accepted(client, occurrence):
    client.cookies["__Host-webinar_csrf"] = "tok123"
    resp = _my(client, occurrence, None, **{"X-Webinar-Request": None, "X-Webinar-CSRF": "tok123"})
    # _my pops only the capability cookie, so the CSRF cookie set above is still sent.
    assert resp.status_code == 200


def test_my_registration_query_validation_and_visibility(client, form, occurrence):
    hdr = {"X-Webinar-Request": "1"}
    assert client.get(MY, headers=hdr).status_code == 400
    assert client.get(MY, {"occurrenceId": "nope"}, headers=hdr).status_code == 400
    assert client.get(MY, {"occurrenceId": str(uuid.uuid4())}, headers=hdr).status_code == 404
    draft = make_occurrence(make_event(form, slug="draft-my", visibility="draft"))
    assert client.get(MY, {"occurrenceId": str(draft.pk)}, headers=hdr).status_code == 404


def test_my_registration_get_only(client, occurrence):
    resp = client.post(f"{MY}?occurrenceId={occurrence.pk}", data="{}", content_type="application/json", headers=SAME_ORIGIN)
    assert resp.status_code == 405


def test_my_registration_rate_limited(client, settings, monkeypatch, occurrence):
    from webinars.api import views

    settings.RATE_LIMIT_ENABLED = True
    monkeypatch.setattr(views, "MY_REGISTRATION_LIMIT", (2, 60))
    assert _my(client, occurrence).status_code == 200
    assert _my(client, occurrence).status_code == 200
    resp = _my(client, occurrence)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"


def test_api_my_registration_requires_proxy_key(client, occurrence, proxy_key):
    url = f"/api/v1/my-registration?occurrenceId={occurrence.pk}"
    assert client.get(url).status_code == 401
    for scopes in (("register",), ("trusted",), ("config_read",), ("public_read",), ("content_write",)):
        assert client.get(url, headers={"Authorization": f"Bearer {make_api_key(scopes, name='x')}"}).status_code == 403
    token = cookie_value(_direct_register(client, payload(occurrence)))
    client.cookies[COOKIE] = token
    resp = client.get(url, headers={"Authorization": f"Bearer {proxy_key}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["registered"] is True
    assert data["calendar"]["icsUrl"] == f"/api/v1/occurrences/{occurrence.pk}/calendar.ics"
    _assert_no_pii(resp)
    assert client.post(url, headers={"Authorization": f"Bearer {proxy_key}"}).status_code == 405
