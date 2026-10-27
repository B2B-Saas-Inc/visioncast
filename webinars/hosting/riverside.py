"""Riverside: attach an existing event and create per-registrant join links.

Contract: UNVERIFIED. Riverside documents attaching to an existing event, listing
registrants and creating a registrant that returns a unique join link, but we could not
confirm the exact paths and field names. Everything below is a pinned constant so it can
be corrected in one place after sandbox verification:

* host ``API_HOST`` (UNVERIFIED), Bearer API key;
* verify event: ``GET /api/v1/events/{eventId}`` (UNVERIFIED);
* create registrant: ``POST /api/v1/events/{eventId}/registrants`` ``{email, first_name,
  last_name}`` -> ``{id, join_link}`` (UNVERIFIED);
* attendance (read-only): ``GET /api/v1/events/{eventId}/registrants`` listing registrants
  with an attended/joined marker (``ATTENDANCE_ATTENDED_KEYS``) and optional
  ``joined_at`` / ``left_at`` / ``duration`` fields; pagination via ``next_page_token``
  (UNVERIFIED, capped at ``ATTENDANCE_MAX_PAGES``). Rows are matched to our
  ``ProviderRegistrant.external_id`` first, then by email.

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
# UNVERIFIED attendance constants: the registrant list path (same as REGISTRANTS_PATH), the
# keys under which the list and the attended marker may appear, and the page token name.
ATTENDANCE_PATH = REGISTRANTS_PATH
ATTENDANCE_LIST_KEYS = ("registrants", "data", "items")
ATTENDANCE_ATTENDED_KEYS = ("attended", "joined", "has_joined", "has_attended")
ATTENDANCE_PAGE_PARAM = "page_token"
ATTENDANCE_MAX_PAGES = 20
_PAGE_TOKEN = re.compile(r"^[A-Za-z0-9_.:=+/-]{1,512}$")


class RiversideAdapter(Adapter):
    provider = "riverside"
    label = "Riverside (attach existing event)"
    capabilities = Capabilities(attach=True, register_attendees=True, attendance_import=True)
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
        "Attendance is read from the event's registrant list (UNVERIFIED attended marker) only when a person asks.",
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
        if action == "attendance":
            return [Plan("attendance", "GET", self._url(ATTENDANCE_PATH, external_id or "{eventId}"), headers=auth)]
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

    def fetch_attendance(self, occ, config, secrets, external_id, *, transport):
        """Read-only: registrants of the event that carry an attended/joined marker (UNVERIFIED)."""
        if not self.valid_external_id(external_id):
            return Outcome("failed", "attendance: invalid_external_id")
        auth = {"Authorization": f"Bearer {secrets['api_key']}"}
        rows = []
        page_token = ""
        complete = False
        try:
            for _page in range(ATTENDANCE_MAX_PAGES):
                url = self._url(ATTENDANCE_PATH, external_id)
                if page_token:
                    url += f"?{ATTENDANCE_PAGE_PARAM}={quote(page_token, safe='')}"
                data = self._send(transport, "attendance", "GET", url, headers=auth, side_effect=False)
                items = None
                for key in ATTENDANCE_LIST_KEYS:
                    if isinstance(data.get(key), list):
                        items = data[key]
                        break
                if items is None:
                    return Outcome("failed", "attendance: response_unexpected")
                for item in items:
                    row = _registrant_row(item)
                    if row is not None:
                        rows.append(row)
                nxt = data.get("next_page_token")
                if not isinstance(nxt, str) or not nxt or not _PAGE_TOKEN.match(nxt):
                    complete = True
                    break
                page_token = nxt
        except _Stop as stop:
            return stop.outcome
        return Outcome("succeeded", external_id=external_id, data={"rows": rows, "complete": complete})


def _registrant_row(item):
    """One attended registrant as a normalised row; None for non-attended or unusable items."""
    if not isinstance(item, dict):
        return None
    attended = any(item.get(k) is True for k in ATTENDANCE_ATTENDED_KEYS)
    joined = item.get("joined_at") or item.get("join_time")
    if not attended and not isinstance(joined, str):
        return None
    email = item.get("email")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        first, last = item.get("first_name"), item.get("last_name")
        name = " ".join(p.strip() for p in (first, last) if isinstance(p, str) and p.strip())
    duration = item.get("duration") if "duration" in item else item.get("duration_seconds")
    left = item.get("left_at") or item.get("leave_time")
    row = {
        "external_id": scalar_id(item, "id", "registrant_id"),
        "email": email.strip() if isinstance(email, str) else "",
        "name": name.strip() if isinstance(name, str) else "",
        "joined_at": joined if isinstance(joined, str) else "",
        "left_at": left if isinstance(left, str) else "",
        "duration_seconds": duration if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0 else None,
    }
    if not row["external_id"] and not row["email"]:
        return None
    return row
