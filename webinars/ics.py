"""RFC 5545 calendar generation with stable UID / SEQUENCE.

Only public information is included: private join / waiting-room links are
never written to calendar files. The event URL comes from
``calendar_links.public_event_url`` (``PUBLIC_EVENT_URL_TEMPLATE`` or the JSON API).
"""
import re
from datetime import timezone as dt_timezone

from webinars.calendar_links import event_details, public_event_url

PRODID = "-//Webinar Platform//Webinar Calendar 1.0//EN"

# RFC 5545 TEXT excludes control characters other than HTAB; newlines are escaped.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# A URI property value: printable ASCII only, never a line break or quote.
_URI = re.compile(r"https?://[\x21-\x7e]+")


def escape_text(value):
    """Escape a TEXT property value (RFC 5545 section 3.3.11)."""
    value = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL.sub("", value)
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_line(line):
    """Fold a content line to at most 75 octets per physical line, UTF-8 safe."""
    out = []
    current = ""
    current_len = 0
    for ch in line:
        size = len(ch.encode("utf-8"))
        if current_len + size > 75:
            out.append(current)
            current = " " + ch
            current_len = 1 + size
        else:
            current += ch
            current_len += size
    out.append(current)
    return "\r\n".join(out)


def _utc(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_public_url(event, occurrence=None):
    return public_event_url(event, occurrence)


def build_ics(occurrence):
    """Return a VCALENDAR string (CRLF line endings) for ``occurrence``."""
    event = occurrence.event
    cancelled = occurrence.status == occurrence.Status.CANCELLED
    url = event_public_url(event, occurrence)
    description = event_details(event, url)
    dtstamp = occurrence.updated_at or occurrence.created_at

    lines = [
        "BEGIN:VCALENDAR",
        f"PRODID:{PRODID}",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"METHOD:{'CANCEL' if cancelled else 'PUBLISH'}",
        "BEGIN:VEVENT",
        f"UID:{escape_text(occurrence.ics_uid)}",
        f"SEQUENCE:{int(occurrence.ics_sequence)}",
        f"DTSTAMP:{_utc(dtstamp)}",
        f"DTSTART:{_utc(occurrence.starts_at)}",
        f"DTEND:{_utc(occurrence.ends_at)}",
        f"SUMMARY:{escape_text(event.public_title)}",
        f"DESCRIPTION:{escape_text(description)}",
    ]
    if _URI.fullmatch(url) and '"' not in url:
        lines.append(f"URL:{url}")
    lines += [
        f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
        "TRANSP:OPAQUE",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"
