"""Attribution cleaning: allowlisted parameters/identifiers, consent gating, URL stripping.

Attribution data is descriptive only and never used for authentication.
"""
import re
from urllib.parse import urlsplit, urlunsplit

MAX_VALUE_LENGTH = 256
MAX_URL_LENGTH = 2000

CLICK_ID_PARAMETERS = frozenset(
    ["gclid", "gbraid", "wbraid", "fbclid", "msclkid", "ttclid", "twclid", "ScCid", "rdt_cid"]
)
CAMPAIGN_PARAMETERS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
)
VALUETRACK_PARAMETERS = (
    "campaignid", "adgroupid", "keyword", "matchtype", "network", "device",
    "creative", "placement", "targetid", "loc_physical_ms",
)
ALLOWED_PARAMETERS = frozenset(CAMPAIGN_PARAMETERS) | frozenset(VALUETRACK_PARAMETERS) | CLICK_ID_PARAMETERS

# Accepted input key -> canonical camelCase identifier key.
IDENTIFIER_ALIASES = {
    "fbp": "fbp", "_fbp": "fbp",
    "fbc": "fbc", "_fbc": "fbc",
    "gaClientId": "gaClientId", "_ga": "gaClientId", "ga4ClientId": "gaClientId",
    "posthogDistinctId": "posthogDistinctId", "posthog_distinct_id": "posthogDistinctId",
    "mixpanelDeviceId": "mixpanelDeviceId", "mixpanel_device_id": "mixpanelDeviceId",
    "amplitudeDeviceId": "amplitudeDeviceId", "amplitude_device_id": "amplitudeDeviceId",
    "gclAw": "gclAw", "_gcl_aw": "gclAw",
    "gclAu": "gclAu", "_gcl_au": "gclAu",
    "scid": "scid", "_scid": "scid",
    "rdtUuid": "rdtUuid", "_rdt_uuid": "rdtUuid",
    "twclid": "twclid", "_twclid": "twclid",
}
# Consent purpose required per identifier.
IDENTIFIER_PURPOSE = {
    "gaClientId": "analytics",
    "posthogDistinctId": "analytics",
    "mixpanelDeviceId": "analytics",
    "amplitudeDeviceId": "analytics",
    "fbp": "advertising",
    "fbc": "advertising",
    "gclAw": "advertising",
    "gclAu": "advertising",
    "scid": "advertising",
    "rdtUuid": "advertising",
    "twclid": "advertising",
}

# Any unexpanded ValueTrack/template placeholder ("{keyword}", "{{campaign}}", "abc{x}").
_PLACEHOLDER_RE = re.compile(r"[{}]")
_PRINTABLE_ASCII_RE = re.compile(r"^[\x21-\x7e]+$")


def _granted(consents, purpose):
    if not isinstance(consents, dict):
        return False
    entry = consents.get(purpose)
    return isinstance(entry, dict) and entry.get("granted") is True


def _clean_parameter(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_VALUE_LENGTH or _PLACEHOLDER_RE.search(value):
        return None
    if any(ord(c) < 32 or ord(c) == 0x7F for c in value):
        return None
    return value


def _clean_identifier(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_VALUE_LENGTH or not _PRINTABLE_ASCII_RE.match(value):
        return None
    return value


def strip_url(url):
    """Keep only scheme, host (and port) and path of an http(s) URL; else ``""``."""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if not url or len(url) > MAX_URL_LENGTH or any(ord(c) < 32 or ord(c) == 0x7F for c in url):
        return ""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return ""
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    result = urlunsplit((scheme, netloc, parts.path, "", ""))
    return result if len(result) <= MAX_URL_LENGTH else ""


def clean_attribution(raw, consents):
    """Return ``{"parameters", "identifiers", "pageUrl", "referrer"}`` with only allowlisted data.

    No tracking before consent: with neither analytics nor advertising granted nothing is
    kept (campaign/ValueTrack parameters and page/referrer URLs need either purpose;
    click IDs and ad cookies need advertising; the GA client ID needs analytics).
    """
    raw = raw if isinstance(raw, dict) else {}
    advertising = _granted(consents, "advertising")
    analytics = _granted(consents, "analytics")
    any_tracking = advertising or analytics

    parameters = {}
    raw_params = raw.get("parameters")
    if isinstance(raw_params, dict) and any_tracking:
        for key, value in raw_params.items():
            if key not in ALLOWED_PARAMETERS:
                continue
            if key in CLICK_ID_PARAMETERS and not advertising:
                continue
            cleaned = _clean_parameter(value)
            if cleaned is not None:
                parameters[key] = cleaned

    identifiers = {}
    raw_ids = raw.get("identifiers")
    if isinstance(raw_ids, dict):
        for key, value in raw_ids.items():
            canonical = IDENTIFIER_ALIASES.get(key) if isinstance(key, str) else None
            if canonical is None:
                continue
            purpose = IDENTIFIER_PURPOSE[canonical]
            if (purpose == "analytics" and not analytics) or (purpose == "advertising" and not advertising):
                continue
            cleaned = _clean_identifier(value)
            if cleaned is not None and canonical not in identifiers:
                identifiers[canonical] = cleaned

    return {
        "parameters": parameters,
        "identifiers": identifiers,
        "pageUrl": strip_url(raw.get("pageUrl")) if any_tracking else "",
        "referrer": strip_url(raw.get("referrer")) if any_tracking else "",
    }
