"""Continuation glue: settings and system check, calendar links on public and registration
responses, OpenAPI coverage of the new routes, the packaged implementation guide, and
the Django admin publication readiness check (services.publish_problems)."""
import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests.conftest import (
    COOKIE,
    JOIN_URL,
    ORIGIN,
    WAITING_URL,
    cookie_value,
    make_event,
    make_occurrence,
    payload,
    post_registration,
)
from webinars.models import Event, Occurrence

pytestmark = pytest.mark.django_db

SAME_ORIGIN = {"Origin": ORIGIN, "X-Webinar-Request": "1", "Sec-Fetch-Site": "same-origin"}
WEB_KEYS = ("googleUrl", "outlookUrl", "office365Url")


def _no_private(text):
    assert JOIN_URL not in text and WAITING_URL not in text


# ---------------------------------------------------------------------------
# Settings and system check
# ---------------------------------------------------------------------------


def test_settings_read_from_environment_with_defaults():
    # Source check: the test fixture pins both values, so the env defaults are asserted here.
    from pathlib import Path

    from django.conf import settings

    source = (Path(settings.BASE_DIR) / "config" / "settings.py").read_text()
    assert 'PUBLIC_EVENT_URL_TEMPLATE = env_str("PUBLIC_EVENT_URL_TEMPLATE")' in source
    assert 'WEBINAR_DIRECT_ICS_REWRITE = env_bool("WEBINAR_DIRECT_ICS_REWRITE", True)' in source
    example_path = Path(settings.BASE_DIR) / ".env.example"
    if example_path.is_file():  # not shipped in the image
        example = example_path.read_text().splitlines()
        assert "PUBLIC_EVENT_URL_TEMPLATE=" in example and "WEBINAR_DIRECT_ICS_REWRITE=1" in example


def test_public_event_url_check_is_registered(settings):
    from django.core.checks import run_checks

    settings.PUBLIC_EVENT_URL_TEMPLATE = "http://www.example.com/webinars/{slug}"
    assert "webinars.E101" in {e.id for e in run_checks(tags=["webinars"])}
    settings.PUBLIC_EVENT_URL_TEMPLATE = "https://www.example.com/webinars/{slug}"
    assert "webinars.E101" not in {e.id for e in run_checks(tags=["webinars"])}


# ---------------------------------------------------------------------------
# Public projection: calendarLinks
# ---------------------------------------------------------------------------


def test_public_occurrence_has_calendar_links(client, event, occurrence):
    data = client.get(f"/api/v1/events/{event.slug}").json()
    occ = data["occurrences"][0]
    links = occ["calendarLinks"]
    assert set(links) == {"ics", "google", "outlook", "office365"}
    assert links["ics"] == occ["calendarUrl"]
    assert links["google"].startswith("https://calendar.google.com/calendar/render?")
    assert links["outlook"].startswith("https://outlook.live.com/calendar/0/deeplink/compose?")
    assert links["office365"].startswith("https://outlook.office.com/calendar/0/deeplink/compose?")
    _no_private(json.dumps(data))


def test_public_calendar_links_use_template(client, settings, event, occurrence):
    settings.PUBLIC_EVENT_URL_TEMPLATE = "https://www.example.com/webinars/{slug}"
    links = client.get(f"/api/v1/events/{event.slug}").json()["occurrences"][0]["calendarLinks"]
    assert "www.example.com%2Fwebinars%2F" in links["google"]


def test_cancelled_occurrence_has_ics_only(client, event, occurrence):
    Occurrence.objects.filter(pk=occurrence.pk).update(status=Occurrence.Status.CANCELLED)
    links = client.get(f"/api/v1/events/{event.slug}").json()["occurrences"][0]["calendarLinks"]
    assert links["ics"] and links["google"] is None and links["outlook"] is None and links["office365"] is None


def test_calendar_links_add_no_queries_per_occurrence(client, event, occurrence):
    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert client.get("/api/v1/events").status_code == 200
        return len(ctx.captured_queries)

    one = count()
    make_occurrence(event, days=20)
    make_occurrence(event, days=30)
    assert count() == one


# ---------------------------------------------------------------------------
# Registration responses: web calendar links (icsUrl unchanged)
# ---------------------------------------------------------------------------


def test_key_registration_has_web_calendar_links(client, occurrence, proxy_key):
    resp = post_registration(client, payload(occurrence), proxy_key)
    assert resp.status_code == 201
    cal = resp.json()["calendar"]
    assert cal["icsUrl"] == f"/api/v1/occurrences/{occurrence.pk}/calendar.ics"
    assert cal["googleUrl"].startswith("https://calendar.google.com/")
    assert cal["outlookUrl"].startswith("https://outlook.live.com/")
    assert cal["office365Url"].startswith("https://outlook.office.com/")
    _no_private(resp.content.decode())


def test_direct_registration_and_my_registration_carry_web_links(client, occurrence):
    reg = client.post("/webinar-api/registrations", data=json.dumps(payload(occurrence)),
                      content_type="application/json", headers=SAME_ORIGIN)
    assert reg.status_code == 201
    cal = reg.json()["calendar"]
    assert cal["icsUrl"] == f"/webinar-api/calendar.ics?occurrenceId={occurrence.pk}"
    assert all(cal[k] for k in WEB_KEYS)
    client.cookies[COOKIE] = cookie_value(reg)
    mine = client.get("/webinar-api/my-registration", {"occurrenceId": str(occurrence.pk)},
                      headers={"X-Webinar-Request": "1"})
    assert mine.status_code == 200
    assert mine.json()["calendar"]["icsUrl"] == f"/webinar-api/calendar.ics?occurrenceId={occurrence.pk}"
    assert all(mine.json()["calendar"][k] for k in WEB_KEYS)
    _no_private(mine.content.decode())


def test_rewrite_switch_off_keeps_platform_ics_path_everywhere(client, settings, occurrence):
    settings.WEBINAR_DIRECT_ICS_REWRITE = False
    reg = client.post("/webinar-api/registrations", data=json.dumps(payload(occurrence)),
                      content_type="application/json", headers=SAME_ORIGIN)
    platform = f"/api/v1/occurrences/{occurrence.pk}/calendar.ics"
    assert reg.json()["calendar"]["icsUrl"] == platform
    client.cookies[COOKIE] = cookie_value(reg)
    mine = client.get("/webinar-api/my-registration", {"occurrenceId": str(occurrence.pk)},
                      headers={"X-Webinar-Request": "1"})
    assert mine.json()["calendar"]["icsUrl"] == platform
    # The direct calendar route itself stays available either way.
    assert client.get("/webinar-api/calendar.ics", {"occurrenceId": str(occurrence.pk)}).status_code == 200


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


@pytest.fixture
def spec(client):
    return client.get("/api/v1/openapi.json").json()


def test_openapi_documents_new_routes(spec):
    paths = spec["paths"]
    assert paths["/api/v1/my-registration"]["get"]["security"] == [{"apiKey": []}]
    assert paths["/webinar-api/my-registration"]["get"]["security"] == []
    assert "429" in paths["/webinar-api/my-registration"]["get"]["responses"]
    assert paths["/webinar-api/calendar.ics"]["get"]["security"] == []
    for path in ("/api/v1/my-registration", "/webinar-api/my-registration", "/webinar-api/calendar.ics"):
        params = paths[path]["get"]["parameters"]
        assert [(p["name"], p["in"], p["required"]) for p in params] == [("occurrenceId", "query", True)]


def test_openapi_every_route_is_documented(spec):
    from webinars.urls import urlpatterns

    documented = set(spec["paths"])
    skip = {"studio/", "developers/", "developers/api/", "developers/mcp/", "api/v1/openapi.json",
            "media/covers/<uuid:pk>.<str:ext>"}
    for pattern in urlpatterns:
        route = str(pattern.pattern)
        if route in skip:
            continue
        path = "/" + route
        for conv, name in (("<uuid:pk>", "{id}"), ("<slug:slug>", "{slug}"), ("<str:version>", "{version}"),
                           ("<str:code>", "{code}"), ("<str:token>", "{token}")):
            path = path.replace(conv, name)
        assert path in documented, path


def test_openapi_management_reads_mention_config_read(spec):
    for path in ("/api/v1/manage/events", "/api/v1/manage/events/{id}", "/api/v1/manage/occurrences/{id}",
                 "/api/v1/manage/occurrences/{id}/waiting-room"):
        assert "config_read" in spec["paths"][path]["get"]["description"], path
        for method in ("post", "patch", "delete"):
            op = spec["paths"][path].get(method)
            assert op is None or "config_read" not in op.get("description", ""), (path, method)


def test_openapi_schemas_match_responses(client, spec, event, occurrence, proxy_key):
    schemas = spec["components"]["schemas"]
    links = client.get(f"/api/v1/events/{event.slug}").json()["occurrences"][0]["calendarLinks"]
    assert set(links) == set(schemas["CalendarLinks"]["properties"])
    assert "calendarLinks" in schemas["PublicOccurrence"]["properties"]
    cal = post_registration(client, payload(occurrence), proxy_key).json()["calendar"]
    assert set(cal) == set(schemas["RegistrationCalendar"]["properties"])
    mine = client.get("/webinar-api/my-registration", {"occurrenceId": str(occurrence.pk)},
                      headers={"X-Webinar-Request": "1"}).json()
    my_schema = schemas["MyRegistration"]
    assert set(mine) <= set(my_schema["properties"]) and set(my_schema["required"]) <= set(mine)
    assert not set(my_schema["properties"]) & {"email", "personId", "registrationId", "joinUrl", "waitingRoomUrl"}


# ---------------------------------------------------------------------------
# Packaged implementation guide
# ---------------------------------------------------------------------------


def test_implementation_guide_is_packaged():
    from webinars.mcp_resources import _SECRET_PATTERNS, GUIDES, PLAYBOOK, read_guide

    assert GUIDES["implementation-guide"] == "docs/implementation-guide.md"
    text = read_guide("implementation-guide")
    assert text and not any(p.search(text) for p in _SECRET_PATTERNS)
    assert "webinar://guides/implementation-guide" in PLAYBOOK


# ---------------------------------------------------------------------------
# Django admin publication readiness (B4)
# ---------------------------------------------------------------------------


def _admin_form(event, **changes):
    from webinars.admin import AdminEventForm
    from webinars.services import event_form_data

    data = event_form_data(event)
    data.update(changes)
    return AdminEventForm(data=data, instance=Event.objects.get(pk=event.pk))


def test_event_admin_uses_readiness_form():
    from django.contrib import admin

    from webinars.admin import AdminEventForm

    assert admin.site._registry[Event].form is AdminEventForm


def test_admin_refuses_publishing_unready_event(form):
    draft = make_event(form, slug="admin-unready", visibility="draft", action="")
    f = _admin_form(draft, visibility="public")
    assert not f.is_valid()
    messages = f.errors["visibility"]
    assert "The event is not ready to publish." in messages
    assert "Schedule at least one upcoming session." in messages
    assert "Write the Action copy: what should visitors do?" in messages
    assert Event.objects.get(pk=draft.pk).visibility == "draft"


def test_admin_publishes_ready_event(form):
    draft = make_event(form, slug="admin-ready", visibility="draft", action="Register now")
    make_occurrence(draft)
    for target in ("public", "unlisted"):
        assert _admin_form(draft, visibility=target).is_valid(), target


def test_admin_checks_the_submitted_candidate(form):
    draft = make_event(form, slug="admin-candidate", visibility="draft", action="")
    make_occurrence(draft)
    assert _admin_form(draft, visibility="public", action="Save your seat").is_valid()
    ready = make_event(form, slug="admin-candidate-2", visibility="draft", action="Register")
    make_occurrence(ready)
    f = _admin_form(ready, visibility="public", action="")
    assert not f.is_valid() and "Write the Action copy: what should visitors do?" in f.errors["visibility"]


def test_admin_unpublish_and_unchanged_visibility_not_blocked(form):
    live = make_event(form, slug="admin-live", visibility="public", action="")  # no sessions: not ready
    assert _admin_form(live, public_title="Renamed").is_valid()  # same visibility: no transition
    assert _admin_form(live, visibility="draft").is_valid()


def test_admin_add_view_cannot_create_published_event(form):
    from webinars.admin import AdminEventForm
    from webinars.services import event_form_data

    template = make_event(form, slug="admin-template", visibility="draft", action="Register")
    data = event_form_data(template)
    data.update(slug="admin-new", visibility="public")
    f = AdminEventForm(data=data)
    assert not f.is_valid()
    assert "Schedule at least one upcoming session." in f.errors["visibility"]
    data["visibility"] = "draft"
    assert AdminEventForm(data=data).is_valid()
