# Hosted waiting room

Optional per session (occurrence). Code: `webinars/waiting_room.py` (logic), `webinars/waiting_room_views.py` (HTTP), templates in `webinars/templates/waiting_room/`.

## What attendees get
A stable URL `/w/<code>/` (shown in the studio session page, management API `waitingRoom.url`, MCP). Share this link in invites instead of the provider link. The code survives reschedules and disabling/re-enabling.

## Admission (server-authoritative)
State is computed on every request, in this order: `cancelled` → `ended` (End pressed, or scheduled end passed; manual Open keeps doors open up to 6 h past the end) → `hold` (overrides the clock) → `open` (Open pressed, or `admission_lead_minutes` before start) → `waiting`. `preparing` = would be open but no join destination is set. The destination (`join_url`) is read at the moment of entry, so it can be changed until the last minute.

The browser polls `GET /w/<code>/status` (no PII, no link). When it reports `admit`, the page submits a CSRF-protected `POST /w/<code>/enter`; the server re-checks state and registration and answers `303` with the join link **only in `Location`**. Late, validated arrivals are admitted immediately on page load.

**Disclosure limit (no false promise):** once a browser is redirected, the attendee has the provider link and can share it. The room prevents distribution in invites, page source and APIs; it cannot make a link secret after it is used. Use provider-side controls (registration-required meetings, passcodes) if that matters.

## Identity
- **Recognized browser**: holds a valid capability cookie (`CAPABILITY_COOKIE_NAME`). This is device recognition, not email verification. Registered → welcome + countdown. Recognized but not registered → only the missing form fields (keys, never values). "Not you?" revokes the cookie.
- **Unknown browser**: email first. `POST identify` always returns the same 303 and sends the same email for known and unknown addresses; no person lookup happens before verification (no enumeration by response or timing). The email has a 6-digit code and a one-time link. Codes: HMAC-hashed, 15 min TTL, single use, bound to the requesting browser (`wr_pending` cookie hash) and occurrence, max 5 guesses. Links: SHA-256 stored, single use, consumed only by POST (mail scanners' GETs do nothing).
- **Code limits** (database-backed, global across workers; extra requests look identical and send no email):
  - per address per room: `WAITING_ROOM_MAX_CODES_PER_EMAIL` (default 3) per TTL window;
  - per address across all rooms: `WAITING_ROOM_MAX_CODES_PER_EMAIL_GLOBAL` (default 10) per TTL window. The identity cookie is platform-wide, so this bounds total guesses per address platform-wide (codes × 5 guesses) instead of per room;
  - guesses per (browser, address) across all rooms: `WAITING_ROOM_MAX_GUESSES_PER_EMAIL` (default 10) per TTL window. Keyed by the guessing browser, so someone guessing at another person's address never locks that person's own browser out.
  - Suppression never supersedes codes already issued: someone flooding requests for your address cannot cancel the code you already have. Residual risk: they can exhaust the issuance caps so you cannot get a *new* code for up to one TTL window (15 min); the per-IP limit on `identify` and the proxy limits make this costly.
- **Emailed link in another browser (login CSRF)**: opened in the browser that asked for the code (matching `wr_pending` cookie), the link shows a plain Continue. Opened anywhere else, the page shows the address being verified and asks "This is me, continue" or "Not me"; nothing is submitted automatically, and only the CSRF-protected POST from that page spends the link. This stops a planted link (for an attacker's own address) from silently signing a victim's browser in as the attacker.
- After verification the mailbox's person gets a new capability (created if new). New visitors then fill the rest of the configured form; registration goes through the normal registration service (`register(..., allow_started=True)` for late walk-ins until the room ends).

## Check-in and attendance
`CheckIn` is unique per (occurrence, registration). The row, `AttendanceEvent(kind=checked_in)` and `attendance.checked_in` outbox messages commit in one transaction; a concurrent duplicate hits the unique constraint and produces nothing. Reschedule bumps `admission_generation` and resets the mode to automatic; a later entry updates the same row and emits one event for the new generation. Cancellation closes the room. Revoked/expired capabilities and cancelled registrations are never admitted.

After the window closes (End or scheduled end) the worker (`run_worker`) or the End button finalizes once: current check-ins → `attended`, other active registrations with unknown attendance → `no_show`. Recorded attendance is never overwritten, and `no_show` cannot be recorded for a checked-in registrant. Caveat: if a webhook destination has `include_private_links` and you mail the join link directly, absence of a check-in is not proof of absence.

**Finalization is final for the run (admission generation).** Once attendance is finalized:
- the room is `ended` regardless of mode or clock, and nobody is admitted;
- Open / Hold / Auto are refused (`ValidationError` code `attendance_finalized`; Studio shows it as an error message, the management API answers 409). End again is a no-op;
- to run the session again, **reschedule** it. A reschedule starts a new generation: mode back to automatic, `attendance_finalized_at` cleared, every registration's attendance reset to `unknown`, and unsent `attendance.recorded` messages for the old run are cancelled (`last_error=superseded_by_reschedule`) so no follow-up (e.g. a no-show campaign) is driven by stale state. The `occurrence.rescheduled` message carries the reset attendance. Delivered or in-flight messages are not recalled. The new run is finalized again when it ends.

**Check-in vs End:** `admit` and finalization both lock the occurrence row and `admit` re-checks the state under that lock, so a check-in either commits before finalization reads check-ins (→ `attended`) or sees the room ended. Finalization also treats a current check-in it finds as `attended` rather than failing the End.

## Rate limits
`webinars/ratelimit.py`: per-process, per-IP fixed windows (bounded LocMem cache). Not global. **Production must also rate-limit `/w/` and `/mcp` at the reverse proxy.** Set `RATE_LIMIT_TRUST_X_FORWARDED_FOR=1` only behind trusted proxies that append to `X-Forwarded-For`, The number of trusted proxies is the Django setting `RATE_LIMIT_TRUSTED_PROXY_COUNT` (default 1). It is configurable through the environment (1–10; see [operational settings](operational-settings.md)): the client address is the Nth entry from the right (everything further left is client-controlled and ignored). `ratelimit.hit_key(action, key)` counts against an arbitrary identifier (e.g. an API key id, bucket `mcp_key`) instead of the address.

## Accepted risk: unverified registration reaches the room
Anyone can register on the public form with an unverified email, receive an identity cookie, and enter the room. Email verification inside the room does not make the join link more secret than registration already does; the room controls *when* the link is disclosed and records check-ins, it is not an access-control boundary for the meeting. Use provider-side controls if the meeting itself must be restricted.

## Email
Verification needs outgoing email (docs/email.md). With delivery disabled, unknown browsers cannot verify; the room says so.
