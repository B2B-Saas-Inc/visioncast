"""Public event URL template, ICS hardening and Google/Outlook calendar links."""
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.utils import timezone

from tests.conftest import JOIN_URL, WAITING_URL, make_event, make_occurrence
from webinars import calendar_links
from webinars.calendar_links import TemplateError, calendar_links as links_for, check_public_event_url_template
from webinars.ics import build_ics
from webinars.scheduling import cancel_occurrence

pytestmark = pytest.mark.django_db

TEMPLATE = "https://www.example.com/webinars/{slug}"


def _unfold(body):
    return body.replace("\r\n ", "")


def _prop(body, name):
    for line in _unfold(body).split("\r\n"):
        if line.startswith(name + ":"):
            return line[len(name) + 1:]
    return None


def _assert_private_absent(text, occ=None):
    assert JOIN_URL not in text and WAITING_URL not in text
    assert "secret-join-token" not in text and "secret-waiting-token" not in text
    if occ is not None and occ.waiting_room_code:
        assert occ.waiting_room_code not in text


# ---------------------------------------------------------------------------
# Template validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", [
    "https://www.example.com/webinars/{slug}",
    "https://www.example.com/webinars/{slug}/sessions/{occurrence_id}",
    "https://www.example.com/w?event={slug}&session={occurrence_id}#register",
    "https://www.example.com/events#{slug}",
])
def test_valid_templates(template):
    assert calendar_links.validate_template(template)


@pytest.mark.parametrize("template", [
    "",
    "https://www.example.com/webinars/static-page",  # no placeholder
    "http://www.example.com/webinars/{slug}",  # not https
    "https://{slug}.example.com/",  # placeholder in host
    "https://www.example.com:{slug}/",  # placeholder in port
    "https://www.example.com{slug}",  # placeholder in authority
    "{slug}",
    "https://www.example.com/{id}",
    "https://www.example.com/{slug.__class__}",
    "https://www.example.com/{slug[0]}",
    "https://www.example.com/{slug!r}",
    "https://www.example.com/{slug:>40}",
    "https://www.example.com/{}",
    "https://www.example.com/{{slug}}",
    "https://www.example.com/{slug",
    "https://www.example.com/slug}",
    "https://user:pw@www.example.com/{slug}",
    "https://www.example.com:8443/{slug}",
    "javascript:alert(1)//{slug}",
    "https://www.example.com/{slug}\r\nATTENDEE:mailto:evil@example.net",
    "https://www.example.com/{slug}\nX",
    "https://www.example.com/ {slug}",
    'https://www.example.com/{slug}"',
    "https://www.example.com/<{slug}>",
    "https://www.example.com/\\{slug}",
    "https://www.exämple.com/{slug}",
    "https://www.example.com/" + "a" * 500 + "{slug}",
])
def test_invalid_templates_rejected(template):
    with pytest.raises(TemplateError):
        calendar_links.validate_template(template)


def test_system_check(settings):
    settings.PUBLIC_EVENT_URL_TEMPLATE = ""
    assert check_public_event_url_template() == []
    settings.PUBLIC_EVENT_URL_TEMPLATE = TEMPLATE
    assert check_public_event_url_template() == []
    settings.PUBLIC_EVENT_URL_TEMPLATE = "https://{slug}.example.com/"
    errors = check_public_event_url_template()
    assert [e.id for e in errors] == ["webinars.E101"]
    assert "invalid" in errors[0].msg


# ---------------------------------------------------------------------------
# Public event URL
# ---------------------------------------------------------------------------


def test_unset_template_keeps_api_url(settings, occurrence):
    settings.PUBLIC_EVENT_URL_TEMPLATE = ""
    expected = "https://webinars.example.com/api/v1/events/public-webinar"
    assert calendar_links.public_event_url(occurrence.event, occurrence) == expected
    assert _prop(build_ics(occurrence), "URL") == expected


def test_setting_absent_keeps_api_url(settings, occurrence):
    if hasattr(settings, "PUBLIC_EVENT_URL_TEMPLATE"):
        del settings.PUBLIC_EVENT_URL_TEMPLATE
    assert calendar_links.public_event_url(occurrence.event) == "https://webinars.example.com/api/v1/events/public-webinar"


def test_template_used_in_ics(settings, occurrence):
    settings.PUBLIC_EVENT_URL_TEMPLATE = TEMPLATE
    body = build_ics(occurrence)
    assert _prop(body, "URL") == "https://www.example.com/webinars/public-webinar"
    assert "https://www.example.com/webinars/public-webinar" in _prop(body, "DESCRIPTION")
    assert "/api/v1/events/" not in body
    _assert_private_absent(body)


def test_occurrence_placeholder(settings, occurrence):
    settings.PUBLIC_EVENT_URL_TEMPLATE = "https://www.example.com/webinars/{slug}?session={occurrence_id}"
    expected = f"https://www.example.com/webinars/public-webinar?session={occurrence.pk}"
    assert _prop(build_ics(occurrence), "URL") == expected
    # Without an occurrence the template can't be filled: fall back to the API URL.
    assert calendar_links.public_event_url(occurrence.event) == "https://webinars.example.com/api/v1/events/public-webinar"


def test_ics_endpoint_uses_template(settings, client, occurrence):
    settings.PUBLIC_EVENT_URL_TEMPLATE = TEMPLATE
    resp = client.get(f"/api/v1/occurrences/{occurrence.id}/calendar.ics")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert _prop(body, "URL") == "https://www.example.com/webinars/public-webinar"
    _assert_private_absent(body)


def test_invalid_template_falls_back(settings, occurrence):
    settings.PUBLIC_EVENT_URL_TEMPLATE = "https://{slug}.evil.example/"
    body = build_ics(occurrence)
    assert _prop(body, "URL") == "https://webinars.example.com/api/v1/events/public-webinar"
    assert "evil" not in body


def test_slug_values_are_percent_encoded(settings, form):
    # SlugField isn't enforced on direct writes; values must still stay inside the path.
    settings.PUBLIC_EVENT_URL_TEMPLATE = TEMPLATE
    event = make_event(form, slug="a/b?c#d e")
    occ = make_occurrence(event)
    url = calendar_links.public_event_url(event, occ)
    assert url == "https://www.example.com/webinars/a%2Fb%3Fc%23d%20e"
    assert urlsplit(url).netloc == "www.example.com"
    assert _prop(build_ics(occ), "URL") == url


def test_private_links_never_reachable_via_template(settings, form):
    settings.PUBLIC_EVENT_URL_TEMPLATE = "https://www.example.com/webinars/{slug}/{occurrence_id}"
    event = make_event(form, slug="room-event")
    occ = make_occurrence(event, waiting_room_enabled=True, waiting_room_code="roomcode12345")
    body = build_ics(occ)
    _assert_private_absent(body, occ)
    _assert_private_absent(str(links_for(occ)), occ)


# ---------------------------------------------------------------------------
# ICS injection
# ---------------------------------------------------------------------------


def test_ics_text_injection_is_neutralised(settings, form):
    settings.PUBLIC_EVENT_URL_TEMPLATE = TEMPLATE
    event = make_event(
        form,
        slug="inject",
        public_title="Title\r\nATTENDEE:mailto:evil@example.net\nORGANIZER:x;y,z",
        summary="",
    )
    # PostgreSQL rejects NUL on storage; exercise serializer defence in memory.
    event.summary = "Line one\rEND:VEVENT\r\nBEGIN:VALARM\x00\x0b\x1b\x7f\x85 done"
    occ = make_occurrence(event, ics_uid="uid\r\nX-EVIL:1@webinars.test")
    body = build_ics(occ)
    lines = _unfold(body).split("\r\n")
    assert "\n" not in body.replace("\r\n", "")
    assert "\r" not in body.replace("\r\n", "")
    assert not any(line.startswith(("ATTENDEE", "ORGANIZER", "X-EVIL", "BEGIN:VALARM")) for line in lines)
    assert lines.count("END:VEVENT") == 1 and lines.count("BEGIN:VEVENT") == 1
    assert _prop(body, "SUMMARY") == "Title\\nATTENDEE:mailto:evil@example.net\\nORGANIZER:x\\;y\\,z"
    description = _prop(body, "DESCRIPTION")
    assert "END:VEVENT\\nBEGIN:VALARM done" in description
    assert not any(c in body for c in "\x00\x0b\x1b\x7f\x85")
    assert _prop(body, "UID") == "uid\\nX-EVIL:1@webinars.test"


def test_ics_lines_folded_to_75_octets(settings, form):
    settings.PUBLIC_EVENT_URL_TEMPLATE = TEMPLATE
    event = make_event(form, slug="long", public_title="é" * 200, summary="ü" * 2000)
    body = build_ics(make_occurrence(event))
    assert all(len(line.encode("utf-8")) <= 75 for line in body.split("\r\n"))
    # Summary is bounded so the description (and web links) stay a sane size.
    assert len(_prop(body, "DESCRIPTION")) < 2 * calendar_links.MAX_DETAILS_LENGTH


# ---------------------------------------------------------------------------
# Google / Outlook links
# ---------------------------------------------------------------------------


def test_web_calendar_links(settings, occurrence):
    settings.PUBLIC_EVENT_URL_TEMPLATE = TEMPLATE
    links = links_for(occurrence)
    assert links["ics"] == f"https://webinars.example.com/api/v1/occurrences/{occurrence.pk}/calendar.ics"
    page = "https://www.example.com/webinars/public-webinar"
    start = occurrence.starts_at.strftime("%Y%m%dT%H%M%SZ")
    end = occurrence.ends_at.strftime("%Y%m%dT%H%M%SZ")

    google = urlsplit(links["google"])
    assert (google.scheme, google.netloc, google.path) == ("https", "calendar.google.com", "/calendar/render")
    q = parse_qs(google.query)
    assert q["action"] == ["TEMPLATE"]
    assert q["text"] == [occurrence.event.public_title]
    assert q["dates"] == [f"{start}/{end}"]
    assert q["ctz"] == ["Europe/London"]
    assert q["details"][0].endswith(page) and q["details"][0].startswith("A public summary.")

    for key, host in (("outlook", "outlook.live.com"), ("office365", "outlook.office.com")):
        parts = urlsplit(links[key])
        assert (parts.scheme, parts.netloc, parts.path) == ("https", host, "/calendar/0/deeplink/compose")
        q = parse_qs(parts.query)
        assert q["rru"] == ["addevent"] and q["path"] == ["/calendar/action/compose"]
        assert q["subject"] == [occurrence.event.public_title]
        assert q["startdt"] == [occurrence.starts_at.strftime("%Y-%m-%dT%H:%M:%SZ")]
        assert q["enddt"] == [occurrence.ends_at.strftime("%Y-%m-%dT%H:%M:%SZ")]
        assert page in q["body"][0]

    _assert_private_absent(str(links), occurrence)


def test_web_links_escape_reserved_characters(settings, form):
    settings.PUBLIC_EVENT_URL_TEMPLATE = TEMPLATE
    event = make_event(form, slug="amp", public_title="Q&A #1 + 50% off?\r\nx=y")
    links = links_for(make_occurrence(event))
    for key, field in (("google", "text"), ("outlook", "subject"), ("office365", "subject")):
        parts = urlsplit(links[key])
        assert not parts.fragment
        assert parse_qs(parts.query)[field] == ["Q&A #1 + 50% off?\nx=y"]
        assert "\r" not in links[key] and "\n" not in links[key] and " " not in links[key]


def test_cancelled_occurrence_has_ics_only(settings, occurrence):
    settings.PUBLIC_EVENT_URL_TEMPLATE = TEMPLATE
    cancel_occurrence(occurrence)
    occurrence.refresh_from_db()
    links = links_for(occurrence)
    assert links["ics"].endswith(f"/api/v1/occurrences/{occurrence.pk}/calendar.ics")
    assert links["google"] is None and links["outlook"] is None and links["office365"] is None


def test_ended_occurrence_has_ics_only(settings, form):
    occ = make_occurrence(make_event(form, slug="past"), starts_at=timezone.now() - timedelta(days=2))
    links = links_for(occ)
    assert links["ics"] and links["google"] is None and links["outlook"] is None


def test_invalid_timezone_omits_ctz(settings, form):
    occ = make_occurrence(make_event(form, slug="tz"))
    occ.timezone = "Europe/London&x=1"
    q = parse_qs(urlsplit(links_for(occ)["google"]).query)
    assert "ctz" not in q and "x" not in q


def test_unset_template_links_use_api_url(settings, occurrence):
    settings.PUBLIC_EVENT_URL_TEMPLATE = ""
    q = parse_qs(urlsplit(links_for(occurrence)["google"]).query)
    assert q["details"][0].endswith("https://webinars.example.com/api/v1/events/public-webinar")
