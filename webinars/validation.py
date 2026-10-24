"""URL validators for admin-entered URLs (thank-you redirects, media/stream links)."""
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError

_MAX_URL_LENGTH = 1000
MAX_CTA_LABEL = 60


def validate_cta_label(value):
    """Button text: one line of plain text, at most MAX_CTA_LABEL characters, no markup."""
    if not value:
        return
    if len(value) > MAX_CTA_LABEL:
        raise ValidationError(f"Keep button text to {MAX_CTA_LABEL} characters or fewer.", code="too_long")
    if any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 or c in "  " for c in value):
        raise ValidationError("Button text must be a single line without control characters.", code="invalid")
    if any(c in "<>`" for c in value):
        raise ValidationError("Button text can't contain <, > or `.", code="invalid")


def _parse_https(url):
    """Parse ``url`` and enforce https, a host, no userinfo and the default port."""
    if not isinstance(url, str) or not url:
        raise ValidationError("A URL is required.", code="required")
    if len(url) > _MAX_URL_LENGTH:
        raise ValidationError("URL is too long.", code="too_long")
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url) or "\\" in url:
        raise ValidationError("URL contains invalid characters.", code="invalid")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValidationError("URL is malformed.", code="invalid")
    if parts.scheme.lower() != "https":
        raise ValidationError("Only https URLs are allowed.", code="scheme")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise ValidationError("URLs must not contain credentials.", code="userinfo")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise ValidationError("URL must include a host.", code="host")
    if port is not None and port != 443:
        raise ValidationError("Non-default ports are not allowed.", code="port")
    return host


def validate_public_https_url(url):
    """Validate a public https URL (hero images, media, stream and recording links)."""
    _parse_https(url)


def validate_thank_you_url(url):
    """Validate a thank-you redirect: https and host on ``THANK_YOU_REDIRECT_HOSTS``."""
    host = _parse_https(url)
    allowed = {
        str(h).strip().rstrip(".").lower()
        for h in (getattr(settings, "THANK_YOU_REDIRECT_HOSTS", None) or [])
        if str(h).strip()
    }
    if host not in allowed:
        raise ValidationError("Redirect host is not on the allowlist.", code="host_not_allowed")
