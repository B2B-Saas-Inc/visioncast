"""Wistia Webinars (create only).

Contract (documented-unverified, needs sandbox verification):
``POST https://api.wistia.com/modern/webinars`` with ``Authorization: Bearer <token>`` and
``X-Wistia-API-Version: 2026-09``; required ``title``, ``scheduled_for`` (UTC),
``event_duration`` (at least 15 minutes) and ``time_zone``.

Not offered (capability false): update and attach/read-back, because we are not confident
of those contracts. After a reschedule change the time in Wistia yourself, then confirm it
here. There is no idempotency key: an unknown outcome stops for reconciliation. The
response id is read from ``id``/``hashed_id``; an attendee link is only stored if the
response carries an https link on a Wistia host (UNVERIFIED field names).
"""
from .base import (
    Adapter,
    Capabilities,
    Field,
    Outcome,
    Plan,
    REDACTED,
    SECRET_PATTERN,
    Unsupported,
    _Stop,
    https_link,
    scalar_id,
    utc_z,
)

API_HOST = "api.wistia.com"
CREATE_URL = f"https://{API_HOST}/modern/webinars"
API_VERSION = "2026-09"
MIN_DURATION_MINUTES = 15
TITLE_MAX = 255
_LINK_KEYS = ("join_url", "registration_url", "event_url", "url")


class WistiaAdapter(Adapter):
    provider = "wistia"
    label = "Wistia Webinars"
    capabilities = Capabilities(create=True)
    contract = "documented-unverified"
    allowed_hosts = frozenset({API_HOST})
    link_hosts = ("wistia.com", "wistia.net")
    credential_fields = (
        Field("api_token", "API token", "A Wistia API token with webinar write access. Stored encrypted; never shown again.", secret=True, pattern=SECRET_PATTERN),
    )
    config_fields = ()
    notes = (
        "Built from Wistia's public API documentation; needs sandbox verification before production use.",
        f"Sessions must last at least {MIN_DURATION_MINUTES} minutes.",
        "Update is not offered: after a reschedule, change the time in Wistia and confirm it here.",
        "Create has no idempotency key: an unknown outcome is marked for reconciliation and never re-sent automatically.",
    )

    def occurrence_problems(self, occ, action):
        if action == "create" and int(occ.duration_minutes) < MIN_DURATION_MINUTES:
            return [f"Wistia webinars must last at least {MIN_DURATION_MINUTES} minutes."]
        return []

    def _body(self, occ):
        return {
            "title": (occ.event.public_title or occ.event.internal_name)[:TITLE_MAX],
            "scheduled_for": utc_z(occ.starts_at),
            "event_duration": int(occ.duration_minutes),
            "time_zone": occ.timezone,
        }

    def plan(self, action, occ, config, external_id=""):
        if action == "create":
            return [Plan("create", "POST", CREATE_URL, body=self._body(occ),
                         headers={"Authorization": REDACTED, "X-Wistia-API-Version": API_VERSION})]
        raise Unsupported(action)

    def run(self, action, occ, config, secrets, *, transport, external_id="", registration=None, account_key=""):
        if action != "create":
            raise Unsupported(action)
        headers = {"Authorization": f"Bearer {secrets['api_token']}", "X-Wistia-API-Version": API_VERSION}
        try:
            data = self._send(transport, "create", "POST", CREATE_URL, json_body=self._body(occ), headers=headers, side_effect=True)
        except _Stop as stop:
            return stop.outcome
        ext = scalar_id(data, "id", "hashed_id")
        if not ext:
            return Outcome("unknown", "create: response_missing_id")
        link = ""
        for container in (data, data.get("data") if isinstance(data.get("data"), dict) else {}):
            for key in _LINK_KEYS:
                link = link or https_link(container.get(key), self.link_hosts)
        return Outcome("succeeded", external_id=ext, join_url=link)
