"""Sequel (Introvoke) events (create only).

Contract (documented-unverified, needs sandbox verification):

* token: ``POST https://api.introvoke.com/api/oauth/token`` (client credentials; we send
  form ``grant_type=client_credentials&client_id=...&client_secret=...``; cached in memory);
* create: ``POST https://api.introvoke.com/api/v1/event/create`` with a Bearer token and
  ``{companyId, name, startDate, endDate, timezone, type: "Virtual"}``.

Update is undocumented: capability false (change the time in Sequel, then confirm here).
No idempotency key: an unknown outcome stops for reconciliation. The response id is read
from ``id``/``_id``/``uid``/``eventId`` (UNVERIFIED field names); no attendee link is
assumed from the response.
"""
import re
from datetime import timedelta

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
    cached_token,
    scalar_id,
    store_token,
    utc_z,
)

API_HOST = "api.introvoke.com"
TOKEN_URL = f"https://{API_HOST}/api/oauth/token"
CREATE_URL = f"https://{API_HOST}/api/v1/event/create"
_COMPANY_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NAME_MAX = 255


class SequelAdapter(Adapter):
    provider = "sequel"
    label = "Sequel (Introvoke)"
    capabilities = Capabilities(create=True)
    contract = "documented-unverified"
    allowed_hosts = frozenset({API_HOST})
    credential_fields = (
        Field("client_id", "Client ID", "API client ID from Sequel.", secret=True, pattern=SECRET_PATTERN),
        Field("client_secret", "Client secret", "API client secret. Stored encrypted; never shown again.", secret=True, pattern=SECRET_PATTERN),
    )
    config_fields = (
        Field("company_id", "Company ID", "Your Sequel company ID (letters, digits, '-' or '_').", pattern=_COMPANY_ID),
    )
    notes = (
        "Built from Sequel's public API documentation; needs sandbox verification before production use.",
        "Update is not documented: after a reschedule, change the time in Sequel and confirm it here.",
        "Create has no idempotency key: an unknown outcome is marked for reconciliation and never re-sent automatically.",
        "Add the attendee join link in Session details after creation.",
    )

    def _body(self, occ, config):
        return {
            "companyId": config.get("company_id", ""),
            "name": (occ.event.public_title or occ.event.internal_name)[:NAME_MAX],
            "startDate": utc_z(occ.starts_at),
            "endDate": utc_z(occ.starts_at + timedelta(minutes=int(occ.duration_minutes))),
            "timezone": occ.timezone,
            "type": "Virtual",
        }

    def _token_plan(self):
        return Plan("token", "POST", TOKEN_URL, form={"grant_type": "", "client_id": "", "client_secret": ""})

    def plan(self, action, occ, config, external_id=""):
        if action == "create":
            return [self._token_plan(), Plan("create", "POST", CREATE_URL, body=self._body(occ, config), headers={"Authorization": REDACTED})]
        raise Unsupported(action)

    def _token(self, transport, secrets):
        token = cached_token(self.provider, secrets)
        if token:
            return token
        data = self._send(
            transport,
            "token",
            "POST",
            TOKEN_URL,
            form={"grant_type": "client_credentials", "client_id": secrets["client_id"], "client_secret": secrets["client_secret"]},
            side_effect=False,
        )
        token = data.get("access_token")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise _Stop(Outcome("failed", "token: response_missing_token"))
        store_token(self.provider, secrets, token, data.get("expires_in"))
        return token

    def run(self, action, occ, config, secrets, *, transport, external_id="", registration=None, account_key=""):
        if action != "create":
            raise Unsupported(action)
        try:
            token = self._token(transport, secrets)
            data = self._send(
                transport, "create", "POST", CREATE_URL, json_body=self._body(occ, config),
                headers={"Authorization": f"Bearer {token}"}, side_effect=True,
            )
        except _Stop as stop:
            return stop.outcome
        ext = scalar_id(data, "id", "_id", "uid", "eventId")
        if not ext:
            return Outcome("unknown", "create: response_missing_id")
        return Outcome("succeeded", external_id=ext)
