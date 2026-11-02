"""LiveKit: self-hosted rooms through the ``visioncast-live`` service (``live/``).

The service sits in front of a LiveKit server the operator runs, and is the only thing this
adapter talks to. It creates a room per session, gives each registrant a personal join link,
and reports attendance from LiveKit's webhooks. Contract: ours, exercised end to end against
a real LiveKit server by ``live/tests/e2e/``.

Every request here is safe to repeat (rooms and links are derived from their IDs), so failures
retry instead of needing a person to reconcile them. Only the registration ID leaves
VisionCast: no name or email address.

The service's host is typed by an Administrator and receives a registrant's ID and the service
secret, so unlike a vendor's fixed API host it must be an exact ``WEBHOOK_HOST_ALLOWLIST``
entry, the same rule as other self-hosted destinations.
"""
import re

from webinars.native_integrations import _HOSTNAME, destination_host_problem

from .base import (
    Adapter,
    Capabilities,
    EXTERNAL_ID,
    Field,
    Outcome,
    Plan,
    REDACTED,
    Unsupported,
    _Stop,
    https_link,
    scalar_id,
    send,
    utc_z,
)

_SERVICE_SECRET = re.compile(r"^[\x21-\x7e]{32,512}$")
_YES = re.compile(r"^(yes|no)$")
MAX_RECORDINGS = 50
MAX_CAPTION_FILES = 40
ENGAGEMENT_MAX_PAGES = 500
ATTENDANCE_MAX_PAGES = 25  # 200 rows a page, the most one import takes
_TIME = re.compile(r"^[0-9T:\-.+Z]{1,40}$")
_CURSOR = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def room_id(occ):
    return f"vc-{occ.pk}"


def room_slug(occ):
    """The session's address on the live server: the event's slug, and when the event has more than
    one session, its date and time as well."""
    base_slug = re.sub(r"[^a-z0-9-]+", "-", occ.event.slug.lower()).strip("-")[:150] or "session"
    if occ.event.occurrences.exclude(pk=occ.pk).exists():
        return f"{base_slug}-{occ.starts_at:%Y-%m-%d-%H%M}"
    return base_slug


class LiveKitAdapter(Adapter):
    provider = "livekit"
    label = "LiveKit (self-hosted)"
    capabilities = Capabilities(create=True, update=True, register_attendees=True, attendance_import=True,
                                host_room=True, recordings=True, live_settings=True)
    contract = "self-hosted"
    register_idempotent = True
    allowed_hosts = frozenset()  # one per account: see hosts_for()
    credential_fields = (
        Field("service_secret", "Service secret", "LIVE_SERVICE_SECRET from your visioncast-live service, at least 32 characters. "
              "Stored encrypted; never shown again.", secret=True, pattern=_SERVICE_SECRET),
    )
    config_fields = (
        Field("service_host", "Service host", "Host name of your visioncast-live service, e.g. live.example.com. "
              "Your operator must list this exact host in WEBHOOK_HOST_ALLOWLIST.", pattern=_HOSTNAME),
        Field("auto_record", "Recording", "Hosts can always start and stop recording from the room, and everyone in the "
              "room sees when it's on.", required=False, pattern=_YES,
              choices=(("", "Start it from the room"), ("yes", "Start when the session goes live"))),
    )
    notes = (
        "Runs on your own LiveKit server, so you pay for the servers and bandwidth instead of a vendor.",
        "Each registrant gets a personal link, which the waiting room hands out at admission. Each crew member gets their own link too, from the session's hosting page.",
        "Attendance comes from LiveKit's webhooks and is imported when a person asks for it.",
    )

    # ---- the per-account host

    def hosts_for(self, config):
        host = (config or {}).get("service_host", "")
        return frozenset({host}) if host else frozenset()

    def join_link_hosts(self, config):
        return tuple(self.hosts_for(config))

    def allowlist_problems(self):
        return []  # checked per account: config_problems()

    def config_problems(self, config):
        host = (config or {}).get("service_host", "")
        if not host:
            return ["Set the service host on this hosting account."]
        problem = destination_host_problem(host)
        if problem == "not_allowlisted":
            return [f"Ask your operator to add exactly {host} to WEBHOOK_HOST_ALLOWLIST before using LiveKit."]
        if problem is not None:
            return [f"{host} can't be used as the service host."]
        return []

    def validate_config(self, config):
        errors = super().validate_config(config)
        if "service_host" not in errors and (config or {}).get("service_host"):
            problems = self.config_problems(config)
            if problems:
                errors["service_host"] = problems
        return errors

    def valid_external_id(self, value):
        return bool(isinstance(value, str) and EXTERNAL_ID.match(value))

    # ---- requests

    def _url(self, config, path):
        return f"https://{(config or {}).get('service_host') or '{service_host}'}/v1{path}"

    def _room_body(self, occ, config):
        return {
            "title": occ.event.public_title,
            "startsAt": utc_z(occ.starts_at),
            "endsAt": utc_z(occ.ends_at),
            "autoRecord": (config or {}).get("auto_record") == "yes",
            "slug": room_slug(occ),
            "entryUrl": _entry_url(occ),
        }

    def plan(self, action, occ, config, external_id=""):
        auth = {"Authorization": REDACTED}
        room = external_id or room_id(occ)
        if action in ("create", "update"):
            return [Plan(action, "PUT", self._url(config, f"/rooms/{room}"), body=self._room_body(occ, config), headers=auth)]
        if action == "register":
            return [Plan("register", "POST", self._url(config, f"/rooms/{room}/tickets"), body={"identity": "<registration ID>"}, headers=auth)]
        if action == "attendance":
            return [Plan("attendance", "GET", self._url(config, f"/rooms/{room}/attendance"), headers=auth)]
        raise Unsupported(action)

    def _call(self, transport, config, secrets, step, method, path, json_body=None):
        problems = self.config_problems(config)
        if problems:
            raise _Stop(Outcome("failed", f"{step}: {problems[0]}"))
        # side_effect=False: repeating any of these requests is harmless, so a timeout is a
        # plain failure to retry rather than an unknown outcome for a person to untangle.
        return send(transport, self.hosts_for(config), step, method, self._url(config, path), json_body=json_body,
                    headers={"Authorization": f"Bearer {secrets['service_secret']}"}, side_effect=False)

    def run(self, action, occ, config, secrets, *, transport, external_id="", registration=None, account_key=""):
        try:
            room = external_id or room_id(occ)
            hosts = self.join_link_hosts(config)
            if action in ("create", "update"):
                data = self._call(transport, config, secrets, action, "PUT", f"/rooms/{room}", self._room_body(occ, config))
                # There's no shared host link: each host gets their own, when they ask.
                if scalar_id(data, "id") != room:
                    return Outcome("failed", f"{action}: response_unexpected")
                return Outcome("succeeded", external_id=room)
            if action == "register":
                identity = str(registration.pk)
                data = self._call(transport, config, secrets, "register", "POST", f"/rooms/{room}/tickets", {"identity": identity})
                link = https_link(data.get("joinUrl"), hosts)
                if not link:
                    return Outcome("failed", "register: response_missing_link")
                return Outcome("succeeded", external_id=identity, join_url=link)
        except _Stop as stop:
            return stop.outcome
        raise Unsupported(action)

    def fetch_attendance(self, occ, config, secrets, external_id, *, transport):
        if not self.valid_external_id(external_id):
            return Outcome("failed", "attendance: invalid_external_id")
        rows = []
        after = ""
        complete = False
        try:
            for _page in range(ATTENDANCE_MAX_PAGES):
                path = f"/rooms/{external_id}/attendance" + (f"?after={after}" if after else "")
                data = self._call(transport, config, secrets, "attendance", "GET", path)
                items = data.get("rows")
                if not isinstance(items, list):
                    return Outcome("failed", "attendance: response_unexpected")
                rows.extend(row for row in (_attendance_row(item) for item in items) if row is not None)
                after = data.get("next")
                if not isinstance(after, str) or not _CURSOR.match(after):
                    complete = data.get("complete") is True
                    break
        except _Stop as stop:
            return stop.outcome
        return Outcome("succeeded", external_id=external_id, data={"rows": rows, "complete": complete})

    def fetch_settings_link(self, config, secrets, room, *, email, name="", transport):
        try:
            data = self._call(transport, config, secrets, "settings_link", "POST", "/admin-link", {"room": room, "email": email, "name": name})
        except _Stop as stop:
            return stop.outcome
        url = https_link(data.get("url"), self.join_link_hosts(config))
        if not url:
            return Outcome("failed", "settings_link: response_unexpected")
        return Outcome("succeeded", data={"url": url})

    def crew_list(self, config, secrets, external_id, *, transport):
        """Read-only: who is on the session's crew. ``data`` is ``{"crew": [...]}``, links included."""
        try:
            data = self._call(transport, config, secrets, "crew", "GET", f"/rooms/{external_id}/crew")
        except _Stop as stop:
            return stop.outcome
        hosts = self.join_link_hosts(config)
        crew = []
        for item in data.get("crew") if isinstance(data.get("crew"), list) else []:
            url = https_link(item.get("url"), hosts) if isinstance(item, dict) else ""
            if url and scalar_id(item, "id"):
                crew.append({"id": scalar_id(item, "id"), "role": str(item.get("role") or "")[:16], "name": str(item.get("name") or "")[:80],
                             "title": str(item.get("title") or "")[:80], "url": url})
        return Outcome("succeeded", external_id=external_id, data={"crew": crew})

    def crew_add(self, config, secrets, external_id, *, role, name, title="", email="", key="", transport):
        """Adds someone, or finds them again by ``key``. ``join_url`` is their own link."""
        try:
            data = self._call(transport, config, secrets, "crew", "POST", f"/rooms/{external_id}/crew",
                              {"role": role, "name": name, "title": title, "email": email, "key": key})
        except _Stop as stop:
            return stop.outcome
        url = https_link(data.get("url"), self.join_link_hosts(config))
        if not url:
            return Outcome("failed", "crew: response_unexpected")
        return Outcome("succeeded", external_id=scalar_id(data, "id"), join_url=url)

    def crew_new_link(self, config, secrets, external_id, member_id, *, transport):
        try:
            data = self._call(transport, config, secrets, "crew", "POST", f"/rooms/{external_id}/crew/{member_id}/new-link")
        except _Stop as stop:
            return stop.outcome
        url = https_link(data.get("url"), self.join_link_hosts(config))
        return Outcome("succeeded", external_id=member_id, join_url=url) if url else Outcome("failed", "crew: response_unexpected")

    def crew_remove(self, config, secrets, external_id, member_id, *, transport):
        try:
            # A POST: outbound calls only allow reads and writes, never DELETE.
            self._call(transport, config, secrets, "crew", "POST", f"/rooms/{external_id}/crew/{member_id}/remove")
        except _Stop as stop:
            return stop.outcome
        return Outcome("succeeded", external_id=member_id)

    def put_brand_logo(self, config, secrets, png, *, transport):
        """Sets the logo on every room, or takes it off when ``png`` is empty."""
        path, method = ("/brand/logo", "PUT") if png else ("/brand/logo/remove", "POST")
        headers = {"Authorization": f"Bearer {secrets['service_secret']}", "Content-Type": "image/png"}
        try:
            status, _body, _, _ = transport(method, self._url(config, path), png or b"", headers, allowed_hosts=self.hosts_for(config))
        except Exception:
            return Outcome("failed", "brand: network")
        return Outcome("succeeded") if 200 <= status < 300 else Outcome("failed", f"brand: http {status}")

    def live_api(self, config, secrets, step, method, path, body=None, *, transport):
        """One request to the service's own API, for settings it keeps itself. ``data`` is the answer."""
        try:
            data = self._call(transport, config, secrets, step, method, path, body)
        except _Stop as stop:
            return stop.outcome
        return Outcome("succeeded", data=data)

    def fetch_engagement(self, config, secrets, external_id, *, transport):
        """Read-only: ``data`` is ``{"people": {identity: {...}}}``, as sent by the service, checked."""
        people = {}
        after = ""
        complete = False
        try:
            for _page in range(ENGAGEMENT_MAX_PAGES):
                path = f"/rooms/{external_id}/engagement" + (f"?after={after}" if after else "")
                data = self._call(transport, config, secrets, "engagement", "GET", path)
                items = data.get("people")
                if not isinstance(items, dict):
                    return Outcome("failed", "engagement: response_unexpected")
                people.update({str(k)[:128]: _engagement_row(v) for k, v in items.items() if isinstance(v, dict)})
                after = data.get("next")
                if not isinstance(after, str) or not _CURSOR.match(after):
                    complete = True
                    break
        except _Stop as stop:
            return stop.outcome
        return Outcome("succeeded", external_id=external_id, data={"people": people, "phase": str(data.get("phase") or "")[:16], "complete": complete})

    def forget_person(self, config, secrets, external_id, identity, *, transport):
        """Erases what the service holds on one attendee, for an erasure request."""
        try:
            self._call(transport, config, secrets, "forget", "POST", f"/rooms/{external_id}/people/{identity}/forget")
        except _Stop as stop:
            return stop.outcome
        return Outcome("succeeded", external_id=identity)

    def fetch_recordings(self, config, secrets, external_id, *, transport):
        try:
            data = self._call(transport, config, secrets, "recordings", "GET", f"/rooms/{external_id}/recordings")
        except _Stop as stop:
            return stop.outcome
        items = data.get("recordings")
        if not isinstance(items, list):
            return Outcome("failed", "recordings: response_unexpected")
        recordings = []
        for item in items[:MAX_RECORDINGS]:
            if isinstance(item, dict):
                recordings.append({
                    "status": str(item.get("status") or "")[:32],
                    "durationSeconds": item.get("durationSeconds") if isinstance(item.get("durationSeconds"), int) else 0,
                    "sizeBytes": item.get("sizeBytes") if isinstance(item.get("sizeBytes"), int) else 0,
                    "filename": str(item.get("filename") or "")[:300],
                    # Anything but https is dropped, so the studio can safely link to it.
                    "publicUrl": _public_https(item.get("publicUrl")),
                    "captions": [{"speaker": str(c.get("speaker") or "")[:200], "kind": "srt" if c.get("kind") == "srt" else "txt",
                                  "url": _public_https(c.get("url"))}
                                 for c in (item.get("captions") if isinstance(item.get("captions"), list) else [])[:MAX_CAPTION_FILES] if isinstance(c, dict)],
                })
        return Outcome("succeeded", external_id=external_id, data={"recordings": recordings})


def _entry_url(occ):
    """Where someone without their personal link goes to get in."""
    from django.conf import settings

    if not occ.waiting_room_code:
        return ""
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}/w/{occ.waiting_room_code}/"


def _public_https(value):
    if not isinstance(value, str) or len(value) > 1000 or not value.startswith("https://") or any(c in value for c in "\r\n\t "):
        return ""
    return value


def _time(value):
    return value if isinstance(value, str) and _TIME.match(value) else ""


def _short_list(items, fields, limit=50):
    rows = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            rows.append({k: (item.get(k)[:1000] if isinstance(item.get(k), str) else item.get(k)) for k in fields if isinstance(item.get(k), (str, int, bool))})
    return rows[:limit]


def _engagement_row(row):
    count = row.get("chatMessages")
    survey = row.get("survey")
    return {
        "chatMessages": count if isinstance(count, int) else 0,
        "chat": _short_list(row.get("chat"), ("at", "text")),
        "questions": _short_list(row.get("questions"), ("at", "text", "answered", "answer")),
        "polls": _short_list(row.get("polls"), ("question", "answer")),
        "survey": {str(k)[:500]: str(v)[:1000] for k, v in survey.items()} if isinstance(survey, dict) else {},
    }


def _attendance_row(item):
    if not isinstance(item, dict):
        return None
    identity = scalar_id(item, "externalId")
    if not identity:
        return None
    duration = item.get("durationSeconds")
    return {
        "external_id": identity,
        "email": "",
        "name": "",
        "joined_at": _time(item.get("joinedAt")),
        "left_at": _time(item.get("leftAt")),
        "duration_seconds": duration if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0 else None,
    }
