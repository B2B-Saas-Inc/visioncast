# Hosting providers

Link a session (occurrence) to the platform that runs it live. Attendees always use the
stable waiting-room link (`/w/<code>/`); the provider's attendee link stays private and is
only used as the redirect target when the waiting room admits a registrant. The host/start
link is stored encrypted and is never shown anywhere (only `hasHostUrl`).

Code: `webinars/hosting/` (adapters + `service.py`), REST `webinars/api/hosting.py`, studio
`webinars/studio/views_hosting.py`.

## Capability matrix

| Provider | Create | Update | Attach existing | Per-attendee links | Manual link | `attendanceImport` | Contract |
|---|---|---|---|---|---|---|---|
| Zoom Webinars | yes | yes (PATCH) | yes (GET) | no | no | yes (`past_webinars/{id}/participants`) | documented, **not sandbox-verified** |
| Wistia Webinars | yes | no | no | no | no | no (file import) | documented, **not sandbox-verified** |
| Sequel (Introvoke) | yes | no | no | no | no | no (file import) | documented, **not sandbox-verified** |
| Riverside | no (not documented) | no | yes | yes (registrant join links) | no | yes (**UNVERIFIED** registrant list) | **unverified** paths/fields |
| StreamYard | no | no | no | no | yes | no (file import) | no public API; zero outbound calls |

Attendance for providers without `attendanceImport` arrives through the file import
(see [Attendance import](#attendance-import)); it is never simulated. Teams and Google Meet
are not supported. "No" means the action is refused with a clear message, never simulated.

Every vendor contract here needs verification against a real sandbox account before
production use. Riverside endpoint paths (`platform.riverside.fm/api/v1/events/{id}` and
`.../registrants`) are our best reading and are constants in `webinars/hosting/riverside.py`.

## Setup (owner / Administrator)

1. The operator adds the provider's API hosts to `WEBHOOK_HOST_ALLOWLIST`
   (Zoom: `zoom.us`, `api.zoom.us`; Wistia: `api.wistia.com`; Sequel: `api.introvoke.com`;
   Riverside: `platform.riverside.fm`). Credentials are only ever sent to these pinned hosts.
2. Studio → Hosting providers → Connect a provider. Credentials are set-only, encrypted with
   `INTEGRATION_MASTER_KEY` and never shown again (blank keeps the stored value).
   - Zoom: Server-to-Server OAuth app (account ID, client ID, client secret) and the licensed
     host user ID or email (needs a Webinars licence).
   - Wistia: API token. Sessions must be at least 15 minutes.
   - Sequel: client ID/secret and company ID.
   - Riverside: API key; create the event in Riverside first, then attach its ID.
   - StreamYard: no credentials. Optional `HOSTING_MANUAL_LINK_HOSTS` (comma list) allows
     extra embed hosts besides `streamyard.com`.

API: `hosting_admin` scope for accounts; `hosting_provision` for preview/execute/reconcile/
manual links; `config_read` may GET providers and an occurrence's hosting state. All three
scopes are superuser-granted and never delegable. Analysts never see these pages.

## Preview → confirm (never automatic)

Publishing an event never creates provider sessions. On a session's Hosting page (or
`POST /api/v1/manage/occurrences/{id}/hosting/preview`) you see exactly what will be sent
(credentials redacted). `POST .../hosting/execute` first answers 428 with the preview and a
single-use `confirmToken`; repeat with header `Webinar-Confirm-Token`. In the studio only
Administrators/superusers can confirm; Editors may preview.

The studio Confirm button carries a signed token issued by the preview render. It is bound to:

- the user, the session, the account, the action and the external id;
- the session's state and schedule generation.

It expires after 10 minutes. A bare POST, or a confirm after the session changed, is refused with "Preview again".

## Durable state and reconciliation

`execute` commits a `ProvisioningOperation` (in flight) before the call, calls the provider
outside any database lock (10 s per operation, 20 s total, 64 KB response cap, redirects
never followed), then records the outcome:

- success → session `linked`; attendee link → `Occurrence.join_url` (private); host link encrypted;
- definitive failure (4xx, auth, DNS, blocked host) → `failed`, safe to retry;
- unknown (timeout, 429/5xx, connection drop, unreadable 2xx) → `needs_reconciliation`.

None of these providers accept an idempotency key for create, so an unknown create is never
re-sent automatically. Check the provider, then record "It was created (ID …)" or "Nothing
was created" (`POST .../hosting/reconcile`). Updates (Zoom PATCH of the full schedule) are
idempotent and may be retried.

Rescheduling marks a linked session `out_of_date`; send an update (Zoom) or change the time
in the provider yourself. Cancelling a session, or unlinking it, never deletes or cancels the
external session.

**Lost operations.** An operation can stay "in flight" if the web request timed out, the worker was killed or a deploy happened mid-call, or recording the outcome failed.

- Any operation still in flight after 5 minutes (`STALE_IN_FLIGHT_SECONDS`) is treated as unknown.
- A lost create or attach moves the session to `needs_reconciliation`, so a person can reconcile it.
- A lost update keeps the link and marks the session `out_of_date`.
- This runs from the worker and before every preview, reconcile or unlink.
- Stale pending registrants likewise go to `needs_reconciliation`.

**Late outcomes are fenced.** An operation records its outcome on the session only while it still owns it: the operation and the session are both in flight and no newer operation exists. A response that arrives after the operation was given up, reconciled or superseded is noted on the operation (`[late outcome: …]`, audit `hosting.operation_late_outcome`) and never copies an ID, join link or host link onto the session. A late *success* of a create/attach on a session reconciled as "Nothing was created" (now `failed`, nothing else in flight) puts the session back in `needs_reconciliation`, so a retry cannot quietly create a duplicate. Late registrant outcomes are fenced the same way (by state and attempt number).

**Unlinking keeps the join link.** "Unlink" keeps `Occurrence.join_url`, because it may have been pasted by hand. Clear or replace it in session details if the old provider link must stop working.

## Riverside registrants

After attach, the worker (`service.sweep_registrants`) creates one registrant per active
registration and stores its unique join link encrypted. The waiting room admits each
registrant to their own link (`service.attendee_join_url`). An unknown outcome marks that
registrant for reconciliation; it is not re-sent. Failures retry up to 5 times.

Riverside links are enforced per registrant. While an attached Riverside session is linked (or out of date), someone without their own linked registrant link is not admitted. The shared `Occurrence.join_url` is never used as a fallback, so links cannot be passed on. Lookup errors also deny admission.

Doors (`service.has_join_destination`): a linked Riverside session does not need a shared `join_url`. Its room opens on the usual clock and controls (countdown, Open, Hold, End, cancel) as long as the Riverside hosting account is enabled. If the account is disabled, the room shows "preparing" and admits nobody, even if a shared `join_url` is set. Admission never re-enables the account and never calls Riverside.

At admission (`POST /w/<code>/enter`), the room checks everything it already checks: the doors are open, the browser is recognised, the registration is active, and a paid ticket has not been refunded or flagged for refund. Then it needs the registrant's own record, which must be:

- linked to this occurrence's session,
- bound to the session's *current* Riverside account and event ID (see below),
- for this occurrence's active registration,
- decryptable,
- an https link on `riverside.fm` / `riverside.com` with no credentials in it.

Anything else denies admission. The link only appears in the 303 `Location`. The status JSON and the room page never include it, and polling never records a check-in.

**Event binding.** One session row is reused across detach and re-attach, so each registrant record also stores the Riverside account and event ID it was made for (`event_account`, `event_external_id`). The binding is captured under the session lock *before* the registrant call. A link counts only while that binding equals the session's current account and event ID. Matching the event ID alone is not enough: the same ID on a different account does not match.

- **Re-attach to another event or account.** Earlier links stop admitting at once. The next recovery pass (`service.recover_stale_operations`, run by the worker sweep and by every reconcile request) moves those records to `needs_reconciliation` and wipes the old link. They are never re-sent automatically, because a registrant may already exist in the new event. Re-attaching the *same* event on the same account keeps the links.
- **Call in flight during a re-attach.** A result that arrives after the event changed is not stored. The record goes to `needs_reconciliation` with no link. A result from an older attempt is fenced by the attempt number as before.
- **Legacy records.** Records created before migration `0010` have no binding. They are denied and flagged the same way, and they are never backfilled. A person confirms them against the current event.
- **Reconciling binds the record to the current event**, under the session and row locks. `created` stores the pasted link for the current event. `not_created` marks the record `failed` for the current event, so the sweep may retry it there. The attempt count is never reset, so the 5-attempt cap still applies and late results from earlier attempts stay fenced. Reconciling is refused while the session is detached.

### Reconciling stalled registrants

Registrants stuck in `needs_reconciliation` (or pending with a lost attempt) are not admitted until a person checks Riverside and records what they found. No Django shell is needed. All three routes call `service.reconcile_registrant`, so the existing fencing, row locks and 5-attempt cap still apply.

- **created**: paste that registrant's *own* https Riverside link (riverside.fm / riverside.com). It is stored encrypted and is set-only: it is never returned, rendered or echoed in a preview.
- **not_created**: the registrant goes back to `failed`, and the worker sweep may retry it within the 5-attempt cap.

| Interface | List | Reconcile | Who |
|---|---|---|---|
| Studio | Session → Hosting → "Riverside registrants to check" (`/studio/sessions/<id>/hosting/registrants`) | per-row form | Administrators / superusers only; Editors and Analysts get 403 |
| REST | `GET /api/v1/manage/occurrences/{id}/hosting/registrants` | `POST …/hosting/registrants/reconcile` `{registrationId, decision, joinUrl?, externalId?}` | list: `hosting_provision` or `config_read`; reconcile: `hosting_provision` |
| MCP | `list_session_registrants` | `reconcile_session_registrant` (gated) | same scopes |

Guarantees:
- **Lists are read-only.** API/MCP lists return registration IDs, state, attempt count, `boundToCurrentEvent` and update time. They also include records bound to another event (or to none) that recovery has not flagged yet. Reconciling such a record first flags it, so a token issued while it still showed `linked` is refused: reload and confirm again. They never include names, emails or links. The studio list also shows a masked email (`j***@example.com`) to Administrators. Listing makes no provider call and writes nothing.
- **Confirmation is required.**
  - REST: the first POST returns 428 with a link-free preview and a single-use `confirmToken`. Repeat the identical body with `Webinar-Confirm-Token`.
  - MCP: call with `dryRun` (the default), then `dryRun: false` plus the token.
  - Each token is bound to the key and the exact arguments (the link only as an HMAC). It is also bound to the registrant's state, attempt number, update time and event binding, and to the session's account, event ID, mode, state and update time. Any new attempt, outcome, reconciliation, detach or re-attach invalidates it. `reconcile_registrant(expected_attempts=…, expected_binding=…)` re-checks the attempt and the current event under the locks. The preview shows `providerEventId`, `providerAccountId` and `boundToCurrentEvent` so you can check which event you are confirming for.
  - Studio: forms carry CSRF and a signed state token bound to the user, session, registrant and that same state, valid for 10 minutes.
- **Refusals.**
  - A registration ID from another session gets 404, the same as an unknown ID.
  - A registrant that is not Riverside, or not in `needs_reconciliation`, gets 409.
  - An invalid link or ID gets 400, and the token is not consumed.
- **Nothing is sent to Riverside.** These routes only record your finding. They do not check the link against Riverside or import attendance. The Riverside contract itself is still unverified against a sandbox (see the provider table).

## Attendance import

Who attended is never inferred and never pulled automatically. A person (or an
`attendance_write` key) asks for an import, reviews the matches, and applies it; only then
is attendance recorded, through the same `scheduling.record_attendance` path as the
management API and the waiting room, so `attendance.recorded` is delivered once per
registration. Code: `webinars/attendance_import.py` (pipeline), `webinars/api/attendance_imports.py`
(REST, shared by MCP), studio `session_attendance` in `webinars/studio/views_hosting.py`.

### Sources

| Source | How | Contract |
|---|---|---|
| Zoom (`provider`) | `GET https://api.zoom.us/v2/past_webinars/{webinarId}/participants?page_size=100[&next_page_token=…]`, Bearer token from the Server-to-Server app; scope `webinar:read:admin` (`webinar:read` for user-level apps); needs a Pro-or-higher account with the Webinar add-on. Fields read: `id`, `user_id`, `user_email`, `name`, `join_time`, `leave_time`, `duration` (seconds); pagination `next_page_token` (expires after 15 minutes). Pages are capped at 50 (`ATTENDANCE_MAX_PAGES`); the page size is 100 instead of Zoom's maximum 300 so one page fits the transport's 64 KB response cap. A capped list is stored with `complete: false`. | Zoom API reference, Webinars → "List webinar participants": https://developers.zoom.us/docs/api/webinars/#tag/webinars/GET/past_webinars/{webinarId}/participants. The reference site renders in the browser only (the page returns 404 to non-browser fetches), so path, scopes, `page_size` / `next_page_token` and the participant fields were cross-checked against the OpenAPI-derived listing at https://docs.rs/zoom-api/latest/zoom_api/webinars/struct.Webinars.html and Zoom's own (legacy) published reference https://zoom.github.io/api/ (participant report fields). **Not sandbox-verified.** |
| Riverside (`provider`) | `GET https://platform.riverside.fm/api/v1/events/{eventId}/registrants` (Bearer API key), reading an attended/joined marker (`attended`, `joined`, `has_joined`, `has_attended`, or a `joined_at` / `join_time` value) per registrant, with `id`, `email`, `name` / `first_name` + `last_name`, optional `left_at` / `duration`; pagination `next_page_token` → `?page_token=` (cap 20 pages). Constants in `webinars/hosting/riverside.py` (`ATTENDANCE_*`). | **UNVERIFIED** path, field and marker names (same status as the other Riverside paths). |
| Wistia, Sequel, StreamYard | `attendanceImport: false`: a provider pull is refused with "does not support attendance import here: import a file instead". | n/a |
| File (`file`) | JSON rows over REST/MCP (`email`, `name`, `externalId`, `joinedAt`, `leftAt`, `durationSeconds`) or a CSV upload in the studio. CSV headers are tolerated for Zoom participant/attendee reports (`Attended`, `User Name (Original Name)`, `First Name`/`Last Name`, `Email`, `Join Time`, `Leave Time`, `Time in Session (minutes)`, `Duration (Minutes)`), Wistia and StreamYard exports (`Name`, `Email Address`, `Duration`, `Join time`/`Leave time`); a report preamble before the header line is skipped; rows with `Attended = No` are dropped; `H:MM:SS` durations and minute columns are converted to seconds (a bare `duration` column is read as minutes unless the header says seconds). Limits: 1 MB, 5,000 rows, parsed server-side; the raw file is never stored. | n/a |

### Pipeline

- **One run = one `AttendanceImport`** (occurrence, source `zoom` / `riverside` / `file`, state
  `pending` → `in_flight` → `needs_review` → `applied`, or `failed`; counts; who asked; for a
  pull, the `ProvisioningOperation` with action `attendance`) and its deduplicated
  `AttendanceImportRow`s (provider id, normalised email key, masked email `j***@example.com`,
  display name, joined/left/duration, match state, matched registration). Raw provider
  payloads and join links are never stored. Migration `0011_attendance_import`.
- **Matching.** Provider id ↔ `ProviderRegistrant.external_id` of the same session first,
  else email key ↔ `Person.email_key` of *active* registrations of that occurrence. Several
  candidates (or an id and an email that point at different registrations) → `ambiguous`;
  none → `unmatched`. Never a guess. Rows for the same person in one run are merged:
  durations summed, earliest join, latest leave.
- **Pulls are durable and read-only.** Step A commits the in-flight operation and import,
  step B calls the provider outside any lock (same bounded transport as provisioning), step C
  records the outcome. A failed, timed-out or unparsable pull leaves the import `failed` and
  the operation `failed` (never `unknown`: nothing was changed at the provider, so a retry is
  always safe and there is nothing to reconcile). An in-flight pull older than 5 minutes is
  recovered as `failed` by the same recovery pass as other operations; a response that
  arrives after that is fenced (noted on the operation, rows never stored, audit
  `attendance_import.late_outcome`). The session's link state is untouched by pulls.
- **Apply** (preview → confirm). The preview counts matched / unmatched / ambiguous rows,
  what will be recorded and what is already attended. Confirming records `attended` for every
  matched or resolved row whose registration is active and not already `attended`
  (`record_attendance(..., source="import:<source>")`); already-attended registrations and
  already-applied rows are skipped, so applying twice or importing the same list again never
  re-emits `attendance.recorded`. Unmatched and ambiguous rows stay for review.
- **Review.** Resolve an unmatched/ambiguous row to an active registration of the same
  occurrence (attendance is recorded at once, same path) or ignore it. Rows already matched,
  resolved or ignored cannot be changed.
- **No-shows** (preview → confirm; separate from apply). Only after the import is applied,
  the session has ended (`waiting_room.room_state` says `ended`) and once per import. If the
  session has a waiting room that is not finalized yet, `waiting_room.finalize_attendance`
  runs first (current check-ins become `attended`, the rest `no_show`, its own audit). Then
  every remaining active registration without attendance becomes `no_show` via
  `record_attendance`; registrants it refuses because they checked in through the waiting
  room are counted as `refused` and left alone.
- **Audit** (ids and counts only): `attendance_import.created`, `.pull_started`,
  `.pull_finished`, `.lost`, `.late_outcome`, `.applied`, `.row_resolved`, `.no_shows`; plus
  the usual `attendance.recorded` per registration.
- **Retention.** `purge_personal_data` blanks the email key, masked email, display name and
  provider id of rows older than `OUTBOX_PAYLOAD_RETENTION_DAYS` (30) and, on person erasure,
  of rows matched to that person's registrations or carrying their email key. Import counts
  and match states stay.

### Interfaces

| Interface | Create | Read | Apply | Resolve | No-shows | Who |
|---|---|---|---|---|---|---|
| Studio | Session → Hosting → "Attendance" (`/studio/sessions/<id>/attendance`): "Pull from provider" (only when the linked provider is capable; the request is shown with credentials redacted) and CSV upload | imports list, selected import's rows with masked emails | button with a signed state token (10 minutes, bound to user, session, import and its state) | per-row form: registration (masked emails) or Ignore | button with a signed state token | Administrators / superusers only; Editors and Analysts get 403 |
| REST | `POST /api/v1/manage/occurrences/{id}/attendance/imports` `{source: "provider"}` or `{source: "file", rows: [...]}` → 201 (a failed pull is still 201 with `state: failed`) | `GET …/attendance/imports` (counts), `GET …/attendance/imports/{importId}` (rows) | `POST …/{importId}/apply` (428 + `confirmToken`, then `Webinar-Confirm-Token`) | `POST …/{importId}/rows/{rowId}/resolve` `{registrationId}` or `{ignore: true}` | `POST …/{importId}/no-shows` (428 + confirm) | writes: `attendance_write`; reads: `attendance_write` or `config_read` |
| MCP | `import_session_attendance` (gated; dry run shows the provider request or the matching counts, writes nothing) | `list_attendance_imports` | `apply_attendance_import` (gated) | `resolve_attendance_row` (gated) | `mark_session_no_shows` (gated) | same scopes |

Confirmation tokens (REST and MCP) are bound to the key, the exact arguments and the
import's state (state, update time, applied/ignored counts, no-show pass), so any resolve,
apply or outcome in between invalidates them. A provider pull additionally requires an
enabled hosting account linked to the session; credentials are never returned or rendered.
Other-occurrence import or row ids get 404; state conflicts get 409 and consume no token.
Responses and pages never contain full email addresses, join links or provider payloads.
