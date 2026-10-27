# Management API (event writes)

Server-to-server API for a CMS or back office to manage events. Implementation: `webinars/api/management.py`. Routes: `webinars/urls.py`.

## Authentication and scopes

Send the key as `Authorization: Bearer wbk_<prefix>_<secret>`. `X-Api-Key` also works. Issue keys with `manage.py create_api_key`.

| Scope | Grants |
|---|---|
| `content_write` | events (content), hosts, topics, form versions, occurrences, cancellation |
| `publication` | event visibility changes (also needs `content_write`) and recording publication; confirmed, see [Authentication](api-reference.md#authentication) and below |
| `attendance_write` | `POST /api/v1/manage/registrations/<id>/attendance` and the attendance import routes under `/api/v1/manage/occurrences/<id>/attendance/imports` (reads also with `config_read`) |
| `config_read` | **read-only**: `GET` on `/api/v1/manage/events`, `/events/<id>`, `/occurrences/<id>` and `/occurrences/<id>/waiting-room` (see below) |

**Read-only access (`config_read`).** The scope is checked per method: `config_read` is
accepted only for `GET` on the four endpoints above. Every other method needs the write
scope, so a read-only key gets `403` before any body is parsed. Responses are the same
projection as for `content_write`, including **draft** events and `internalName`, except
that every `url` key (today only `waitingRoom.url`, the shareable `/w/<code>/` room link)
is `null` unless the key also holds `content_write`. Provider join and waiting-room links
are never returned to anyone. MCP's in-process management calls still require
`content_write`. `config_read` is non-privileged and can be delegated by a `keys_admin`
key that holds it, so treat it as able to read unpublished copy.

`publication` is privileged: only a superuser (studio) or `manage.py create_api_key` can grant it.

Scope names follow the existing snake_case `ApiKey.Scope` convention. That means `content_write`, not `content:write`.

- No other scope grants write access, including `config_read`, `trusted`, `proxy`, `register`, `public_read` and `reporting`.
- The two write scopes do not imply each other.
- Existing keys are not upgraded (see migration `0003_management_api_scopes`, which changes no schema).

Status codes:

- `401 unauthorized`: the key is missing, invalid or revoked.
- `403 forbidden`: the key lacks the required scope. The scope check runs before the method check.
- `405 method_not_allowed`: the method is not supported. The response includes an `Allow` header.
- `415`: the request is not `Content-Type: application/json`.
- `413`: the body is larger than 64 KB.

Errors use the same body as the rest of the API: `{"error":{"code","message","fields"?},"fieldErrors"?}`.

- `400 validation_error`: field-level messages in `error.fields`, keyed by the camelCase input name. `_` means a cross-field error.
- `404 not_found`
- `409 conflict`

All responses are `Cache-Control: no-store`.

## Input rules

- The body must be a JSON object.
- Each resource accepts only the camelCase keys listed below. Any other key returns 400 `Unknown field.`. Nothing is assigned from request keys dynamically.
- Types are strict: strings are not coerced, `null` is rejected unless listed as allowed, and lists must contain strings only.
- Validation reuses the admin code paths:
  - `EventForm` / `Event.clean`: https hero URL, list of learning outcomes, unique slug, thank-you redirect only to hosts in `THANK_YOU_REDIRECT_HOSTS`;
  - `OccurrenceForm`: IANA timezone, DST gap rejected, DST overlap requires `dstFold`, https join/waiting URLs.

## Projection (what responses contain)

Responses never contain:

- join or waiting-room URLs (only `hasJoinUrl` / `hasWaitingRoomUrl`);
- person or registration profile data.

The public `/api/v1/events` projection is unchanged. Draft events stay 404 there.

**Event:** `id, revision, internalName, slug, publicTitle, summary, heroImageUrl, attention, interest, decision, action, learningOutcomes[], hosts[] (slugs), topics[] (slugs), visibility, formVersion, thankYouMode, thankYouUrl, thankYouMessage, createdAt, updatedAt, occurrences[]`.

**Occurrence:** `id, eventId, seriesId, timezone, localStart (wall clock, YYYY-MM-DDTHH:MM:SS), dstFold, durationMinutes, startsAt, endsAt (UTC, Z), status, registrationOpen, hasJoinUrl, hasWaitingRoomUrl, recordingUrl (null until published), recordingPublishedAt, icsSequence, scheduleGeneration, cancelledAt`.

## Endpoints

### Events (`content_write`)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/manage/events` | `{"results":[Event…]}`, newest 200 |
| POST | `/api/v1/manage/events` | 201 Event |
| GET | `/api/v1/manage/events/<uuid>` | Event |
| PATCH | `/api/v1/manage/events/<uuid>` | 200 Event |
| DELETE | `/api/v1/manage/events/<uuid>` | 204 |

**POST** (create):

- Writable keys: `internalName, slug, publicTitle, summary, heroImageUrl, attention, interest, decision, action, learningOutcomes, hosts, topics, visibility, formVersion, thankYouMode ("message"|"redirect"), thankYouUrl, thankYouMessage`.
- Required: `internalName, slug, publicTitle, formVersion`.
- `hosts` and `topics` are lists of existing slugs. `formVersion` is an existing version string.
- Draft first: `visibility` may only be omitted or `"draft"`.
- Creates revision 1 with an `EventRevision` snapshot and writes audit `event.created` (actor is the API key).

**PATCH** (update):

- Partial; same keys as create.
- Publish, unlist or archive by setting `visibility` to `public`, `unlisted` or `draft`. A visibility change needs the `publication` scope as well (else 403) and two steps:
  1. The request returns `428` with `preview` and `confirmToken`; nothing changes.
  2. Repeat the identical request with header `Webinar-Confirm-Token: <confirmToken>` (single use, 10 minutes, bound to the key, the exact body and the event revision).
- Publishing (`public`/`unlisted`) is refused with `409 not_ready` when the patched event has no upcoming session, no Attention hook or summary, or no Action copy. The studio, MCP and Django admin use the same check (`services.publish_problems`).
- If anything changed: `revision` is incremented under a row lock, a new `EventRevision` snapshot is stored and audit `event.revised` is written. A no-op PATCH does not create a revision.

**DELETE** returns 409 unless the event is `draft` and no occurrence has any registration, including cancelled ones. To retire a published event, PATCH its visibility instead.

### Occurrences (`content_write`)

| Method | Path | Body |
|---|---|---|
| POST | `/api/v1/manage/events/<uuid>/occurrences` | create; required `timezone`, `localStart` |
| GET | `/api/v1/manage/occurrences/<uuid>` | |
| PATCH | `/api/v1/manage/occurrences/<uuid>` | partial update |
| POST | `/api/v1/manage/occurrences/<uuid>/cancel` | `{}` |
| POST | `/api/v1/manage/occurrences/<uuid>/recording` | `{"recordingUrl":"https://…"}` |
| GET, PATCH | `/api/v1/manage/occurrences/<uuid>/waiting-room` | `enabled` (bool), `admissionLeadMinutes` (int), `admissionMode` (`auto`\|`hold`\|`open`\|`ended`). Response: `{enabled, url, admissionMode, admissionLeadMinutes, state, attendanceFinalizedAt}`; `url` is the shareable `/w/<code>/` room. 409 on cancelled occurrences. See [waiting-room.md](waiting-room.md). |

Writable keys for occurrence create and PATCH:

- `seriesId` (string id of a series of the same event, or `null`)
- `timezone`
- `localStart` (`YYYY-MM-DDTHH:MM[:SS]`, no offset)
- `dstFold` (`0`, `1` or `null`)
- `durationMinutes` (int ≥ 1, default 60)
- `registrationOpen` (bool)
- `joinUrl`, `waitingRoomUrl` (https; write-only)

`recordingUrl` is not writable here; use the recording endpoint.

**Rescheduling:** a PATCH that changes `timezone`, `localStart`, `dstFold` or `durationMinutes` runs `scheduling.reschedule_occurrence`. It:

- bumps `icsSequence` and `scheduleGeneration`;
- marks pending reminders `superseded` and creates new-generation reminders, so late ones are `skipped_late`;
- enqueues `occurrence.rescheduled` per active registration.

**Cancelled occurrences:** any non-empty PATCH and any recording publication return 409.

**Cancel** runs `scheduling.cancel_occurrence`. It:

- closes registration;
- bumps `icsSequence`;
- suppresses pending reminders;
- enqueues `occurrence.cancelled`.

Cancel is idempotent: repeating it causes no further bump or notification.

**Recording** requires the `publication` scope (not `content_write`) and an https URL, and is confirmed the same way as visibility changes (428, then the identical request with `Webinar-Confirm-Token`). It runs `scheduling.publish_recording`, which enqueues `recording.published`.

### Hosts, topics, form versions (`content_write`)

| Method | Path | Keys |
|---|---|---|
| POST | `/api/v1/manage/hosts` | `name`*, `slug`*, `title`, `bio`, `imageUrl` (https) |
| PATCH | `/api/v1/manage/hosts/<slug>` | same, partial |
| POST | `/api/v1/manage/topics` | `name`*, `slug`* |
| PATCH | `/api/v1/manage/topics/<slug>` | same, partial |
| POST | `/api/v1/manage/forms` | `version`*, `requiredFields`*, `optionalFields`, `fieldMapping`, `customFields`, `defaultPhoneRegion`, `smsDisclosureVersion`, `smsDisclosureText`, `isActive` |
| PATCH | `/api/v1/manage/forms/<version>` | `description`, `isActive` only |

\* required.

Form version rules:

- `requiredFields` and `optionalFields` must be canonical keys (`email, phone, firstName, lastName, name, company, jobTitle, companySize`) and must not overlap.
- `fieldMapping` maps an alias to a canonical key.
- Form versions are immutable contracts. Creating an existing version returns 409, and changing schema keys returns 400 `This field cannot be changed.`. Publish a new version instead.

### Attendance (`attendance_write`)

`POST /api/v1/manage/registrations/<uuid>/attendance` with body `{"kind":"attended"|"no_show"|"follow_up"}`.

- Runs `scheduling.record_attendance`. It appends an `AttendanceEvent` with `source="api"`, updates `attendance` for attended/no_show, and enqueues `attendance.recorded` or `follow_up.requested`.
- Returns 409 if:
  - the registration is cancelled;
  - the occurrence is cancelled;
  - `attended`/`no_show` is sent before `startsAt`.
- Attendance is never inferred.
- Response: `{"registrationId","occurrenceId","attendance"}` only.

#### Attendance imports (`attendance_write`; reads also `config_read`)

Bulk attendance from the hosting provider or a report, reviewed and applied explicitly
([hosting.md → Attendance import](hosting.md#attendance-import)). Base path
`/api/v1/manage/occurrences/<uuid>/attendance/imports`:

| Method | Path | Body / effect |
|---|---|---|
| GET | `…/imports` | imports of the session: counts and state, no rows |
| POST | `…/imports` | `{"source":"provider"}` (one read-only pull from the linked Zoom/Riverside session; 409 when the provider has no `attendanceImport`, the session is not linked or the account is disabled; a failed pull is returned as 201 with `state: failed`, safe to retry) or `{"source":"file","rows":[{email,name,externalId,joinedAt,leftAt,durationSeconds}]}` (max 5,000 rows). Rows are matched (provider id, then email) and merged per person; nothing is recorded yet. |
| GET | `…/imports/<importId>` | the import with its rows: `match` (`matched`/`unmatched`/`ambiguous`/`ignored`/`resolved`), `emailMasked`, `name`, `registrationId` |
| POST | `…/imports/<importId>/apply` | `{}`; first call 428 with a preview and a single-use `confirmToken` bound to the import's state, then repeat with `Webinar-Confirm-Token`. Records `attended` (`record_attendance`, `source="import:<source>"`) for matched/resolved rows whose registration is not already attended; re-applying records nothing new. |
| POST | `…/imports/<importId>/rows/<rowId>/resolve` | `{"registrationId": …}` (active registration of this session; recorded at once) or `{"ignore": true}`; 409 for rows already matched/resolved/ignored |
| POST | `…/imports/<importId>/no-shows` | `{}`; 428 + confirm as for apply. Only after apply and after the session ended; marks remaining active registrations `no_show`, never those who checked in through the waiting room (`noShows.refused`). |

Unknown and other-session import/row ids are 404. Responses never contain full email
addresses, join links or provider payloads.

## Audit

Every write is recorded in `AuditLog` with `actor_api_key`, the object id and field names only:

- `event.created|revised|deleted`
- `host.saved`, `topic.saved`, `formversion.saved`
- `occurrence.api_write`
- `attendance.api_write`
- `attendance_import.created|pull_started|pull_finished|lost|late_outcome|applied|row_resolved|no_shows` (ids and counts only)

The shared scheduling functions also write their own entries (`occurrence.cancelled|rescheduled`, `recording.published`, `attendance.recorded`) with no actor, because `webinars/scheduling.py` does not accept an API key.

## Not supported

- Series creation (use admin).
- Deleting hosts, topics, forms or occurrences.
- Bulk operations.
- Optimistic concurrency tokens. Writes are serialized by row locks, and the last write wins.
- Idempotency keys for management writes.
- Rate limiting.
