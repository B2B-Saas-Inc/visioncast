"""Public event page URLs and "add to calendar" links (ICS, Google, Outlook).

``PUBLIC_EVENT_URL_TEMPLATE`` points calendar entries at the marketing site's event
page instead of the JSON event API, e.g. ``https://www.example.com/webinars/{slug}``.

Template rules (``validate_template``):

* placeholders are ``{slug}`` and ``{occurrence_id}`` only; at least one is required;
  no ``{{`` escapes, attribute/index access, conversions or format specs;
* placeholders may appear only in the path, query or fragment, never in the scheme,
  host or port, so a template cannot point at another site per event;
* the literal text is printable ASCII URI characters;
* the rendered URL must pass ``validation.validate_public_https_url`` (https, host,
  no credentials, default port, at most 1000 characters).

Values are percent-encoded before substitution. An unset or invalid template falls
back to the JSON event API URL (the previous behaviour). Only public fields are used:
join, waiting-room and managed-room links are never read here.
"""
import logging
import re
from datetime import timezone as dt_timezone
from functools import lru_cache
from urllib.parse import quote, urlencode, urlsplit

from django.conf import settings
from django.core import checks
from django.core.exceptions import ValidationError
from django.utils import timezone

from webinars.validation import validate_public_https_url

logger = logging.getLogger(__name__)

PLACEHOLDERS = ("slug", "occurrence_id")
MAX_TEMPLATE_LENGTH = 500
MAX_TITLE_LENGTH = 300
MAX_DETAILS_LENGTH = 1000

GOOGLE_URL = "https://calendar.google.com/calendar/render"
OUTLOOK_URL = "https://outlook.live.com/calendar/0/deeplink/compose"
OFFICE365_URL = "https://outlook.office.com/calendar/0/deeplink/compose"

_TOKEN = re.compile(r"\{([^{}]*)\}")
# RFC 3986 unreserved + reserved + "%", plus the braces of placeholders.
_TEMPLATE_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%{}]*")
_TZ_NAME = re.compile(r"[A-Za-z0-9_+\-/]{1,64}")
_SAMPLE = {"slug": "sample-event", "occurrence_id": "00000000-0000-4000-8000-000000000000"}

_warned = set()


class TemplateError(ValueError):
    pass


def configured_template():
    return (getattr(settings, "PUBLIC_EVENT_URL_TEMPLATE", "") or "").strip()


def _substitute(template, values):
    return _TOKEN.sub(lambda m: quote(str(values[m.group(1)]), safe=""), template)


@lru_cache(maxsize=8)
def validate_template(template):
    """Return the frozenset of placeholder names used, or raise ``TemplateError``."""
    if not isinstance(template, str) or not template:
        raise TemplateError("The template is empty.")
    if len(template) > MAX_TEMPLATE_LENGTH:
        raise TemplateError(f"The template must be at most {MAX_TEMPLATE_LENGTH} characters.")
    if not _TEMPLATE_CHARS.fullmatch(template):
        raise TemplateError("The template contains characters that are not allowed in a URL.")
    names = [m.group(1) for m in _TOKEN.finditer(template)]
    unknown = sorted({n for n in names if n not in PLACEHOLDERS})
    if unknown:
        raise TemplateError("Unknown placeholder; use only {slug} and {occurrence_id}.")
    stripped = _TOKEN.sub("", template)
    if "{" in stripped or "}" in stripped:
        raise TemplateError("The template has an unmatched brace.")
    if not names:
        raise TemplateError("The template must contain {slug} or {occurrence_id}.")
    try:
        parts = urlsplit(template)
    except ValueError:
        raise TemplateError("The template is not a valid URL.") from None
    if "{" in parts.scheme or "{" in parts.netloc:
        raise TemplateError("Placeholders are allowed only in the path, query or fragment.")
    try:
        validate_public_https_url(_substitute(template, _SAMPLE))
    except ValidationError as exc:
        raise TemplateError(exc.messages[0]) from None
    return frozenset(names)


def _warn_once(template, message):
    if template not in _warned:
        _warned.add(template)
        logger.warning("PUBLIC_EVENT_URL_TEMPLATE ignored: %s", message)


def api_event_url(event):
    """The JSON event API URL used when no public page template is configured."""
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    return f"{base}/api/v1/events/{quote(event.slug, safe='')}"


def public_event_url(event, occurrence=None):
    """Public page URL for ``event`` (and ``occurrence`` when the template uses it)."""
    template = configured_template()
    if template:
        try:
            names = validate_template(template)
        except TemplateError as exc:
            _warn_once(template, str(exc))
        else:
            if "occurrence_id" not in names or occurrence is not None:
                url = _substitute(template, {"slug": event.slug, "occurrence_id": occurrence.pk if occurrence else ""})
                try:
                    validate_public_https_url(url)
                    return url
                except ValidationError:
                    pass
    return api_event_url(event)


def ics_path(occurrence):
    return f"/api/v1/occurrences/{occurrence.pk}/calendar.ics"


def ics_url(occurrence):
    return (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/") + ics_path(occurrence)


def _clean(value, limit):
    """Plain text without control characters (newlines kept), at most ``limit`` chars."""
    value = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(c for c in value if c in "\n\t" or not (ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0))
    value = value.strip()
    if limit <= 1:
        return ""
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def event_details(event, url):
    """Description text shared by ICS and web calendar links: summary plus public URL."""
    summary = _clean(event.summary, MAX_DETAILS_LENGTH - len(url) - 2)
    return "\n\n".join(part for part in [summary, url] if part)


def _compact(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _addable(occurrence):
    return occurrence.status == occurrence.Status.SCHEDULED and occurrence.ends_at > timezone.now()


def google_url(occurrence, title, details):
    params = {"action": "TEMPLATE", "text": title,
              "dates": f"{_compact(occurrence.starts_at)}/{_compact(occurrence.ends_at)}", "details": details}
    if _TZ_NAME.fullmatch(occurrence.timezone or ""):
        params["ctz"] = occurrence.timezone
    return f"{GOOGLE_URL}?{urlencode(params, quote_via=quote)}"


def _outlook_url(base, occurrence, title, details):
    params = {"path": "/calendar/action/compose", "rru": "addevent", "subject": title,
              "startdt": _iso(occurrence.starts_at), "enddt": _iso(occurrence.ends_at), "body": details}
    return f"{base}?{urlencode(params, quote_via=quote, safe='/')}"


def calendar_links(occurrence):
    """``{"ics", "google", "outlook", "office365"}`` for public projections.

    ``ics`` is always the absolute ICS URL (a cancelled occurrence's ICS carries
    METHOD:CANCEL). The web links are ``None`` unless the occurrence is scheduled
    and has not ended. Nothing private is included.
    """
    event = occurrence.event
    links = {"ics": ics_url(occurrence), "google": None, "outlook": None, "office365": None}
    if _addable(occurrence):
        title = _clean(event.public_title, MAX_TITLE_LENGTH)
        details = event_details(event, public_event_url(event, occurrence))
        links["google"] = google_url(occurrence, title, details)
        links["outlook"] = _outlook_url(OUTLOOK_URL, occurrence, title, details)
        links["office365"] = _outlook_url(OFFICE365_URL, occurrence, title, details)
    return links


def check_public_event_url_template(app_configs=None, **kwargs):
    """System check: an invalid configured template is an error (runtime falls back)."""
    template = configured_template()
    if not template:
        return []
    try:
        validate_template(template)
    except TemplateError as exc:
        return [checks.Error(f"PUBLIC_EVENT_URL_TEMPLATE is invalid: {exc}", id="webinars.E101",
                             hint="Example: https://www.example.com/webinars/{slug}")]
    return []
