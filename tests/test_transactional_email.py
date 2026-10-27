"""Shared transactional-email rendering (webinars/native_providers/_transactional_email.py) and the
registry extension (``EVENT_TYPES`` / ``REQUIRED_GROUPS`` per provider module).

No network: rendering is pure; the registry tests use ``native_integrations.validate`` only.
"""
import copy
import socket

import pytest

from tests.test_native_integrations import body
from webinars import native_integrations as ni
from webinars.native_providers import _transactional_email as tx
from webinars.models import Destination

pytestmark = pytest.mark.django_db

EMAIL_PROVIDERS = ("resend", "ses")
BASE_URL = "https://webinars.example.com"
CFG = {
    "region": "global",
    "from_address": "events@example.com",
    "from_name": "Example Events",
    "reply_to": "",
    "subject_prefix": "",
    "send_ack": "confirmed",
    "processing_ack": "confirmed",
}
RESEND = {"native_provider": "resend", "native_region": "global", "native_api_key": "re_secretApiKey_123456", "native_from_address": "events@example.com", "native_send_ack": "confirmed", "native_processing_ack": "confirmed"}
BREVO = {"native_provider": "brevo", "native_region": "global", "native_api_key": "xkeysib-abcdef123456", "native_contact_sync": "events", "native_email_merge_ack": "confirmed", "native_processing_ack": "confirmed"}
ALL_GROUPS = list(Destination.GROUPS)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def public_urls(settings):
    settings.PUBLIC_BASE_URL = BASE_URL
    settings.PUBLIC_EVENT_URL_TEMPLATE = ""
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.resend.com", "api.brevo.com", "email.eu-west-2.amazonaws.com"]


def mail_body(**overrides):
    b = body()
    b["event"]["slug"] = "sample-webinar"
    b["event"]["occurrence"].update({"startsAt": "2026-11-03T14:00:00Z", "endsAt": "2026-11-03T15:00:00Z", "timezone": "Europe/London"})
    for key, value in overrides.items():
        b[key] = value
    return b


def render(event_type="registration.created", b=None, cfg=None, attendance=None):
    b = b if b is not None else mail_body()
    if attendance:
        b["registration"]["attendance"] = attendance
    facts = ni._facts(b, event_type, None)
    return tx.render({**CFG, **(cfg or {})}, facts, event_type, b)


# --- registry extension ---------------------------------------------------------


def test_defaults_unchanged_for_every_existing_provider():
    for provider, _label in ni.PROVIDER_CHOICES:
        if not provider or provider in EMAIL_PROVIDERS:
            continue
        assert ni.supported_event_types(provider) == ni.NATIVE_EVENT_TYPES, provider
        assert ni.required_groups(provider) == ni.REQUIRED_GROUPS, provider
        module = ni.REGISTRY.get(provider)
        assert module is None or not hasattr(module, "EVENT_TYPES"), provider
    assert ni.supported_event_types("") == ni.NATIVE_EVENT_TYPES and ni.required_groups("nope") == ni.REQUIRED_GROUPS


@pytest.mark.parametrize("provider", EMAIL_PROVIDERS)
def test_email_providers_declare_their_own_events_and_groups(provider):
    assert ni.supported_event_types(provider) == tx.EVENT_TYPES
    assert ni.required_groups(provider) == ("identity", "registration", "event")
    assert "profile" not in ni.required_groups(provider)
    assert set(tx.EVENT_TYPES) == {"registration.created", "reminder.due", "occurrence.rescheduled", "occurrence.cancelled", "attendance.recorded", "recording.published", "follow_up.requested"}
    assert "registration.updated" not in tx.EVENT_TYPES and "attendance.checked_in" not in tx.EVENT_TYPES


def test_validate_uses_per_provider_event_types():
    kw = dict(allowed_groups=ALL_GROUPS, required_consents=[])
    assert "event_types" in ni.validate("brevo", BREVO, event_types=["reminder.due"], **kw)
    assert "event_types" not in ni.validate("brevo", BREVO, event_types=["registration.updated"], **kw)
    assert "event_types" not in ni.validate("resend", RESEND, event_types=list(tx.EVENT_TYPES), **kw)
    problems = ni.validate("resend", RESEND, event_types=["attendance.checked_in"], **kw)
    assert problems["event_types"] == ["Resend only supports: " + ", ".join(tx.EVENT_TYPES) + "."]


def test_validate_uses_per_provider_required_groups():
    kw = dict(event_types=["registration.created"], required_consents=[])
    assert "allowed_groups" not in ni.validate("resend", RESEND, allowed_groups=["identity", "registration", "event"], **kw)
    problems = ni.validate("resend", RESEND, allowed_groups=["identity", "event"], **kw)
    assert problems["allowed_groups"] == ["Resend needs these data groups: registration."]
    assert "allowed_groups" in ni.validate("brevo", BREVO, allowed_groups=["identity", "event"], **kw)


def test_check_send_policy_honours_module_event_types():
    reminder = Destination(event_types=["reminder.due"], allowed_groups=ALL_GROUPS, required_consents=[])
    assert ni.check_send_policy("resend", RESEND, reminder) is None
    assert ni.check_send_policy("brevo", BREVO, reminder) == "native_config_invalid"
    checked_in = Destination(event_types=["attendance.checked_in"], allowed_groups=ALL_GROUPS, required_consents=[])
    assert ni.check_send_policy("resend", RESEND, checked_in) == "native_config_invalid"
    assert ni.check_send_policy("brevo", BREVO, checked_in) is None


# --- templates and placeholders -------------------------------------------------


@pytest.mark.parametrize(
    "event_type,attendance,key,subject",
    [
        ("registration.created", None, "registration_created", "You're registered: Sample webinar"),
        ("reminder.due", None, "reminder_due", "Reminder: Sample webinar starts Tue 3 Nov 2026, 14:00 GMT"),
        ("occurrence.rescheduled", None, "occurrence_rescheduled", "New time for Sample webinar"),
        ("occurrence.cancelled", None, "occurrence_cancelled", "Cancelled: Sample webinar"),
        ("attendance.recorded", "attended", "attendance_attended", "Thanks for joining Sample webinar"),
        ("attendance.recorded", "no_show", "attendance_no_show", "Sorry we missed you at Sample webinar"),
        ("recording.published", None, "recording_published", "Recording available: Sample webinar"),
        ("follow_up.requested", None, "follow_up_requested", "Following up on Sample webinar"),
    ],
)
def test_builtin_template_per_event(event_type, attendance, key, subject):
    b = mail_body()
    if event_type == "recording.published":
        b["event"]["occurrence"]["recordingUrl"] = "https://videos.example.com/rec/1"
    message = render(event_type, b, attendance=attendance)
    assert message["key"] == key and message["subject"] == subject and message["to"] == "person@example.com"
    assert message["text"].startswith("Hi Sample,\n\n")
    assert "https://webinars.example.com/api/v1/events/sample-webinar" in message["text"]
    assert "{" not in message["text"] and "{" not in message["subject"]
    assert message["html"].startswith("<!DOCTYPE html>") and "<p>Hi Sample,</p>" in message["html"]


def test_every_message_key_has_a_template_and_override_fields():
    assert set(tx.TEMPLATES) == set(tx.MESSAGE_KEYS)
    names = {f.name for f in tx.FIELDS}
    for key in tx.MESSAGE_KEYS:
        assert f"subject_{key}" in names and f"body_{key}" in names
        assert tx.template_problem(tx.TEMPLATES[key][0], key, subject=True) is None
        assert tx.template_problem(tx.TEMPLATES[key][1], key, subject=False) is None
    assert all(f.multiline for f in tx.FIELDS if f.name.startswith("body_"))
    assert not any(f.multiline for f in tx.FIELDS if not f.name.startswith("body_"))


def test_unknown_attendance_and_unsupported_event_skipped():
    with pytest.raises(ni.Skip, match="native_attendance_unknown"):
        render("attendance.recorded", attendance="weird")
    with pytest.raises(ni.Skip, match="native_event_unsupported"):
        render("attendance.checked_in")
    with pytest.raises(ni.Skip, match="native_event_unsupported"):
        render("registration.updated")


def test_first_name_falls_back_to_there_and_identity_email():
    b = mail_body()
    b.pop("profile")
    message = render(b=b)
    assert message["text"].startswith("Hi there,") and message["to"] == "person@example.com"
    b = mail_body()
    b["profile"]["firstName"] = ""
    assert render(b=b)["text"].startswith("Hi there,")


def test_values_are_one_line_and_escaped_in_html():
    b = mail_body()
    b["event"]["title"] = 'Tom & <b>Jerry</b> "live"\r\nBcc: x@example.com'
    b["profile"]["firstName"] = "Sam\nple"
    message = render(b=b)
    assert "\n" not in message["subject"] and "Bcc:" in message["subject"]
    assert message["subject"] == "You're registered: Tom & <b>Jerry</b> \"live\" Bcc: x@example.com"
    assert "<b>" not in message["html"] and "&lt;b&gt;Jerry&lt;/b&gt;" in message["html"] and "&amp;" in message["html"]
    assert "Hi Sam ple," in message["text"]


def test_html_paragraphs_line_breaks_and_links():
    html = tx.to_html("One line\nsecond line\n\nSee https://example.com/a?b=1&c=2. Done")
    assert "<p>One line<br>\nsecond line</p>" in html
    assert '<a href="https://example.com/a?b=1&amp;c=2">https://example.com/a?b=1&amp;c=2</a>. Done' in html
    assert "<a " not in tx.to_html("plain http://insecure.example")  # only https links are anchored
    assert "<script" not in tx.to_html("<script>alert(1)</script>")


# --- link selection -------------------------------------------------------------


def test_managed_room_link_beats_provider_link():
    b = mail_body()
    b["event"]["occurrence"].update({"managedRoomUrl": f"{BASE_URL}/w/ROOMCODE/", "joinUrl": "https://zoom.example/j/PRIVATE"})
    message = render(b=b)
    assert f"Join link: {BASE_URL}/w/ROOMCODE/" in message["text"] and "PRIVATE" not in message["text"] and "PRIVATE" not in message["html"]


def test_provider_join_link_only_when_present_in_refiltered_body():
    b = mail_body()
    b["event"]["occurrence"]["joinUrl"] = "https://zoom.example/j/PRIVATE"
    assert "Join link: https://zoom.example/j/PRIVATE" in render(b=b)["text"]
    b = mail_body()
    b["event"]["occurrence"]["waitingRoomUrl"] = "https://zoom.example/w/WAIT"
    message = render(b=b)
    assert "WAIT" not in message["text"] and f"Join link: {BASE_URL}/api/v1/events/sample-webinar" in message["text"]


@pytest.mark.parametrize("link", ["http://zoom.example/j/x", "javascript:alert(1)", "https://zoom.example/j/x y", 42, ""])
def test_non_https_links_fall_back_to_the_event_url(link):
    b = mail_body()
    b["event"]["occurrence"]["joinUrl"] = link
    message = render(b=b)
    assert f"Join link: {BASE_URL}/api/v1/events/sample-webinar" in message["text"]
    assert "zoom.example" not in message["text"] and "javascript" not in message["html"]


def test_event_url_uses_public_template_when_configured(settings):
    settings.PUBLIC_EVENT_URL_TEMPLATE = "https://www.example.com/webinars/{slug}?o={occurrence_id}"
    message = render()
    assert "Event details: https://www.example.com/webinars/sample-webinar?o=00000000-0000-4000-8000-000000000004" in message["text"]


def test_calendar_link_is_the_public_ics_url():
    message = render()
    assert f"{BASE_URL}/api/v1/occurrences/00000000-0000-4000-8000-000000000004/calendar.ics" in message["text"]


def test_recording_link_required_for_recording_message():
    with pytest.raises(ni.Skip, match="native_recording_missing"):
        render("recording.published")
    b = mail_body()
    b["event"]["occurrence"]["recordingUrl"] = "http://videos.example.com/insecure"
    with pytest.raises(ni.Skip, match="native_recording_missing"):
        render("recording.published", b)
    b["event"]["occurrence"]["recordingUrl"] = "https://videos.example.com/rec/1"
    assert "https://videos.example.com/rec/1" in render("recording.published", b)["text"]


# --- local time ------------------------------------------------------------------


@pytest.mark.parametrize(
    "starts,tz,expected",
    [
        ("2026-11-03T14:00:00Z", "Europe/London", "Tue 3 Nov 2026, 14:00 GMT"),
        ("2026-07-01T14:00:00Z", "Europe/London", "Wed 1 Jul 2026, 15:00 BST"),
        ("2026-07-01T14:00:00Z", "America/New_York", "Wed 1 Jul 2026, 10:00 EDT"),
        ("2026-01-02T12:00:00Z", None, "Fri 2 Jan 2026, 12:00 UTC"),
        ("2026-01-02T12:00:00Z", "Not/AZone", "Fri 2 Jan 2026, 12:00 UTC"),
        ("2026-01-02T12:00:00+01:00", "UTC", "Fri 2 Jan 2026, 11:00 UTC"),
        ("not a date", "UTC", None),
        (None, "UTC", None),
    ],
)
def test_format_local(starts, tz, expected):
    assert tx.format_local(starts, tz) == expected


def test_unparsable_start_renders_a_safe_phrase_and_duration_is_computed_not_invented():
    b = mail_body()
    b["event"]["occurrence"]["startsAt"] = "soon"
    text = render(b=b, cfg={"body_registration_created": "{starts_at_local}|{duration_minutes}|{timezone}"})["text"]
    assert text == "the scheduled time||Europe/London"
    assert render(cfg={"body_registration_created": "{duration_minutes}"})["text"] == "60"


# --- recipients, status, consent of the registration -------------------------------


def test_cancelled_registration_skipped_except_for_cancellation_notice():
    b = mail_body()
    b["registration"]["status"] = "cancelled"
    with pytest.raises(ni.Skip, match="native_registration_cancelled"):
        render("reminder.due", b)
    assert render("occurrence.cancelled", b)["key"] == "occurrence_cancelled"


@pytest.mark.parametrize("email", [None, "", "not-an-email", "a@b", "x y@example.com", "héllo@example.com"])
def test_no_usable_email_is_skipped(email):
    b = mail_body()
    b["profile"]["email"] = email
    b["identity"]["email"] = email
    with pytest.raises(ni.Skip, match="native_no_email"):
        render(b=b)


def test_unverified_identity_never_rendered():
    b = mail_body()
    b["identity"]["verified"] = False
    with pytest.raises(ni.Skip, match="native_identity_unverified"):
        render(b=b)


def test_send_ack_and_delivery_id_required():
    with pytest.raises(ni._Stop) as stop:
        render(cfg={"send_ack": ""})
    assert stop.value.outcome.error == "native_match_strategy_unconfirmed"
    b = mail_body()
    b["delivery_metadata"] = {}
    with pytest.raises(ni._Stop) as stop:
        render(b=b)
    assert stop.value.outcome.error == "native_delivery_id_missing"


# --- operator overrides -----------------------------------------------------------


def test_subject_and_body_overrides_with_prefix():
    message = render(cfg={"subject_prefix": "[Example]", "subject_registration_created": "See you at {event_title}, {first_name}", "body_registration_created": "Hello {first_name}.\r\n\r\nJoin: {join_link}"})
    assert message["subject"] == "[Example] See you at Sample webinar, Sample"
    assert message["text"] == f"Hello Sample.\n\nJoin: {BASE_URL}/api/v1/events/sample-webinar"
    assert "<p>Hello Sample.</p>" in message["html"]


def test_overrides_apply_per_message_only():
    message = render("reminder.due", cfg={"subject_registration_created": "custom"})
    assert message["subject"].startswith("Reminder:")


@pytest.mark.parametrize(
    "template,subject,problem",
    [
        ("{nope}", True, "placeholder"),
        ("{first_name!r}", True, "placeholder"),
        ("{first_name:>10}", True, "placeholder"),
        ("{first_name.__class__}", True, "placeholder"),
        ("{0}", True, "placeholder"),
        ("{}", True, "placeholder"),
        ("{recording_link}", True, "placeholder"),
        ("unbalanced {", True, "braces"),
        ("unbalanced }", True, "braces"),
        ("two\nlines", True, "control_characters"),
        ("tab\there", True, "control_characters"),
        ("bell\x07", False, "control_characters"),
        ("x" * 201, True, "too_long"),
        ("x" * 4001, False, "too_long"),
        ("   ", True, "empty"),
        (42, True, "format"),
    ],
)
def test_template_problems(template, subject, problem):
    assert tx.template_problem(template, "registration_created", subject=subject) == problem


def test_template_allows_literal_braces_newlines_in_bodies_and_recording_link_for_recordings():
    assert tx.template_problem("Use {{braces}} and {first_name}", "reminder_due", subject=True) is None
    assert tx.template_problem("Line\n\nAnother {join_link}", "reminder_due", subject=False) is None
    assert tx.template_problem("{recording_link}", "recording_published", subject=False) is None
    assert render(cfg={"body_registration_created": "{{not a placeholder}} {first_name}"})["text"] == "{not a placeholder} Sample"


def test_validate_reports_bad_overrides_and_missing_send_ack():
    creds = {**RESEND, "native_subject_reminder_due": "{nope}", "native_body_occurrence_cancelled": "x" * 4001, "native_body_recording_published": "{recording_link}", "native_send_ack": ""}
    problems = ni.validate("resend", creds, event_types=["registration.created"], allowed_groups=ALL_GROUPS, required_consents=[])
    assert set(problems) == {"native-subject_reminder_due", "native-body_occurrence_cancelled", "native-send_ack"}
    assert "Unknown placeholder" in problems["native-subject_reminder_due"][0]
    assert ni.validate("resend", RESEND, event_types=["registration.created"], allowed_groups=ALL_GROUPS, required_consents=[]) == {}


def test_render_refuses_invalid_stored_override_without_sending():
    with pytest.raises(ni._Stop) as stop:
        render(cfg={"body_registration_created": "{secret}"})
    assert stop.value.outcome.error == "native_config_invalid"


def test_rendered_size_is_bounded(monkeypatch):
    monkeypatch.setattr(tx, "MAX_MESSAGE_BYTES", 100)
    with pytest.raises(ni._Stop) as stop:
        render()
    assert stop.value.outcome.error == "send: payload_too_large"


# --- addresses ------------------------------------------------------------------


def test_from_header_and_reply_to():
    assert tx.from_header(CFG) == "Example Events <events@example.com>"
    assert tx.from_header({**CFG, "from_name": ""}) == "events@example.com"
    assert tx.reply_to(CFG) is None and tx.reply_to({**CFG, "reply_to": "hello@example.com"}) == "hello@example.com"
    with pytest.raises(ni._Stop):
        tx.from_header({**CFG, "from_address": "Example <events@example.com>"})


@pytest.mark.parametrize("value", ["events@example.com", "a.b+c@sub.example.co.uk"])
def test_address_pattern_accepts_plain_addresses(value):
    assert tx.EMAIL.match(value)


@pytest.mark.parametrize("value", ["Example <events@example.com>", "events@example.com\nBcc: x@example.com", "events@localhost", "", "x" * 250 + "@example.com"])
def test_address_pattern_rejects_display_names_injection_and_junk(value):
    assert not tx.EMAIL.match(value)


@pytest.mark.parametrize("value", ["Example Events", "O'Brien & Co. (Events)"])
def test_from_name_accepted(value):
    assert tx.FROM_NAME.match(value)


@pytest.mark.parametrize("value", ['Evil "<x@example.com>', "Line\nbreak", "<script>", "é", ""])
def test_from_name_rejected(value):
    assert not tx.FROM_NAME.match(value)


def test_subject_prefix_cannot_carry_placeholders_or_newlines():
    assert tx.SUBJECT_PREFIX.match("[Example Events]")
    for bad in ("{event_title}", "two\nlines", "", "x" * 41):
        assert not tx.SUBJECT_PREFIX.match(bad)


def test_tracking_headers_carry_delivery_id_and_event_type():
    facts = ni._facts(mail_body(), "reminder.due", None)
    assert tx.tracking_headers(facts, "reminder.due") == {"X-Webinar-Delivery-Id": "00000000-0000-4000-8000-000000000005", "X-Webinar-Event-Type": "reminder.due"}


def test_render_does_not_mutate_the_body():
    b = mail_body()
    before = copy.deepcopy(b)
    render(b=b)
    assert b == before
