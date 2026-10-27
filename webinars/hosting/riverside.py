"""Riverside: attach an existing event and create per-registrant join links.

Contract: UNVERIFIED. Riverside documents attaching to an existing event, listing
registrants and creating a registrant that returns a unique join link, but we could not
confirm the exact paths and field names. Everything below is a pinned constant so it can
be corrected in one place after sandbox verification:

* host ``API_HOST`` (UNVERIFIED), Bearer API key;
* verify event: ``GET /api/v1/events/{eventId}`` (UNVERIFIED);
* create registrant: ``POST /api/v1/events/{eventId}/registrants`` ``{email, first_name,
  last_name}`` -> ``{id, join_link}`` (UNVERIFIED).

Session creation is not documented: capability ``create`` is false. Registrant creation
has no idempotency key: an unknown outcome marks that registrant for reconciliation and
it is never re-sent automatically (listing registrants by email is not used because that
filter is not documented).
"""
import re
from urllib.parse import quote

from .base import (
    EXTERNAL_ID,
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
)

# UNVERIFIED constants (docs/hosting.md).
API_HOST = "platform.riverside.fm"
API_BASE = f"https://{API_HOST}/api/v1"
EVENT_PATH = "/events/{event_id}"
REGISTRANTS_PATH = "/events/{event_id}/registrants"
_EVENT_ID = re.compile(r"^[A-Za-z0-9_-]{4,128}$")
_LINK_KEYS = ("join_link", "join_url", "link", "url")


class RiversideAdapter(Adapter):
    provider = "riverside"
    label = "Riverside (attach existing event)"
    capabilities = Capabilities(attach=True, register_attendees=True)
    contract = "unverified"
    allowed_hosts = frozenset({API_HOST})
    link_hosts = ("riverside.fm", "riverside.com")
    credential_fields = (
        Field("api_key", "API key", "Riverside API key for your workspace. Stored encrypted; never shown again.", secret=True, pattern=SECRET_PATTERN),
    )
    config_fields = ()
    notes = (
        "Endpoint paths and field names are UNVERIFIED; verify against a Riverside sandbox before production use.",
        "Riverside sessions are not created here: create the event in Riverside and attach its ID.",
        "Each registrant gets a unique join link, created after registration and used by the waiting room.",
        "Registrant creation has no idempotency key: unknown outcomes are marked for reconciliation.",
    )

    def valid_external_id(self, value):
        return bool(isinstance(value, str) and _EVENT_ID.match(value) and EXTERNAL_ID.match(value))

    def _url(self, path, event_id):
        return API_BASE + path.format(event_id=quote(event_id, safe=""))

    def plan(self, action, occ, config, external_id=""):
        auth = {"Authorization": REDACTED}
        if action == "attach":
            return [Plan("attach", "GET", self._url(EVENT_PATH, external_id or "{eventId}"), headers=auth)]
        if action == "register":
            return [Plan("register", "POST", self._url(REGISTRANTS_PATH, external_id or "{eventId}"),
                         body={"email": "<registrant email>", "first_name": "<first name>", "last_name": "<last name>"}, headers=auth)]
        raise Unsupported(action)

    def run(self, action, occ, config, secrets, *, transport, external_id="", registration=None, account_key=""):
        auth = {"Authorization": f"Bearer {secrets['api_key']}"}
        try:
            if action == "attach":
                data = self._send(transport, "attach", "GET", self._url(EVENT_PATH, external_id), headers=auth, side_effect=False)
                found = scalar_id(data, "id", "event_id", "eventId")
                if found and found != external_id:
                    return Outcome("failed", "attach: response_id_mismatch")
                if not found and not data:
                    return Outcome("failed", "attach: response_empty")
                return Outcome("succeeded", external_id=external_id)
            if action == "register":
                person = registration.person
                body = {"email": person.email, "first_name": person.first_name or "", "last_name": person.last_name or ""}
                data = self._send(transport, "register", "POST", self._url(REGISTRANTS_PATH, external_id), json_body=body,
                                  headers=auth, side_effect=True)
                ext = scalar_id(data, "id", "registrant_id")
                link = ""
                for container in (data, data.get("data") if isinstance(data.get("data"), dict) else {}):
                    for key in _LINK_KEYS:
                        link = link or https_link(container.get(key), self.link_hosts)
                if not link:
                    # 2xx without a usable link: a registrant probably exists; reconcile by hand.
                    return Outcome("unknown", "register: response_missing_link")
                return Outcome("succeeded", external_id=ext, join_url=link)
        except _Stop as stop:
            return stop.outcome
        raise Unsupported(action)
