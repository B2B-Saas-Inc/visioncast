# API reference

Machine-readable versions: the canonical OpenAPI 3.1 document served at
`GET /api/v1/openapi.json` (generated from `webinars/openapi.py`; `docs/openapi.yaml`
is superseded) and
[`registration.schema.json`](registration.schema.json) (JSON Schema 2020-12 for the
registration body). Architecture and identity rules: [`architecture.md`](architecture.md).
Browser/GTM integration and the example proxy: [`browser-integration.md`](browser-integration.md).

> Status: preview. The registration, profile, reset and error contracts are fixed by
> `docs/architecture.md` and covered by the test suite (final PostgreSQL run pending).
> Event field lists below follow `webinars/api/views.py` (`serialize_event`), which is
> authoritative. Event writes: [`management-api.md`](management-api.md).
> None of the endpoints on this page is rate-limited by the application; limits belong at
> the edge. (Only the waiting room `/w/…`, `/mcp`, studio login and invitation acceptance
> have coarse per-process limits — `webinars/ratelimit.py`.)

## Conventions

* JSON in and out (`Content-Type: application/json`), UTF-8. Timestamps are ISO 8601
  UTC instants ending in `Z`; each occurrence also carries its IANA `timezone`.
* All error responses use one envelope:

  ```json
  {"error": {"code": "validation_error", "message": "…", "fields": {"email": "Enter a valid email address."}},
   "fieldErrors": {"email": "Enter a valid email address."}}
  ```

  `fieldErrors` mirrors `error.fields` for the browser SDK and is present only when
  there are field errors. The example proxy strips messages (see `integration.md`).

  | HTTP | `code` | Meaning |
  |---|---|---|
  | 400 | `validation_error` | Body/query invalid. `fields` maps keys (canonical, `custom.<key>`, or top-level) to messages. Submitted values are never echoed. |
  | 401 | `unauthorized` | API key missing, malformed, unknown or revoked. |
  | 403 | `forbidden` | Key lacks the required scope. |
  | 403 | `origin_rejected` | Same-origin CSRF checks failed (see below). |
  | 404 | `not_found` | Unknown or non-public event/occurrence/form version. |
  | 405 | `method_not_allowed` | Wrong method for the route. |
  | 409 | `idempotency_conflict` | `idempotencyKey` reused with a different body, or replayed from another browser's capability. |
  | 410 | `registration_closed` | Occurrence cancelled, closed for registration, or already started/finished. |
  | 413 | `payload_too_large` | Body over 64 KB. |
  | 415 | `unsupported_media_type` | POST body is not `application/json`. |

  A `429` can only come from your edge proxy; the application does not rate-limit.

## Authentication

API keys are created with `python manage.py create_api_key --name <label> --scope <scope>`
(repeat `--scope` to grant several). The raw key, format `wbk_<prefix>_<secret>`,
is printed once; only the SHA-256 of the full key is stored. Send it as:

* `Authorization: Bearer wbk_…` (preferred), or
* `X-Webinar-Proxy-Key: wbk_…`, or `X-Api-Key: wbk_…`.

| Scope | Allows |
|---|---|
| `public_read` | No REST endpoint requires it; event reads need no key. Used by MCP read tools and resources ([`mcp.md`](mcp.md)). |
| `register` | `POST /api/v1/registrations` from a CMS back end. No browser cookie handling. |
| `proxy` | Everything `register` does plus `GET /api/v1/profile`, `GET /api/v1/my-registration` and `POST /api/v1/reset`, with the browser capability cookie forwarded. Use this for the first-party proxy in `sdk/proxy/`. |
| `trusted` | Registration from a trusted server; `organization.externalIds` are stored as `trusted_api` and `verified=true`. **Privileged:** granted only by a superuser or `manage.py create_api_key`. |
| `content_write` | Management API: events, hosts, topics, forms, occurrences ([`management-api.md`](management-api.md)). Not visibility changes or recordings. |
| `attendance_write` | Management API: attendance only. |
| `reporting` | No REST endpoint uses it. The MCP `get_report_summary` tool uses it; there are also admin and studio reports. |
| `publication` | **Privileged.** REST: event visibility changes (together with `content_write`) and `POST /api/v1/manage/occurrences/<id>/recording`, both confirmed (below). MCP: `set_event_visibility`, `publish_recording`. |
| `config_read` | REST: read-only `GET` on management events (drafts included), sessions and waiting rooms, with `waitingRoom.url` null; never a write ([`management-api.md`](management-api.md)). MCP: redacted configuration ([`mcp.md`](mcp.md)). |
| `delivery_admin`, `keys_admin`, `team_admin` | **Privileged**, MCP only; see [`mcp.md`](mcp.md). `team_admin` delegates human authority: see its section there. |

No scope implies another; in particular no read or registration scope grants writes. Privileged scopes (`trusted`, `publication`, `delivery_admin`, `keys_admin`, `team_admin`) are granted only by a superuser in the studio or by `manage.py create_api_key` from a trusted shell; Administrators and other keys cannot grant them.

**Publication confirmation (REST).** A request that changes an event's visibility, or publishes a recording, first returns `428` with `{"error": {"code": "confirmation_required"}, "preview": {...}, "confirmToken": "..."}` and changes nothing. Repeat the identical request with header `Webinar-Confirm-Token: <confirmToken>` within 10 minutes. The token is single-use and bound to the key, the exact body and the target's state (a change in between gives `400 confirmation_mismatch`). Publishing an event that is not ready (no upcoming session, no Attention/summary, no Action copy, checked on the patched result) returns `409 not_ready` with no token. The token shows the same key asked twice; it is not independent human approval.

Never put an API key in GTM, page source or any browser-delivered asset. The browser only
ever talks to a first-party path (`/webinar-api/*`) on its own origin.

## Public endpoints

### `GET /api/v1/events`

Query: `q` (title/summary text, max 200 chars), `topic` (topic slug), `host` (host slug),
`when=upcoming|recorded`, `limit` (1–50, default 20), `offset` (≥ 0, default 0).
Only `public` events are listed (never `draft` or `unlisted`). Ordered newest first
(`-created_at`); `count` is the total match count. Invalid filters/pagination → `400`.

```json
{"count": 1, "limit": 20, "offset": 0, "results": [Event]}
```

Each `Event` (same shape as the detail endpoint, `webinars/api/views.py`
`serialize_event`): `slug, title, summary, heroImageUrl, aida{attention,interest,decision,action},
learningOutcomes[], hosts[{name,slug,title,imageUrl}], topics[{name,slug}], formVersion, cta,
occurrences[{id, startsAt, endsAt, timezone, status, registrationOpen, recordingUrl, calendarUrl, calendarLinks}]`.
`occurrences` lists **all** occurrences (past, cancelled included) ordered by start; filter
client-side on `status`/`registrationOpen`. `calendarUrl` is absolute (`PUBLIC_BASE_URL`).
`calendarLinks` is `{ics, google, outlook, office365}`: `ics` equals `calendarUrl`; the
Google, Outlook.com and Microsoft 365 "add event" links are `null` when the occurrence is
cancelled or has ended. They are built from the public title, times, summary and event
page URL only (`webinars/calendar_links.py`), follow the vendors' undocumented URL
conventions (not verified against the live services), and do not follow reschedules.

### `GET /api/v1/events/<slug>`

`Event` (shape above) for `public` or `unlisted` events; `404` for drafts. **Never** includes
`join_url` or `waiting_room_url`. A recording URL appears only after publication.

### `GET /api/v1/occurrences/<uuid>/calendar.ics`

`text/calendar`. The `UID` is stable for the life of the occurrence; `SEQUENCE`
increases on every reschedule and on cancellation (emitted with `STATUS:CANCELLED`),
so calendar clients update the existing entry instead of adding a duplicate.
`URL` and the end of `DESCRIPTION` hold the event page from `PUBLIC_EVENT_URL_TEMPLATE`
(for example `https://www.example.com/webinars/{slug}`; placeholders `{slug}` and
`{occurrence_id}`, https only, after the host), or the JSON event API URL when the
setting is empty or invalid. `manage.py check` reports an invalid template as
`webinars.E101`. Join and waiting-room links are never written.

### `POST /api/v1/registrations`

Scopes: `register`, `proxy` or `trusted`. Body (`schemaVersion` `"1"`):

```json
{
  "schemaVersion": "1",
  "occurrenceId": "0f8b2d4e-6c1a-4b7e-9f3d-2a5c8e1b7d90",
  "formVersion": "2025-default",
  "idempotencyKey": "c8f1a1f2-9b8e-4a9c-8d2e-3f7b6a5c4d21",
  "profile": {"email": "ada@example.com", "firstName": "Ada", "lastName": "Lovelace",
              "name": null, "company": "Analytical Engines", "jobTitle": "", "companySize": "51-200", "phone": "+447700900123"},
  "organization": {"externalIds": [{"namespace": "crm_account", "value": "ACC-1042"}]},
  "consents": {
    "sms": {"granted": true, "disclosureVersion": "sms-2025-01"},
    "analytics": {"granted": true},
    "advertising": {"granted": false}
  },
  "attribution": {
    "parameters": {"utm_source": "newsletter", "utm_medium": "email"},
    "identifiers": {"gaClientId": "1234567890.1700000000"},
    "pageUrl": "https://www.example.com/webinars/data-101",
    "referrer": "https://news.example.org/"
  },
  "customFields": {"role_focus": "Engineering"}
}
```

Required: `schemaVersion`, `occurrenceId`, `formVersion`, `idempotencyKey`, `profile`,
plus whatever the form version lists in `required_fields`. Profile keys, aliases and
normalisation rules: [`field-mapping.md`](field-mapping.md).

Server behaviour:

* **Durable before acknowledgement**: the person, registration, consent evidence,
  attribution touches, reminder schedule and outbox rows are committed in one
  transaction before the response is returned.
* **Consents** are stored as append-only `client_claim` evidence with
  `server_verified=false`. A client cannot claim server verification. SMS consent
  stores the disclosure version/text and phone snapshot; no SMS is ever sent.
  Unverified submissions record no consent against the person (claim kept on the
  registration only).
* **Attribution** is cleaned to allowlisted parameters/identifiers, gated by the
  submitted analytics/advertising consent, with query string and fragment stripped
  from `pageUrl`/`referrer`. It is descriptive only and never authenticates.
* **Organization external IDs** from `register`/`proxy` keys are stored unverified;
  only `trusted` keys create verified IDs.
* **Identity**: see "Identity rules" in `architecture.md`. In short, email is never
  verified; a matching email from a browser without a valid capability never updates
  or reveals the existing profile or consents and never issues a capability. There is
  no account recovery or cross-device recognition.

Responses have an identical body in every case, so callers cannot learn whether an
email was already registered. Status is `201` for a new registration and for every
unverified submission (including duplicates); `200` only when a capability holder
repeats their own registration; a replay returns the stored status. Cookie-less callers
always get a `Set-Cookie`: a real capability only when a new person was created,
otherwise a random decoy that resolves to nothing.

```json
{
  "status": "registered",
  "occurrenceId": "0f8b2d4e-6c1a-4b7e-9f3d-2a5c8e1b7d90",
  "thankYou": {"mode": "message", "message": "Thanks — see you there."},
  "calendar": {"title": "Data 101", "startsAt": "2025-11-04T15:00:00Z", "endsAt": "2025-11-04T16:00:00Z",
               "timezone": "Europe/London", "icsUrl": "/api/v1/occurrences/0f8b…/calendar.ics",
               "googleUrl": "https://calendar.google.com/…", "outlookUrl": "https://outlook.live.com/…",
               "office365Url": "https://outlook.office.com/…", "uid": "…", "sequence": 0}
}
```

`icsUrl` is relative. Key routes return the `/api/v1/` path; the Node proxy and the
same-origin direct routes return `/webinar-api/calendar.ics?occurrenceId=<uuid>` (direct
mode unless `WEBINAR_DIRECT_ICS_REWRITE=false`). `googleUrl`, `outlookUrl` and
`office365Url` are the same links as the public `calendarLinks` (`null` when cancelled or
ended); an idempotent replay returns the links stored with the original response.

`thankYou` is either `{"mode":"message","message":"…"}` or
`{"mode":"redirect","redirectUrl":"https://…"}` (host validated against
`THANK_YOU_REDIRECT_HOSTS`). The response never contains profile values, person IDs,
or join/waiting-room links (those go only to trusted webhook destinations that opt in).

#### Idempotency

| Situation | Result |
|---|---|
| New key | Processed; response stored against the key with a hash of the request. |
| Same key, same body, no cookie (retry after timeout/network failure) | Stored status and body replayed with a decoy `Set-Cookie`. **A replay never issues a real capability**: if the original response carrying a new capability was lost, that browser stays unrecognised. No duplicate rows or webhooks. |
| Same key, same body, with a capability | Replayed (no `Set-Cookie`) only if that capability made the original request or owns the stored registration; otherwise `409 idempotency_conflict`. |
| Same key, different body | `409 idempotency_conflict`. Nothing is written. |
| New key, same person + occurrence | Identical body; existing registration kept. `200` for the capability holder; `201` for a browser without a valid capability, which changes nothing on the existing registration. |

Only successful responses are stored, so a request that failed with a 4xx can be retried
with the same key.

Browsers should keep one key per submission intent, reuse it on retries, and generate
a new one after a completed registration (the GTM script does this).

#### Capacity and paid sessions (`202`)

Full contract: [tickets.md](tickets.md). Summary (preview; Stripe mocked in tests only):

| Status | Body | Meaning |
|---|---|---|
| `202` | `{"status":"verification_required","occurrenceId","verification":{"available","verifyUrl","message"}}` | Paid session and the caller has not proven the mailbox. **Mandatory before a paid seat:** a registered or recognised cookie alone is not enough. Same answer for every unverified caller, no hold, no Stripe call. `verifyUrl` is the session's waiting room; `available:false` (and `verifyUrl:null`) when the waiting room is disabled or email delivery is `disabled`, so paid sessions need an enabled waiting room and SMTP (or preview) mail. |
| `202` | `{"status":"payment_required","order":{id,status,checkoutUrl,expiresAt,statusToken,…}}` | Verified buyer; a seat is held atomically until Stripe confirms payment or the checkout expires. Not registered yet. |
| `409` | `sold_out` | No seat left (holds count). |

`GET /api/v1/orders/<id>` with header `Webinar-Order-Token` returns order status (public,
token-authorised). Refunds are never automated: refund in Stripe, then record it
(`POST /api/v1/manage/orders/<id>/mark-refunded`, `payments_admin`). A Stripe refund or
dispute blocks waiting-room admission. Public occurrences show `availability.soldOut`,
plus `availability.remaining` only when the session sets `showRemaining`.

Management routes for tickets (`payments_admin`, `content_write` for capacity) and
hosting (`hosting_admin`, `hosting_provision`) are in [tickets.md](tickets.md),
[hosting.md](hosting.md) and `GET /api/v1/openapi.json`; the same handlers are exposed
as MCP tools ([mcp.md](mcp.md)).

Stalled Riverside registrants:
- **List:** `GET /api/v1/manage/occurrences/<id>/hosting/registrants` (`hosting_provision` or `config_read`). Returns registration IDs and state only, never names, emails or links.
- **Reconcile:** `POST /api/v1/manage/occurrences/<id>/hosting/registrants/reconcile` (`hosting_provision`). Send `{registrationId, decision, joinUrl?, externalId?}`. The first call returns 428 with a single-use `confirmToken`; repeat the identical request with `Webinar-Confirm-Token`. `joinUrl` is set-only.
- **Refusals:** other-session IDs get 404; non-Riverside or non-stalled registrants get 409.
- No provider call is made. See [hosting.md](hosting.md#reconciling-stalled-registrants).

Attendance imports (`attendance_write`; reads also `config_read`), base
`/api/v1/manage/occurrences/<id>/attendance/imports`:
- **Create:** `POST …/imports` with `{source: "provider"}` (one read-only pull from the linked Zoom/Riverside session; refused for providers without `attendanceImport`) or `{source: "file", rows: [...]}` (max 5,000 rows). Rows are matched by provider id, then email; nothing is recorded yet.
- **Read:** `GET …/imports` (counts) and `GET …/imports/<importId>` (rows with masked emails).
- **Apply:** `POST …/imports/<importId>/apply` — 428 with a single-use `confirmToken`, then repeat with `Webinar-Confirm-Token`. Records `attended` once per registration; re-applying records nothing new.
- **Review:** `POST …/imports/<importId>/rows/<rowId>/resolve` `{registrationId}` or `{ignore: true}`.
- **No-shows:** `POST …/imports/<importId>/no-shows` (428 + confirm), only after apply and after the session ended; waiting-room check-ins are never marked no-show.
- See [hosting.md](hosting.md#attendance-import) and [management-api.md](management-api.md).

## Proxy-target endpoints (`proxy` scope)

The first-party proxy (`sdk/proxy/`) maps `/webinar-api/*` on the marketing site to
these endpoints, adds its server-held key, and forwards only the capability cookie (`CAPABILITY_COOKIE_NAME`, default `webinar_session`, which must equal the proxy's `PROXY_SESSION_COOKIE`)
in and `Set-Cookie` for it back out.

### `GET /api/v1/profile?occurrenceId=<uuid>&formVersion=<v>`

```json
{"recognized": true, "missingFields": ["jobTitle", "companySize"], "formVersion": "2025-default"}
```

* `recognized` is true only when a valid, unexpired, unrevoked capability resolved to a
  person. It is never derived from email, ad IDs or the public person UUID.
* `missingFields` lists canonical keys (and `custom.<key>`) the form needs that the
  profile lacks. Anonymous callers get every form field. No values are returned.
  An empty list means the UI shows only a "Confirm my RSVP" button.

### `POST /api/v1/registrations`

As above; the forwarded cookie decides which profile (if any) may be updated. Cookie-less
calls always get a `Set-Cookie` (a new capability or a decoy), which the proxy must forward
unchanged.

### `GET /api/v1/my-registration?occurrenceId=<uuid>`

```json
{"occurrenceId": "0f8b…", "recognized": true, "registered": true, "status": "registered",
 "thankYou": {"mode": "message", "message": "…"}, "calendar": {"icsUrl": "…", "…": "…"}}
```

* The person is identified **only** by the forwarded capability cookie. `occurrenceId`
  (exactly once, a UUID) is the only accepted parameter; anything else is `400`.
* `status` is `"registered"`, `"cancelled"` or `null`; `thankYou` and `calendar` (the
  registration response's public block) appear only when registered. No profile, consent,
  attendance, ids or links are returned, and unknown browsers get the same shape as
  recognised people without a registration. Drafts and unknown occurrences are `404`.

### `POST /api/v1/reset`

Revokes the forwarded capability (if any) and returns `{"reset": true}` with an
expiring `Set-Cookie: webinar_session=…; Max-Age=0`. Always succeeds, so it reveals nothing.
Reset does **not** recover or re-link an identity: afterwards the browser is anonymous,
and a submission with an existing email is unverified.

## Same-origin direct proxy (no API key)

When the CMS is served from the same origin as Django, the browser can call
`GET /webinar-api/profile`, `POST /webinar-api/registrations` and
`POST /webinar-api/reset` directly. CSRF defence (all must pass; `Origin` and
`Sec-Fetch-Site` failures return `403 origin_rejected`):

1. `Origin` exactly matches an entry in `WEBINAR_ALLOWED_ORIGINS` (scheme, host, port).
2. POST requests use `Content-Type: application/json`.
3. `Sec-Fetch-Site`, when present, is `same-origin`.
4. POSTs carry either `X-Webinar-Request: 1` or the browser SDK's double-submit token
   `X-Webinar-CSRF` equal to the `__Host-webinar_csrf` cookie (issued by
   `GET /webinar-api/profile`). Both are non-simple headers, so any cross-site caller
   would need a CORS preflight, which is never granted. Mismatch → `403 forbidden`.

Two more direct routes:

* `GET /webinar-api/my-registration?occurrenceId=<uuid>`: the same body as
  `/api/v1/my-registration`, from the capability cookie. It applies the GET origin rules
  above **and** rule 4 (custom or double-submit header), and is rate limited per client
  address (`429 rate_limited`).
* `GET /webinar-api/calendar.ics?occurrenceId=<uuid>`: the same file and visibility rule
  as `/api/v1/occurrences/<uuid>/calendar.ics`. It reads no cookie or key and has no
  origin check, because the content is public.

## Health

`GET /healthz` returns `200` when the process is up and the database answers, `503`
otherwise. It exposes no configuration or version details.
