"""StreamYard: manual links only. StreamYard has no public API, so this adapter makes no
outbound calls at all. The marketer pastes the attendee watch/join link (https, a
streamyard.com host or an operator-allowed embed host) and an optional embed URL.
"""
from django.conf import settings
from django.core.exceptions import ValidationError

from .base import Adapter, Capabilities, Unsupported, https_link

STREAMYARD_HOSTS = ("streamyard.com",)


def manual_link_hosts():
    """streamyard.com (and sub-domains) plus ``HOSTING_MANUAL_LINK_HOSTS`` (operator setting)."""
    extra = getattr(settings, "HOSTING_MANUAL_LINK_HOSTS", None) or []
    if isinstance(extra, str):
        extra = [h for h in extra.split(",")]
    cleaned = [str(h).strip().lstrip(".").rstrip(".").lower() for h in extra if str(h).strip()]
    return tuple(dict.fromkeys(STREAMYARD_HOSTS + tuple(cleaned)))


class StreamYardAdapter(Adapter):
    provider = "streamyard"
    label = "StreamYard (manual link)"
    capabilities = Capabilities(manual_link=True)
    contract = "none"
    allowed_hosts = frozenset()
    credential_fields = ()
    config_fields = ()
    notes = (
        "StreamYard has no public API: nothing is created, updated or read automatically.",
        "Paste the attendee link (and optionally an embed URL); it stays private until the waiting room admits someone.",
        "After a reschedule, change the time in StreamYard yourself and confirm it here.",
    )

    def allowlist_problems(self):
        return []

    def clean_link(self, value, label):
        value = (value or "").strip()
        if not value:
            return ""
        hosts = manual_link_hosts()
        if not https_link(value, hosts):
            raise ValidationError(f"{label} must be an https link on {', '.join(hosts)}.")
        return value

    def plan(self, action, occ, config, external_id=""):
        raise Unsupported(action)

    def run(self, action, *args, **kwargs):
        raise Unsupported(action)
