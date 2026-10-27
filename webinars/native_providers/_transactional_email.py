"""Shared rendering for transactional attendee email destinations (Resend, Amazon SES).

One message per outbox delivery, rendered from built-in plain-text templates (or the operator's
overrides) with a strict placeholder allowlist. The HTML part is generated from the text
(paragraphs, line breaks and auto-linked https URLs); operators never supply HTML.

Link rules (``join_link``): the managed room URL when the occurrence's waiting room is on
(``outbox.occurrence_links`` then never includes a provider link, whatever the destination
allows); else the provider ``joinUrl``, which is only in the re-filtered body when the
destination has ``include_private_links``; else the public event URL. Every link is checked to
be a public https URL before it is rendered; anything else falls back to the event URL.

Recipients: only ``identity.verified`` registrations (``native_integrations._facts``) with an
email in the profile or identity group. Cancelled registrations are skipped except for
``occurrence.cancelled``. Nothing here reads secrets.
"""
import datetime
import html
import re
from string import Formatter
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError

from webinars import native_integrations as ni
from webinars.validation import validate_public_https_url

EVENT_TYPES = (
    "registration.created",
    "reminder.due",
    "occurrence.rescheduled",
    "occurrence.cancelled",
    "attendance.recorded",
    "recording.published",
    "follow_up.requested",
)
# Profile is optional (first name only); identity carries the fallback email.
REQUIRED_GROUPS = ("identity", "registration", "event")

# Message keys: one template (and one optional subject/body override pair) each.
MESSAGE_KEYS = (
    "registration_created",
    "reminder_due",
    "occurrence_rescheduled",
    "occurrence_cancelled",
    "attendance_attended",
    "attendance_no_show",
    "recording_published",
    "follow_up_requested",
)
MESSAGE_LABELS = {
    "registration_created": "confirmation",
    "reminder_due": "reminder",
    "occurrence_rescheduled": "new time",
    "occurrence_cancelled": "cancellation",
    "attendance_attended": "thank-you (attended)",
    "attendance_no_show": "sorry we missed you (no-show)",
    "recording_published": "recording available",
    "follow_up_requested": "follow-up",
}
PLACEHOLDERS = (
    "first_name",
    "event_title",
    "starts_at_local",
    "timezone",
    "duration_minutes",
    "join_link",
    "calendar_link",
    "recording_link",
    "event_url",
)
# ``recording_link`` exists only for the recording message; elsewhere it is a validation error.
RECORDING_ONLY = frozenset({"recording_link"})

TEMPLATES = {
    "registration_created": (
        "You're registered: {event_title}",
        "Hi {first_name},\n\n"
        "You're registered for {event_title}.\n\n"
        "When: {starts_at_local} ({timezone}).\n\n"
        "Join link: {join_link}\n\n"
        "Add it to your calendar: {calendar_link}\n\n"
        "Event details: {event_url}",
    ),
    "reminder_due": (
        "Reminder: {event_title} starts {starts_at_local}",
        "Hi {first_name},\n\n"
        "{event_title} starts {starts_at_local} ({timezone}).\n\n"
        "Join link: {join_link}\n\n"
        "Event details: {event_url}",
    ),
    "occurrence_rescheduled": (
        "New time for {event_title}",
        "Hi {first_name},\n\n"
        "{event_title} has moved to {starts_at_local} ({timezone}). Your registration still stands.\n\n"
        "Join link: {join_link}\n\n"
        "Update your calendar: {calendar_link}\n\n"
        "Event details: {event_url}",
    ),
    "occurrence_cancelled": (
        "Cancelled: {event_title}",
        "Hi {first_name},\n\n"
        "The session of {event_title} planned for {starts_at_local} ({timezone}) has been cancelled. "
        "We're sorry for the change.\n\n"
        "Event details: {event_url}",
    ),
    "attendance_attended": (
        "Thanks for joining {event_title}",
        "Hi {first_name},\n\n"
        "Thank you for joining {event_title}.\n\n"
        "Event details: {event_url}",
    ),
    "attendance_no_show": (
        "Sorry we missed you at {event_title}",
        "Hi {first_name},\n\n"
        "We missed you at {event_title}. You can find more information here:\n\n"
        "{event_url}",
    ),
    "recording_published": (
        "Recording available: {event_title}",
        "Hi {first_name},\n\n"
        "The recording of {event_title} is now available:\n\n"
        "{recording_link}\n\n"
        "Event details: {event_url}",
    ),
    "follow_up_requested": (
        "Following up on {event_title}",
        "Hi {first_name},\n\n"
        "Thank you for your interest in {event_title}. Reply to this email if you have any questions.\n\n"
        "Event details: {event_url}",
    ),
}

MAX_SUBJECT_CHARS = 200
MAX_SUBJECT_PREFIX_CHARS = 40
MAX_BODY_CHARS = 4000
MAX_FROM_NAME_CHARS = 80
MAX_FIRST_NAME_CHARS = 100
MAX_TITLE_CHARS = 300
# Our own bound on the rendered message (subject + text + html as UTF-8), checked before any call.
MAX_MESSAGE_BYTES = 150_000
# Our own bound on the serialized request body (``_encode``), checked before any call.
MAX_REQUEST_BYTES = 200_000

DELIVERY_ID_HEADER = "X-Webinar-Delivery-Id"
EVENT_TYPE_HEADER = "X-Webinar-Event-Type"

# Plain addr-spec: no display name, comments, quotes or non-ASCII (vendor encoding is unverified).
EMAIL = re.compile(r"^(?=.{6,254}$)[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
# Display name: ASCII letters, digits, spaces and a few punctuation marks; no quotes, brackets or control characters.
FROM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,'&()-]{0,%d}$" % (MAX_FROM_NAME_CHARS - 1))
SUBJECT_PREFIX = re.compile(r"^[\x20-\x7a\x7c\x7e]{1,%d}$" % MAX_SUBJECT_PREFIX_CHARS)  # printable ASCII without { }
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"\s+")
_URL_IN_TEXT = re.compile(r"https://[^\s<>\"']+")
_LINK_TRAIL = ".,;:!?)"
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

CONFIRMED = ni.CONFIRMED
SEND_ACK = ni.Field(
    "send_ack",
    "Transactional email confirmation",
    "Confirm that registrants expect these confirmation, reminder, change and follow-up emails about the session "
    "they registered for, and that the Required consent settings reflect your policy. No marketing content is sent.",
    choices=(("", "Not confirmed"), (CONFIRMED, "Confirmed by the operator")),
)


def _override_fields():
    fields = []
    for key in MESSAGE_KEYS:
        label = MESSAGE_LABELS[key]
        fields.append(
            ni.Field(
                f"subject_{key}",
                f"Subject: {label}",
                f"Optional subject for the {label} email (one line, up to {MAX_SUBJECT_CHARS} characters). "
                "Placeholders: " + ", ".join("{" + p + "}" for p in PLACEHOLDERS if p not in RECORDING_ONLY or key == "recording_published") + ".",
                required=False,
            )
        )
    for key in MESSAGE_KEYS:
        label = MESSAGE_LABELS[key]
        fields.append(
            ni.Field(
                f"body_{key}",
                f"Body: {label}",
                f"Optional plain-text body for the {label} email (up to {MAX_BODY_CHARS} characters; blank lines separate "
                "paragraphs; https links become clickable). Same placeholders as the subject. No HTML.",
                required=False,
                multiline=True,
            )
        )
    return tuple(fields)


# Shared operator settings; a provider puts these after its region field and before its secrets.
FIELDS = (
    ni.Field("from_address", "From address", "Verified sender address at the provider, e.g. events@example.com.", pattern=EMAIL),
    ni.Field("from_name", "From name", "Optional display name, e.g. Example Events (letters, digits, spaces, . , ' & ( ) -).", required=False, pattern=FROM_NAME),
    ni.Field("reply_to", "Reply-to address", "Optional address replies go to.", required=False, pattern=EMAIL),
    ni.Field("subject_prefix", "Subject prefix", "Optional text put before every subject, e.g. [Example Events].", required=False, pattern=SUBJECT_PREFIX),
    *_override_fields(),
    SEND_ACK,
)


def placeholders_for(key):
    return tuple(p for p in PLACEHOLDERS if p not in RECORDING_ONLY or key == "recording_published")


def template_problem(template, key, *, subject):
    """Why ``template`` may not be used for message ``key``, else None (pure string checks)."""
    if not isinstance(template, str):
        return "format"
    limit = MAX_SUBJECT_CHARS if subject else MAX_BODY_CHARS
    if not template.strip():
        return "empty"
    if len(template) > limit:
        return "too_long"
    if _CONTROL.search(template) or (subject and ("\n" in template or "\r" in template or "\t" in template)):
        return "control_characters"
    allowed = placeholders_for(key)
    try:
        parsed = list(Formatter().parse(template))
    except ValueError:
        return "braces"
    used = []
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if field not in allowed or spec or conversion is not None:
            return "placeholder"
        used.append(field)
    return None


TEMPLATE_MESSAGES = {
    "format": "Enter plain text.",
    "empty": "Enter text, or leave the field empty to use the built-in message.",
    "too_long": "Too long: subjects may have %d characters and bodies %d." % (MAX_SUBJECT_CHARS, MAX_BODY_CHARS),
    "control_characters": "Subjects are one line; bodies may use line breaks but no other control characters.",
    "braces": "Unmatched { or }. Write {{ or }} for a literal brace.",
    "placeholder": "Unknown placeholder. Use only: " + ", ".join("{" + p + "}" for p in PLACEHOLDERS) + " ({recording_link} only in the recording message), without formats.",
}


def validate_extra(cfg, credentials, add, **kw):
    """Operator templates and the send confirmation (field patterns cover the addresses)."""
    for key in MESSAGE_KEYS:
        for kind, subject in (("subject", True), ("body", False)):
            value = cfg.get(f"{kind}_{key}")
            if not value:
                continue
            problem = template_problem(value.replace("\r\n", "\n").replace("\r", "\n"), key, subject=subject)
            if problem:
                add(f"native-{kind}_{key}", TEMPLATE_MESSAGES[problem])
    if cfg.get("send_ack") != CONFIRMED:
        add("native-send_ack", "Confirm that registrants expect these transactional emails before enabling.")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _clean_text(value, limit):
    """One line of plain text without control characters, collapsed whitespace, at most ``limit`` chars."""
    if not isinstance(value, str):
        return ""
    value = _WS.sub(" ", _CONTROL.sub("", value.replace("\r", " ").replace("\n", " "))).strip()
    return value[:limit]


def _public_url(value):
    """``value`` when it is a public https URL (as the platform validates links), else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        validate_public_https_url(value)
    except ValidationError:
        return None
    return value if "\n" not in value and " " not in value else None


def _base_url():
    return (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")


def event_url(event):
    """Public page for the event: ``PUBLIC_EVENT_URL_TEMPLATE`` when set, else the JSON event API URL."""
    from webinars.calendar_links import public_event_url

    slug = event.get("slug") if isinstance(event.get("slug"), str) else ""
    occ = event.get("occurrence") if isinstance(event.get("occurrence"), dict) else {}
    occurrence = SimpleNamespace(pk=str(occ.get("id"))) if occ.get("id") else None
    if not slug:
        return _base_url() + "/"
    return public_event_url(SimpleNamespace(slug=slug), occurrence)


def calendar_url(occ):
    return f"{_base_url()}/api/v1/occurrences/{occ.get('id')}/calendar.ics" if occ.get("id") else ""


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.timezone.utc)


def _zone(name):
    if isinstance(name, str) and 0 < len(name) <= 64 and name == name.strip() and not name.startswith("/") and ".." not in name:
        try:
            return ZoneInfo(name)
        except Exception:  # ZoneInfoNotFoundError, ValueError, OSError
            return None
    return None


def format_local(starts_at, tz_name):
    """``Tue 3 Nov 2026, 14:00 GMT`` in the occurrence zone (UTC when the zone is unknown); None if unparsable."""
    dt = _parse_iso(starts_at)
    if dt is None:
        return None
    local = dt.astimezone(_zone(tz_name) or datetime.timezone.utc)
    abbreviation = _clean_text(local.tzname() or "UTC", 16) or "UTC"
    return f"{_DAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]} {local.year}, {local:%H:%M} {abbreviation}"


def _duration_minutes(occ):
    start, end = _parse_iso(occ.get("startsAt")), _parse_iso(occ.get("endsAt"))
    if start is None or end is None or end <= start:
        return ""
    return str(int((end - start).total_seconds() // 60))


def message_key(event_type, facts):
    """Template key for ``event_type``; ``Skip`` for events this module does not send."""
    if event_type == "attendance.recorded":
        if facts["attendance"] == "attended":
            return "attendance_attended"
        if facts["attendance"] == "no_show":
            return "attendance_no_show"
        raise ni.Skip("native_attendance_unknown")
    if event_type not in EVENT_TYPES:
        raise ni.Skip("native_event_unsupported")
    return event_type.replace(".", "_")


def join_link(occ, public_event_url):
    """Managed room URL first (never a provider link when the room exists), else the private join
    link the re-filtered body still carries, else the public event page."""
    from webinars.outbox import MANAGED_ROOM_KEY  # lazy: outbox loads the models

    return _public_url(occ.get(MANAGED_ROOM_KEY)) or _public_url(occ.get("joinUrl")) or public_event_url


def recipient(facts, body):
    identity = body.get("identity") if isinstance(body.get("identity"), dict) else {}
    for candidate in (facts["email"], identity.get("email")):
        if isinstance(candidate, str) and EMAIL.match(candidate):
            return candidate
    raise ni.Skip("native_no_email")


def values_for(key, facts, event_type, body):
    """Placeholder values (plain text, one line each). Raises ``Skip`` when a required link is missing."""
    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    occ = event.get("occurrence") if isinstance(event.get("occurrence"), dict) else {}
    public = event_url(event)
    values = {
        "first_name": _clean_text(facts["first_name"], MAX_FIRST_NAME_CHARS) or "there",
        "event_title": _clean_text(event.get("title"), MAX_TITLE_CHARS) or "your webinar",
        "starts_at_local": format_local(occ.get("startsAt"), occ.get("timezone")) or "the scheduled time",
        "timezone": _clean_text(occ.get("timezone"), 64) or "UTC",
        "duration_minutes": _duration_minutes(occ),
        "join_link": join_link(occ, public),
        "calendar_link": calendar_url(occ) or public,
        "event_url": public,
    }
    if key == "recording_published":
        link = _public_url(occ.get("recordingUrl"))
        if not link:
            raise ni.Skip("native_recording_missing")
        values["recording_link"] = link
    return values


def to_html(text):
    """HTML part from the rendered text: escaped paragraphs, ``<br>`` line breaks, auto-linked https URLs."""
    parts = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        escaped = html.escape(paragraph, quote=True)
        linked = _URL_IN_TEXT.sub(_anchor, escaped)
        parts.append("<p>" + linked.replace("\n", "<br>\n") + "</p>")
    return "<!DOCTYPE html>\n<html><body>\n" + "\n".join(parts) + "\n</body></html>\n"


def _anchor(match):
    url = match.group(0)
    trail = ""
    while url and url[-1] in _LINK_TRAIL:
        trail = url[-1] + trail
        url = url[:-1]
    return f'<a href="{url}">{url}</a>{trail}'


def render(cfg, facts, event_type, body):
    """``{"key", "to", "subject", "text", "html"}`` for one delivery; raises ``Skip`` / ``_Stop``."""
    if cfg.get("send_ack") != CONFIRMED:
        raise ni._Stop(ni.Outcome("dead", "native_match_strategy_unconfirmed"))
    ni._require_delivery_id(facts)
    key = message_key(event_type, facts)
    registration = body.get("registration") if isinstance(body.get("registration"), dict) else {}
    if registration.get("status") == "cancelled" and event_type != "occurrence.cancelled":
        raise ni.Skip("native_registration_cancelled")
    to = recipient(facts, body)
    subject_template = (cfg.get(f"subject_{key}") or "").strip() or TEMPLATES[key][0]
    body_template = (cfg.get(f"body_{key}") or "").replace("\r\n", "\n").replace("\r", "\n").strip() or TEMPLATES[key][1]
    if template_problem(subject_template, key, subject=True) or template_problem(body_template, key, subject=False):
        raise ni._Stop(ni.Outcome("dead", "native_config_invalid"))
    values = values_for(key, facts, event_type, body)
    subject = _clean_text(subject_template.format(**values), 998)
    prefix = cfg.get("subject_prefix") or ""
    if prefix and SUBJECT_PREFIX.match(prefix):
        subject = _clean_text(f"{prefix} {subject}", 998)
    text = body_template.format(**values)
    rendered = {"key": key, "to": to, "subject": subject, "text": text, "html": to_html(text)}
    if len((subject + text + rendered["html"]).encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ni._Stop(ni.Outcome("dead", "send: payload_too_large"))
    return rendered


def from_header(cfg):
    """``Name <address>`` or the bare address; the fields are pattern-validated, so no injection."""
    address, name = cfg.get("from_address") or "", cfg.get("from_name") or ""
    if not EMAIL.match(address):
        raise ni._Stop(ni.Outcome("dead", "native_config_incomplete"))
    if name and FROM_NAME.match(name):
        return f"{name} <{address}>"
    return address


def reply_to(cfg):
    value = cfg.get("reply_to") or ""
    return value if EMAIL.match(value) else None


def tracking_headers(facts, event_type):
    """Custom headers carried by every message (values are ids and event names: printable ASCII)."""
    return {DELIVERY_ID_HEADER: facts["delivery_id"], EVENT_TYPE_HEADER: event_type}


def require_request_size(step, payload):
    ni._require_size(step, payload, MAX_REQUEST_BYTES)
