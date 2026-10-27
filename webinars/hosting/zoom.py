"""Zoom Webinars via a Server-to-Server OAuth app.

Contract (documented-unverified, needs sandbox verification):

* token: ``POST https://zoom.us/oauth/token`` with HTTP Basic ``client_id:client_secret`` and
  form ``grant_type=account_credentials&account_id=...``; ~1 h token, cached in memory only;
* create: ``POST https://api.zoom.us/v2/users/{hostUserId}/webinars`` ``{topic, type: 5,
  start_time (UTC ...Z), duration, timezone, settings}``; needs a Zoom Webinars licence on
  the host user. Response ``id``, ``join_url`` (attendee), ``start_url`` (PRIVATE host link);
* update: ``PATCH https://api.zoom.us/v2/webinars/{id}`` (204);
* attach/verify: ``GET https://api.zoom.us/v2/webinars/{id}``;
* attendance (read-only): ``GET https://api.zoom.us/v2/past_webinars/{webinarId}/participants
  ?page_size=N[&next_page_token=...]`` (scope ``webinar:read:admin``; Pro-or-higher account
  with the Webinar add-on). Response ``participants[]`` with ``id``, ``user_id``,
  ``user_email``, ``name``, ``join_time``, ``leave_time``, ``duration`` (seconds) and
  ``next_page_token`` (expires after 15 minutes). Pages are capped (``ATTENDANCE_MAX_PAGES``)
  and sized to fit the 64 KB response bound of the transport (``ATTENDANCE_PAGE_SIZE``).

Zoom create has no idempotency key: an unknown outcome stops for reconciliation.
"""
import base64
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
    cached_token,
    https_link,
    scalar_id,
    store_token,
    utc_z,
)

OAUTH_HOST = "zoom.us"
API_HOST = "api.zoom.us"
TOKEN_URL = f"https://{OAUTH_HOST}/oauth/token"
API_BASE = f"https://{API_HOST}/v2"

_HOST_USER = re.compile(r"^(?:[A-Za-z0-9_-]{6,64}|[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24})$")
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
ZOOM_WEBINAR_ID = re.compile(r"^[0-9]{6,20}$")
TOPIC_MAX = 200
# Attendance pull: Zoom allows page_size up to 300, but 300 participant objects can exceed
# the transport's 64 KB response cap, so pages are smaller and more of them are allowed.
ATTENDANCE_PAGE_SIZE = 100
ATTENDANCE_MAX_PAGES = 50
_PAGE_TOKEN = re.compile(r"^[A-Za-z0-9_.:=+/-]{1,512}$")


class ZoomAdapter(Adapter):
    provider = "zoom"
    label = "Zoom Webinars"
    capabilities = Capabilities(create=True, update=True, attach=True, attendance_import=True)
    contract = "documented-unverified"
    allowed_hosts = frozenset({OAUTH_HOST, API_HOST})
    link_hosts = ("zoom.us", "zoom.com")
    credential_fields = (
        Field("account_id", "Account ID", "From your Server-to-Server OAuth app (App credentials).", secret=True, pattern=_ACCOUNT_ID),
        Field("client_id", "Client ID", "From the same Server-to-Server OAuth app.", secret=True, pattern=SECRET_PATTERN),
        Field("client_secret", "Client secret", "From the same app. Stored encrypted; never shown again.", secret=True, pattern=SECRET_PATTERN),
    )
    config_fields = (
        Field(
            "host_user_id",
            "Host user ID or email",
            "The licensed Zoom user that hosts the webinars (needs a Webinars licence). Scopes: webinar:write:admin, webinar:read:admin.",
            pattern=_HOST_USER,
        ),
    )
    notes = (
        "Built from Zoom's public API documentation; needs sandbox verification before production use.",
        "Create has no idempotency key: if the outcome is unknown the session is marked for reconciliation and never re-sent automatically.",
        "The host start link is stored encrypted and never shown in public pages or APIs.",
        "Attendance is pulled from past_webinars/{id}/participants (webinar:read:admin) only when a person asks; "
        "it is never applied without a confirmed preview.",
    )

    # ---- plans (preview-safe)

    def _body(self, occ, full=True):
        body = {
            "topic": (occ.event.public_title or occ.event.internal_name)[:TOPIC_MAX],
            "start_time": utc_z(occ.starts_at),
            "duration": int(occ.duration_minutes),
            "timezone": occ.timezone,
        }
        if full:
            body["type"] = 5
            # approval_type 2 = no Zoom registration: attendees register here and get the join link from us.
            body["settings"] = {"approval_type": 2}
        return body

    def _token_plan(self):
        return Plan("token", "POST", TOKEN_URL, form={"grant_type": "account_credentials", "account_id": ""},
                    headers={"Authorization": REDACTED})

    def plan(self, action, occ, config, external_id=""):
        auth = {"Authorization": REDACTED}
        if action == "create":
            host = quote(config.get("host_user_id", ""), safe="@.")
            return [self._token_plan(), Plan("create", "POST", f"{API_BASE}/users/{host}/webinars", body=self._body(occ), headers=auth)]
        if action == "update":
            return [self._token_plan(), Plan("update", "PATCH", f"{API_BASE}/webinars/{quote(external_id, safe='')}", body=self._body(occ, full=False), headers=auth)]
        if action == "attach":
            return [self._token_plan(), Plan("attach", "GET", f"{API_BASE}/webinars/{quote(external_id, safe='')}", headers=auth)]
        if action == "attendance":
            return [self._token_plan(), Plan("attendance", "GET", self._participants_url(external_id or "{webinarId}"), headers=auth)]
        raise Unsupported(action)

    def _participants_url(self, external_id, page_token=""):
        url = f"{API_BASE}/past_webinars/{quote(external_id, safe='')}/participants?page_size={ATTENDANCE_PAGE_SIZE}"
        if page_token:
            url += f"&next_page_token={quote(page_token, safe='')}"
        return url

    def valid_external_id(self, value):
        return bool(isinstance(value, str) and ZOOM_WEBINAR_ID.match(value) and EXTERNAL_ID.match(value))

    # ---- calls

    def _token(self, transport, secrets):
        token = cached_token(self.provider, secrets)
        if token:
            return token
        basic = base64.b64encode(f"{secrets['client_id']}:{secrets['client_secret']}".encode()).decode()
        data = self._send(
            transport,
            "token",
            "POST",
            TOKEN_URL,
            form={"grant_type": "account_credentials", "account_id": secrets["account_id"]},
            headers={"Authorization": f"Basic {basic}"},
            side_effect=False,
        )
        token = data.get("access_token")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise _Stop(Outcome("failed", "token: response_missing_token"))
        store_token(self.provider, secrets, token, data.get("expires_in"))
        return token

    def run(self, action, occ, config, secrets, *, transport, external_id="", registration=None, account_key=""):
        try:
            token = self._token(transport, secrets)
            auth = {"Authorization": f"Bearer {token}"}
            if action == "create":
                [_t, p] = self.plan("create", occ, config)
                data = self._send(transport, "create", "POST", p.url, json_body=p.body, headers=auth, side_effect=True)
                ext = scalar_id(data, "id")
                if not ext:
                    # 2xx but no id: a webinar was probably created; reconcile by hand.
                    return Outcome("unknown", "create: response_missing_id")
                return Outcome(
                    "succeeded",
                    external_id=ext,
                    join_url=https_link(data.get("join_url"), self.link_hosts),
                    host_url=https_link(data.get("start_url"), self.link_hosts),
                )
            if action == "update":
                [_t, p] = self.plan("update", occ, config, external_id)
                # PATCH with the full schedule is idempotent: an unknown outcome may be retried.
                self._send(transport, "update", "PATCH", p.url, json_body=p.body, headers=auth, side_effect=True)
                return Outcome("succeeded", external_id=external_id)
            if action == "attach":
                [_t, p] = self.plan("attach", occ, config, external_id)
                data = self._send(transport, "attach", "GET", p.url, headers=auth, side_effect=False)
                ext = scalar_id(data, "id")
                if ext != external_id:
                    return Outcome("failed", "attach: response_id_mismatch")
                return Outcome(
                    "succeeded",
                    external_id=ext,
                    join_url=https_link(data.get("join_url"), self.link_hosts),
                    host_url=https_link(data.get("start_url"), self.link_hosts),
                )
        except _Stop as stop:
            return stop.outcome
        raise Unsupported(action)

    def fetch_attendance(self, occ, config, secrets, external_id, *, transport):
        """Read-only pull of the participants of a past webinar (paginated, capped)."""
        if not self.valid_external_id(external_id):
            return Outcome("failed", "attendance: invalid_external_id")
        rows = []
        try:
            token = self._token(transport, secrets)
            auth = {"Authorization": f"Bearer {token}"}
            page_token = ""
            complete = False
            for _page in range(ATTENDANCE_MAX_PAGES):
                data = self._send(transport, "attendance", "GET", self._participants_url(external_id, page_token),
                                  headers=auth, side_effect=False)
                items = data.get("participants")
                if not isinstance(items, list):
                    return Outcome("failed", "attendance: response_unexpected")
                for item in items:
                    row = _participant_row(item)
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


def _participant_row(item):
    """Normalise one Zoom participant object; None when it has nothing usable."""
    if not isinstance(item, dict):
        return None
    email = item.get("user_email")
    name = item.get("name")
    duration = item.get("duration")
    row = {
        "external_id": scalar_id(item, "id", "user_id"),
        "email": email.strip() if isinstance(email, str) else "",
        "name": name.strip() if isinstance(name, str) else "",
        "joined_at": item.get("join_time") if isinstance(item.get("join_time"), str) else "",
        "left_at": item.get("leave_time") if isinstance(item.get("leave_time"), str) else "",
        "duration_seconds": duration if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0 else None,
    }
    if not row["external_id"] and not row["email"]:
        return None
    return row
