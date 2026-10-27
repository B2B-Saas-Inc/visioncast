# Implementation guide (start to finish)

> **Status: preview, not production-ready.** This guide follows the code in this
> repository (October 2026). Nothing here has run as an internet-facing deployment.
> The Compose stack and built image have no smoke test, PostgreSQL 16 is unverified,
> and real SMTP/CRM/webhook receivers and backup restore are unverified. Release
> evidence and gates are kept in [validation.md](validation.md); this guide does not
> claim any test result.

Every step has one of these labels:

| Label | Meaning |
|---|---|
| **[Supported]** | Implemented in this repository (routes in `webinars/urls.py`, settings in `config/settings.py`). |
| **[Custom]** | Work you build on your website, edge, or receiver. The platform provides the inputs. |
| **[Not built]** | Not implemented. Don't plan around it. |
| **[Unverified]** | Implemented, but not exercised in a real deployment. |

Authoritative references (this guide links to them rather than repeating them):
`GET /api/v1/openapi.json` (canonical API description; `docs/openapi.yaml` is superseded),
[api-reference.md](api-reference.md), [management-api.md](management-api.md),
[browser-integration.md](browser-integration.md) (the source of truth for the SDK and proxy),
[field-mapping.md](field-mapping.md), [webhooks.md](webhooks.md),
[waiting-room.md](waiting-room.md), [email.md](email.md), [operations.md](operations.md).
These areas are owned elsewhere: [mcp.md](mcp.md), [native-integrations.md](native-integrations.md),
[validation.md](validation.md).

---

## 0. Architecture in one picture

```
www.example.com (your site)                 webinars.example.com (this platform)
  /webinars/<slug>  page  ── server fetch ──>  GET /api/v1/events/<slug>          (public, no key)
  form + webinar-form.js                        GET /api/v1/occurrences/<id>/calendar.ics
  /webinar-api/*  ── EITHER Node proxy ────>   /api/v1/{profile,registrations,reset,my-registration} (proxy key)
                  ── OR edge pass-through ─>   /webinar-api/{profile,registrations,reset,my-registration,calendar.ics} (Origin/CSRF, no key)
  /w/<code>/ (optional edge pass-through) ─>   /w/<code>/...  hosted waiting room
                                               worker ── signed HTTPS webhooks ──> your ESP/CRM/CDP
```

The platform stores events, occurrences, people, registrations and consents, and emits
webhooks. It sends only two kinds of email itself, both over SMTP: waiting-room
verification codes and teammate invitations (see [email.md](email.md)). It does
**not** send confirmations, reminders or follow-ups. Your email or automation system
sends those, driven by webhooks.

---

## 1. Server deployment [Supported] [Unverified]

### 1.1 Build and configure

No image is published, so build it locally (see `compose.yaml`):

```sh
cp .env.example .env && chmod 600 .env     # fill every REQUIRED value
docker compose build
docker compose run --rm migrate            # explicit; never automatic
docker compose run --rm web manage bootstrap_admin --username admin --email you@example.com
docker compose up -d                       # db, web (gunicorn :8000 on 127.0.0.1), worker
```

Services: `db` (`postgres:16-alpine`, internal `backend` network only), `migrate`,
`web`, `worker`, and `test` (opt-in profile). The containers run read-only, with
`cap_drop: ALL` and `no-new-privileges`. Entrypoint roles (`docker/entrypoint.sh`):
`web`, `worker`, `migrate`, `manage <args>`.

Environment keys used by this guide (all in `.env.example` or `config/settings.py`):

| Key | Purpose |
|---|---|
| `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`, `INTEGRATION_MASTER_KEY` | Required secrets. The stack fails closed without them. |
| `DJANGO_ALLOWED_HOSTS` | Platform host names. Add the marketing host only if you pass `/w/` or `/webinar-api/` through with the original `Host` (§6). |
| `CSRF_TRUSTED_ORIGINS` (or `DJANGO_CSRF_TRUSTED_ORIGINS`) | Exact origins for Django-CSRF forms (admin, studio, waiting room). |
| `DJANGO_TRUST_X_FORWARDED_PROTO=1` | Only behind TLS that **overwrites** `X-Forwarded-Proto`. Without it, an HTTPS proxy causes a redirect loop. |
| `PUBLIC_BASE_URL` | Absolute base for `calendarUrl`, ICS `URL`, `managedRoomUrl`. |
| `PUBLIC_EVENT_URL_TEMPLATE` | Optional. Your event page for calendar entries, e.g. `https://www.example.com/webinars/{slug}` (`{slug}`, `{occurrence_id}`; https; placeholders after the host). Empty or invalid uses the JSON event API URL; `manage.py check` reports an invalid value as `webinars.E101` (§6.2). |
| `WEBINAR_ALLOWED_ORIGINS` | Exact origins allowed on `/webinar-api/*` (direct mode). |
| `WEBINAR_DIRECT_ICS_REWRITE` (`true`) | Direct mode: registration `calendar.icsUrl` points at `/webinar-api/calendar.ics`. Set `false` only if your edge cannot forward that path (§5.1, §6.2). |
| `THANK_YOU_REDIRECT_HOSTS` | Hosts allowed as event `thankYouUrl` (https). |
| `WEBHOOK_HOST_ALLOWLIST`, `WEBHOOK_ALLOWED_PORTS` | Webhook destination hosts. An empty allowlist allows nothing. |
| `CAPABILITY_COOKIE_NAME` (`webinar_session`), `CAPABILITY_TTL_DAYS` (180) | Browser recognition cookie. Must equal the Node proxy's `PROXY_SESSION_COOKIE`. |
| `REMINDER_OFFSETS_MINUTES` (`1440,60`), `REMINDER_LATE_GRACE_MINUTES` | Reminder schedule. Grace defaults to 15 (settings and `.env.example`). |
| `OUTBOX_LEASE_SECONDS`, `OUTBOX_BATCH_SIZE`, `OUTBOX_PAYLOAD_RETENTION_DAYS`, `IDEMPOTENCY_TTL_HOURS` | Worker and retention tuning. |
| `EMAIL_DELIVERY` (`disabled` \| `smtp`), `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS`, `SMTP_SSL`, `DEFAULT_FROM_EMAIL` | Platform-sent email only (room codes, invitations). |
| `RATE_LIMIT_ENABLED`, `RATE_LIMIT_TRUST_X_FORWARDED_FOR` | Basic per-process limits. Not global, so you still need edge limits. The trusted-proxy count (`RATE_LIMIT_TRUSTED_PROXY_COUNT`, default 1) is an environment variable bounded to 1–10; see [operational settings](operational-settings.md). |
| `WAITING_ROOM_VERIFICATION_TTL_MINUTES`, `WAITING_ROOM_MAX_CODE_ATTEMPTS`, `WAITING_ROOM_MAX_CODES_PER_EMAIL`, `WAITING_ROOM_POLL_SECONDS`, `INVITATION_TTL_HOURS` | Waiting room and invitations ([waiting-room.md](waiting-room.md)). The platform-wide code and guess caps use code defaults only. |
| `WORKER_HEARTBEAT_MAX_AGE` (60) | Read only by `docker/worker_healthcheck.py`. |

`.env.example` lists every environment-readable key with a safe sample or the code
default, and contains no secrets.

### 1.2 Roles and keys

- After every upgrade, run `docker compose run --rm web manage sync_roles` so the
  Administrator, Editor and Analyst group permissions converge.
- API keys are printed once and stored hashed. Create them with
  `docker compose run --rm web manage create_api_key --name "<label>" --scope <scope>`,
  or in the studio's API-key UI. No scope implies another.
  - REST: `register`, `proxy` and `trusted` (registration); `content_write` and
    `attendance_write` (management API); `publication` (event visibility changes,
    which also need `content_write`, and recordings; both answer 428 and need a repeat
    with `Webinar-Confirm-Token`); `config_read` (read-only `GET` on management events,
    sessions and waiting rooms, drafts included, with `waitingRoom.url` nulled). Public
    REST reads (`/api/v1/events…`, ICS, form schema) need **no key at all**. No REST
    endpoint checks `public_read` or `reporting`.
  - MCP (`/mcp`): `public_read` (public read tools and resources), `reporting`,
    `content_write`, `attendance_write`, `config_read`, `publication`,
    `delivery_admin`, `keys_admin` and `team_admin`. [mcp.md](mcp.md) is authoritative
    for MCP scopes.
  - Privileged scopes (`trusted`, `publication`, `delivery_admin`, `keys_admin`,
    `team_admin`) are granted only by a superuser in the studio or by
    `manage.py create_api_key`.
- Webhook signing secrets: `manage set_destination_secret --destination <uuid> --generate`.

### 1.3 TLS, edge, readiness

- **[Custom]** TLS termination, rate limits (registration, profile, reset, `/admin/`,
  `/studio/login/`, `/w/`, `/mcp`) and MFA, SSO or a VPN in front of `/admin/` and
  `/studio/`. `docker/nginx.conf.example` is an **untested** starting point.
- Readiness: `GET /healthz` returns `{"status":"ok"}`, or 503 when the database is
  unreachable. The web healthcheck is in the Dockerfile. The worker is "healthy"
  when `WORKER_HEARTBEAT_FILE` is fresh (`docker/worker_healthcheck.py`).
- Run **exactly one worker service** (§9.1). Scale `web` with `WEB_CONCURRENCY`.

---

## 2. Content model and index pages

### 2.1 Creating content

- Marketers work in the studio at `/studio/`, which has a visual form builder, a
  per-session embed snippet, cover upload and waiting-room controls ([studio.md](studio.md)).
  Developers use the management API with a `content_write` key
  ([management-api.md](management-api.md)); a `config_read` key can read but not change it.
- Management write fields (camelCase, `webinars/api/management.py`):
  - Event: `internalName, slug, publicTitle, summary, heroImageUrl, attention, interest, decision, action, learningOutcomes[], hosts[] (slugs), topics[] (slugs), visibility (draft|public|unlisted), formVersion, thankYouMode, thankYouUrl, thankYouMessage, registerLabel, confirmLabel`.
  - Occurrence: `seriesId, timezone (IANA), localStart (wall clock), dstFold (0|1), durationMinutes, registrationOpen, joinUrl, waitingRoomUrl`. The two URL fields are **write-only**; reads return only `hasJoinUrl`/`hasWaitingRoomUrl`.
  - Waiting room (`PATCH /api/v1/manage/occurrences/<id>/waiting-room`): `enabled, admissionLeadMinutes, admissionMode`.
  - Cancel: `POST …/occurrences/<id>/cancel`. Recording: `…/occurrences/<id>/recording` (`publication`, 428 confirmation). Attendance: `…/registrations/<id>/attendance` (`attendance_write`).
  - Publishing (`visibility` other than `draft` in a PATCH) needs `content_write` **and** `publication`, and returns `409 not_ready` when the event fails the readiness check (title, opening copy, Action copy, an upcoming session). The studio, MCP and Django admin apply the same check.
  - Hosts `name, slug, title, bio, imageUrl`. Topics `name, slug`. Forms `version, description, requiredFields, optionalFields, fieldMapping, customFields, defaultPhoneRegion, smsDisclosureVersion, smsDisclosureText, isActive`.

### 2.2 Public read API (no key) [Supported]

| Route | Notes |
|---|---|
| `GET /api/v1/events` | Lists only `visibility=public` events. Query: `q` (title or summary, ≤200 chars), `topic` (slug), `host` (slug), `when` (`upcoming` \| `recorded`), `limit` (1–50, default 20), `offset` (≥0). Order is `-created_at, pk`. Response: `{count, limit, offset, results[]}`. An invalid filter returns 400. |
| `GET /api/v1/events/<slug>` | `public` **or `unlisted`** events. 404 for drafts. |
| `GET /api/v1/occurrences/<uuid>/calendar.ics` | Public and unlisted events only. No auth. |
| `GET /api/v1/forms/<version>` | Public form schema (labels, types, options). |

Event object (`serialize_event`):

| Path | Type / notes |
|---|---|
| `slug`, `title`, `summary` | `title` is the public title. `internalName` is never public. |
| `heroImageUrl` | Uploaded cover (`/media/covers/<uuid>.<ext>`) or external URL, or `null`. |
| `aida.attention`, `aida.interest`, `aida.decision`, `aida.action` | Copy blocks. |
| `learningOutcomes[]` | Strings. |
| `hosts[]` | `{name, slug, title, imageUrl}`. Your template decides between "Host" and "Hosts". |
| `topics[]` | `{name, slug}` |
| `formVersion` | Pass this to the form as `data-form-version`. |
| `cta` | CTA labels (register and confirm). The exact shape is in `/api/v1/openapi.json`. |
| `occurrences[]` | Sorted by `startsAt`: `{id, startsAt, endsAt (UTC "Z"), timezone (IANA), status (scheduled\|cancelled…), registrationOpen, recordingUrl (null unless published), calendarUrl (absolute), calendarLinks {ics, google, outlook, office365}}`. The web links are `null` when the session is cancelled or has ended (§6.2). |

Public responses **never** contain `joinUrl`, `waitingRoomUrl` or the managed room URL.

### 2.3 Index page recipe [Custom]

1. Fetch the list on the server or at build time: `GET /api/v1/events?when=upcoming&limit=20&offset=0`.
   Paginate with `offset += limit` while `offset < count`. Use a separate
   `?when=recorded` index for the on-demand library.
2. Topic and host filter pages use `?topic=<slug>` and `?host=<slug>`.
3. Unlisted events never appear in lists. Link to them directly by slug.
4. Cache public responses at your CDN or ISR layer (they are not `no-store`).
   Revalidate after studio edits. There is no change webhook for content, so use
   time-based revalidation or a deploy hook.

---

## 3. Event page mapping [Custom]

| Page element | Source |
|---|---|
| `<h1>` / `<title>` | `title` |
| Hero | `heroImageUrl` (meaningful alt text from `title`). Leave the image out when it is `null`. |
| Attention / Interest / Decision / Action sections | `aida.*`. Use `aida.action` near the form. |
| "You'll learn" | `learningOutcomes[]` |
| Host or Hosts heading | `hosts.length === 1 ? "Host" : "Hosts"`. Render `name`, `title`, `imageUrl`. |
| Topic chips | `topics[]`, linking to `/webinars?topic=<slug>` |
| Submit label | `cta` register label. Put the confirm label in `data-confirm-label`. |

**Date and timezone.** Show `startsAt` in the event's `timezone` as the canonical time,
and optionally the visitor's local time on the client, for example with
`Intl.DateTimeFormat(undefined, {timeZone: occ.timezone, dateStyle: 'full', timeStyle: 'short', timeZoneName: 'short'})`.
Server rendering in the visitor's zone causes hydration mismatches, so render the
event zone on the server and localise after hydration. Always include the zone name.

**Occurrence selection** (deterministic):

1. Candidates are `occurrences.filter(o => o.registrationOpen)`. That flag is already
   false for cancelled occurrences, past occurrences and occurrences with
   registration closed.
2. Default to the earliest candidate. If there are several, render a radio or select
   control that switches the form's `data-occurrence-id`, then call
   `WebinarForm.init()` on a freshly rendered form (§5.4).
3. With no candidates, show the latest occurrence whose `recordingUrl` is non-null
   (on-demand), or a "no upcoming sessions" state. Cancelled occurrences can stay
   visible as "Cancelled" but must not get a form.

---

## 4. Registration form [Supported SDK] [Custom markup]

Use the studio's per-session **Embed** snippet, or write the markup by hand as shown
in [browser-integration.md §3](browser-integration.md#3-html-form-markup):
`form[data-webinar-form][data-occurrence-id][data-form-version]`.

| Concern | Rule |
|---|---|
| Canonical names | `email, phone, firstName, lastName, name, company, jobTitle, companySize` |
| Accepted snake_case aliases (SDK) | `first_name, last_name, full_name→name, job_title, company_name→company, company_size, email_address→email, phone_number→phone` |
| Any other name | `data-field-map='{"work_email":"email"}'` in the browser, and/or the form version's server-side `fieldMapping` ([field-mapping.md](field-mapping.md)). Conflicting values are rejected. |
| Custom fields | `name="custom.<key>"`, where `<key>` matches `^[A-Za-z][A-Za-z0-9_]{0,63}$`. Declare the key in the form version's `customFields`. Delivered as `profile.customFields` in webhooks. |
| Org IDs | `name="organization.externalId"` with `data-namespace`. Always **unverified** from the browser. Only `trusted`-scope server calls store verified IDs. |
| SMS consent | `name="consents.sms"` checkbox with `data-disclosure-version` equal to the form's `smsDisclosureVersion`. The checkbox must stay separate and unticked by default. |
| Analytics / advertising consent | Not form fields. They come from the CMP through `WebinarForm.setConsent` (§5.3). |
| Progressive profiling | Automatic. `GET /profile` returns only `{recognized, missingFields, formVersion}`; fields not in `missingFields` are hidden and disabled. No stored value ever reaches the browser. |

Registration request body (`schemaVersion: "1"`, JSON Schema: [registration.schema.json](registration.schema.json)):

```json
{
  "schemaVersion": "1",
  "occurrenceId": "9a8b7c6d-4444-4d3c-b2a1-000000000004",
  "formVersion": "default-v1",
  "idempotencyKey": "1f0c6a52-0d1e-4b0a-9a55-6c2b2f1d9e01",
  "profile": {"email": "ada@example.com", "firstName": "Ada", "company": "Example Ltd"},
  "customFields": {"role": "Engineering"},
  "organization": {"externalIds": [{"namespace": "crm.account", "value": "ACC-1234"}]},
  "consents": {"analytics": {"granted": true}, "advertising": {"granted": false},
               "sms": {"granted": false, "disclosureVersion": "sms-2026-01"}},
  "attribution": {"parameters": {"utm_source": "newsletter"}, "identifiers": {},
                  "pageUrl": "https://www.example.com/webinars/intro", "referrer": null}
}
```

Success response (`registration.build_response_body`). The status is 201 when the
registration was created **or** the identity is unverified, and 200 for an
idempotent replay of a verified registration:

```json
{
  "status": "registered",
  "occurrenceId": "9a8b7c6d-4444-4d3c-b2a1-000000000004",
  "thankYou": {"mode": "message", "message": "Thanks — you're registered."},
  "calendar": {"title": "Intro to Widgets", "startsAt": "2026-03-10T15:00:00Z", "endsAt": "2026-03-10T15:45:00Z",
               "timezone": "Europe/London", "icsUrl": "/api/v1/occurrences/9a8b…0004/calendar.ics",
               "googleUrl": "https://calendar.google.com/calendar/render?action=TEMPLATE&…",
               "outlookUrl": "https://outlook.live.com/calendar/0/deeplink/compose?…",
               "office365Url": "https://outlook.office.com/calendar/0/deeplink/compose?…",
               "uid": "9a8b…0004@webinars.example.com", "sequence": 0}
}
```

`icsUrl` is shown as returned on key routes; the Node proxy and direct mode rewrite it
(§6.2). `googleUrl`, `outlookUrl` and `office365Url` are `null` when the session is
cancelled or has ended. `thankYou` can also be `{"mode":"redirect","redirectUrl":"https://www.example.com/thanks/intro"}`.

---

## 5. Website deployment: SDK and first-party proxy

### 5.1 Choose a proxy mode

| Mode | Marketing-origin route | Platform route | Key | Use when |
|---|---|---|---|---|
| **A. Node proxy** (`sdk/proxy/server.js`) | `/webinar-api/{profile,registrations,reset,my-registration,calendar.ics}` to Node on `127.0.0.1:8787` | `/api/v1/{profile,registrations,reset,my-registration}` and `/api/v1/occurrences/<id>/calendar.ics` | `proxy` key, server-only (never sent on the calendar route) | The platform is on another host or private network. |
| **B. Direct edge pass-through** | `/webinar-api/{profile,registrations,reset,my-registration,calendar.ics}` (or the whole `/webinar-api/` prefix) to the platform's `/webinar-api/*`, path unchanged | Django `direct_profile/registrations/reset/my_registration/calendar` | none | Your edge can forward to the platform and you accept origin and CSRF checks in place of a key. |

**Upgrading a Mode B edge:** earlier versions of this guide forwarded only
`/webinar-api/{profile,registrations,reset}`. Registration responses now point
`calendar.icsUrl` at `/webinar-api/calendar.ics`, so add that path at the edge, or set
`WEBINAR_DIRECT_ICS_REWRITE=false` to keep the old `/api/v1/occurrences/…` link (§6.2).

Both modes keep every request **same-origin from the browser's point of view**. Never
call the platform host cross-origin from the browser: no CORS is granted.

**Mode A configuration** (only the keys listed in [browser-integration.md §2](browser-integration.md#2-proxy-deployment)):
`PROXY_BACKEND_URL=https://webinars.example.com` (origin only),
`PROXY_API_KEY=<proxy-scope key>` (from a secret store, never in GTM or the page),
`PROXY_ALLOWED_ORIGINS=https://www.example.com`, and
`PROXY_SESSION_COOKIE` equal to the platform's `CAPABILITY_COOKIE_NAME`. Leave
`PROXY_CSRF_COOKIE` at its default, because the SDK hard-codes `__Host-webinar_csrf`.

**Mode B checks** (`webinars/api/views.py`). `Origin` must exactly match an entry in
`WEBINAR_ALLOWED_ORIGINS`; it is required on POST and checked on GET when present.
`Sec-Fetch-Site` must be `same-origin` when present. POST requests need
`Content-Type: application/json` plus either `X-Webinar-CSRF` equal to the
`__Host-webinar_csrf` cookie or `X-Webinar-Request: 1`; so does the
`my-registration` GET, which is also rate limited. The profile GET issues the
CSRF cookie. `calendar.ics` reads no cookie and has no origin check. Your edge must forward `Origin`, `Cookie` and `Sec-Fetch-Site`
unchanged and must not cache. Set `DJANGO_ALLOWED_HOSTS` to the `Host` value your
edge sends upstream. Cookies are host-only on the marketing origin, so do not add a
`Domain`. Direct mode never stores organisation IDs as verified.

### 5.2 SDK delivery

Serve `webinar-form.js` from a first-party path: the platform's
`/static/webinar-sdk/webinar-form.js`, mirrored to your CDN. In GTM, add a Custom HTML
tag that defines `window.WebinarFormConfig = {proxyBase: '/webinar-api', autoInit: true, consent: {analytics:false, advertising:false}}`
**before** the script tag ([browser-integration.md §6](browser-integration.md#6-gtm-configuration)).
Fire it on DOM Ready on form pages only. Under a strict CSP, `connect-src 'self'` is
enough.

### 5.3 Consent

The default is denied. Map your CMP to `WebinarForm.setConsent({analytics, advertising})`
or `setConsentProvider(fn)`. The SDK collects attribution only with consent, and pushes
the PII-free `webinar_registration` dataLayer event only with analytics consent. SMS
consent comes only from the checkbox. All consent stored by the platform is a client
claim (`source: client_claim`, `serverVerified: false`).

### 5.4 SPAs

Render the form, then call `WebinarForm.init(container)`. Forms that are already bound
are skipped. To switch occurrence, re-render a new `<form>` element with the new
`data-occurrence-id` and call `init` again; mutating attributes on a bound form has no
effect. The idempotency key lives in memory only, so a route change before the
response arrives loses it. Disable navigation while `data-webinar-state` is
submitting.

---

## 6. Thank-you page, calendar and lobby

### 6.1 Thank-you [Supported]

The SDK follows `thankYou.redirectUrl` only if it is https or same-origin. The server
accepts only hosts listed in `THANK_YOU_REDIRECT_HOSTS`. Otherwise the SDK shows
`thankYou.message`. The redirect URL carries **no** registration or person data. To
personalise the page, use only data you already hold in the page (title and date from
the public event API).

To show "you're registered" (for example on a revisit), call
`GET /webinar-api/my-registration?occurrenceId=<uuid>` from your own script [Supported]
with the `X-Webinar-CSRF` header (or `X-Webinar-Request: 1` in Mode B). It identifies the
browser only by its capability cookie and returns `{occurrenceId, recognized, registered,
status}`, plus `thankYou` and `calendar` when registered. It never returns profile data,
ids or join links, and unknown browsers look the same as people who aren't registered.
The SDK does not call it; that wiring is yours [Custom]. Contract:
[continuation-api.md §3](continuation-api.md) and `/api/v1/openapi.json`.

### 6.2 Calendar (ICS) [Supported]

- The platform returns `calendar.icsUrl` as a relative path,
  `/api/v1/occurrences/<id>/calendar.ics`. What the browser receives depends on the
  proxy mode:
  - **Mode A (Node proxy) [Supported]:** the proxy rewrites `icsUrl` to
    `/webinar-api/calendar.ics?occurrenceId=<uuid>` and serves that route, fetching
    the public ICS upstream with no key or cookies. It drops any `icsUrl` of
    unexpected shape. The SDK's "Add to calendar" link works same-origin with no
    extra edge rules.
  - **Mode B (direct pass-through) [Supported]:** the platform rewrites `icsUrl` the
    same way and serves `/webinar-api/calendar.ics?occurrenceId=<uuid>` itself (same
    file as the `/api/v1/` route; no cookie, key or origin check). Your edge must
    forward that path (§5.1). With `WEBINAR_DIRECT_ICS_REWRITE=false` the old relative
    `/api/v1/occurrences/…` path is returned instead, and your edge must route
    `/api/v1/occurrences/` to the platform [Custom].
  - **Server-to-server registration** (`register`/`trusted` key): `icsUrl` is not
    rewritten; use `calendarUrl`, or build `PUBLIC_BASE_URL` plus `icsUrl`.
- For emails, use `PUBLIC_BASE_URL + "/api/v1/occurrences/" + event.occurrence.id + "/calendar.ics"`
  (the `/webinar-api/calendar.ics` route exists only on the marketing origin).
- The ICS is dynamic and needs no auth. It has a stable `UID`; `SEQUENCE` increases on
  reschedule and cancel. A cancelled occurrence returns `METHOD:CANCEL` and
  `STATUS:CANCELLED`. `DESCRIPTION` and `URL` point at your event page when
  `PUBLIC_EVENT_URL_TEMPLATE` is set (e.g. `https://www.example.com/webinars/{slug}`),
  otherwise at the public event **JSON** API (`/api/v1/events/<slug>`). The file
  **never contains join or lobby links**, so put the lobby link in your emails, not in
  the calendar.
- **Google and Outlook links [Supported] [Unverified]:** public occurrences carry
  `calendarLinks {ics, google, outlook, office365}` and registration responses carry
  `calendar.googleUrl`, `outlookUrl` and `office365Url`. They use the title, times,
  summary and the event page URL above, never private links, and are `null` once the
  session is cancelled or has ended. The URL formats are the vendors' undocumented
  conventions and have not been checked against the live services. Unlike the ICS
  `UID`/`SEQUENCE`, an event added this way does **not** follow a reschedule. The SDK
  shows only the ICS link; render the web links yourself [Custom].

### 6.3 `www.example.com/lobby/<event-slug>` [Custom]

The platform's attendee entry is **`/w/<code>/`**, a stable per-occurrence managed
waiting room that survives reschedules. It is not keyed by event slug. Options:

1. **Use the platform host as-is (least work):** send `managedRoomUrl` in emails
   (§7). Visitors on a different host than the form are not recognised by cookie, so
   they verify by email code. That requires `EMAIL_DELIVERY=smtp`.
2. **Pass `/w/` through on the marketing origin, path unchanged** (`location /w/ { proxy_pass https://webinars.example.com; }`, forwarding `Cookie`, `Origin`, `Referer`,
   `X-Forwarded-Proto`). The capability cookie set by `/webinar-api/*` on
   `www.example.com` is then sent to the room, so registered browsers are recognised
   without email. Requirements: add `https://www.example.com` to `CSRF_TRUSTED_ORIGINS`
   (room forms use Django CSRF), make `DJANGO_ALLOWED_HOSTS` accept the forwarded
   `Host`, include `/static/` (room CSS and JS), and **do not rewrite the prefix**,
   because templates hard-code `/w/<code>/…` paths. `PUBLIC_BASE_URL` must be the
   marketing origin if you want `managedRoomUrl` to point there. That changes ICS and
   API links too, so all of them must then be routed. This setup is **[Unverified]**.
3. **`/lobby/<event-slug>` resolver:** a small server route on your site that picks
   the occurrence (same rules as §3, but include occurrences currently live) and
   answers 302 to `/w/<code>/`. The room code is **not** in the public REST API. Get
   it from `waitingRoom.url` in the REST management API, which requires a
   `content_write` key (a read-only `config_read` key gets `waitingRoom.url: null` by
   design), or from a mapping you store when the webhook
   `event.occurrence.managedRoomUrl` arrives.
   MCP tools are meant for agents, not for a website's request path. Never put the
   lookup key in the browser.

The room redirects to the provider join link only via the `Location` header of
`POST /w/<code>/enter` after admission. **Never** place `joinUrl` in public pages,
SDK config, ICS files or analytics.

---

## 7. Webhooks: one source for confirmations, reminders and follow-ups

### 7.1 Destinations [Supported]

Create a Destination in the admin with these fields: `url` (https, host in
`WEBHOOK_HOST_ALLOWLIST`), `kind` (`email`/`crm`/`generic` are labels only),
`event_types[]`, `allowed_groups[]` (`identity, profile, organization, consents, attribution, registration, event, delivery_metadata`),
`required_consents[]`, `field_mapping{}`, `include_private_links`, `max_attempts`
and `enabled`. Then set the secret with `set_destination_secret`. Native vendor
adapters are covered in [native-integrations.md](native-integrations.md), which is
maintained separately. Treat a vendor as available only when that document says it is
implemented and verified.

### 7.2 Payload (schemaVersion "1", `webinars/outbox.py`)

Top level: `schemaVersion`, `eventType`, `occurredAt`, then each allowed group, then
`mapped` (only when `field_mapping` is set).

| Path | Notes |
|---|---|
| `identity.personId`, `identity.email`, `identity.verified` | `verified=false` means the email belonged to an existing person but came from an unknown browser. |
| `profile.{email,phone,firstName,lastName,name,company,jobTitle,companySize}` | `null` when unknown. For unverified registrations, only the submitted values. |
| `profile.customFields.<key>` | |
| `organization.externalIds[]` | `{namespace, value, verified, source}` |
| `consents.{sms,analytics,advertising}` | `{granted, disclosureVersion, recordedAt, withdrawnAt, source, serverVerified}`. Every purpose is ungranted for unverified registrations. |
| `attribution.firstTouch`, `attribution.registrationTouch` | `{parameters, identifiers, pageUrl, referrer, recordedAt}`. `firstTouch` is `null` when unverified. |
| `registration.{id,status,attendance,formVersion,identityVerified,createdAt,updatedAt}` | |
| `event.{id,slug,title,summary,heroImageUrl,aida,learningOutcomes,hosts[],topics[],thankYou,cta,revision}` | A self-contained snapshot, so receivers need no API call. |
| `event.occurrence.{id,startsAt,endsAt,timezone,status,icsUid,icsSequence,cancelledAt}` | |
| `event.occurrence.recordingUrl`, `recordingPublishedAt` | Only once published. |
| `event.occurrence.managedRoomUrl` | When the room is enabled. Provider links are then never sent. |
| `event.occurrence.joinUrl`, `waitingRoomUrl` | Only when the room is off **and** `include_private_links` is set. |
| `delivery_metadata.{deliveryId,destinationId,destinationKind,eventType,createdAt,attempt}` | `attempt` is set at send time and changes on retries. `deliveryId` never changes. |

Event types: `registration.created`, `registration.updated`, `occurrence.cancelled`,
`occurrence.rescheduled`, `reminder.due`, `recording.published`,
`attendance.recorded`, `attendance.checked_in`, `follow_up.requested`. Occurrence-level
events fan out **one delivery per registrant**. A full sample payload is in
[webhooks.md](webhooks.md#example-payload-fake-data).

### 7.3 Receiver: verify, deduplicate, branch [Custom]

Headers: `Webinar-Delivery-Id`, `Webinar-Event-Type`, `Webinar-Timestamp`, and
`Webinar-Signature: v1=<hex>` over `"{deliveryId}.{timestamp}." + raw body`. Verify the
signature on raw bytes in constant time, reject timestamp skew over 300 s, store
`deliveryId` and acknowledge duplicates with 2xx without reprocessing. Return 2xx only
after a durable write; anything else is retried with backoff. Redirects are failures.
The reference implementation is [`examples/verify_webhook.py`](../examples/verify_webhook.py).

```python
# Pseudocode for an ESP/automation receiver. Sends are keyed on deliveryId (idempotent).
def handle(p):
    occ, ev = p["event"]["occurrence"], p["event"]
    ident = p.get("identity", {})
    if not ident.get("email"):                   # destination not allowed identity/profile
        return ack()
    if occ["status"] == "cancelled" and p["eventType"] != "occurrence.cancelled":
        return ack()                             # stale reminder/confirmation, skip
    link = occ.get("managedRoomUrl") or FALLBACK_EVENT_PAGE.format(slug=ev["slug"])  # never joinUrl in marketing email unless intended
    when = fmt_local(occ["startsAt"], occ["timezone"])  # always state the zone
    t = p["eventType"]
    if t == "registration.created":   send("confirmation", link=link, when=when, ics=f"{PUBLIC_BASE_URL}/api/v1/occurrences/{occ['id']}/calendar.ics")
    elif t == "registration.updated": update_contact_only()
    elif t == "reminder.due":         send("reminder", link=link, when=when)
    elif t == "occurrence.rescheduled": send("rescheduled", link=link, when=when, seq=occ["icsSequence"])
    elif t == "occurrence.cancelled": send("cancelled"); cancel_own_scheduled_sends(p["registration"]["id"])
    elif t == "recording.published":  send("recording", url=occ["recordingUrl"])
    elif t == "attendance.recorded":
        a = p["registration"]["attendance"]
        send("followup_attended" if a == "attended" else "followup_missed" if a == "no_show" else None)
    elif t == "follow_up.requested":  send("followup_generic")
    # attendance.checked_in: update CRM ("joined the lobby"); do not email.
    return ack()
```

Fallbacks to build in. If `firstName` is null, greet without a name. If `hosts` is
empty, leave out the host block. If `heroImageUrl` is null, use no image. If
`managedRoomUrl` is absent, link to the event page and send the join link from your
provider's own system.

### 7.4 Scheduling ownership rule (one owner)

**The platform owns the timing of reminders. Your ESP sends `reminder.due` deliveries
immediately and must not run its own time-based reminder schedule for the same
webinar.** If you do schedule in the ESP instead, leave `reminder.due` out of every
destination's `event_types`, and then you own every edge case below.

Platform behaviour (`webinars/scheduling.py`):

- Reminders are created per registration for each `REMINDER_OFFSETS_MINUTES`.
  **Late signup:** offsets already in the past are stored as `skipped_late` and never
  sent. A due reminder is skipped if it is more than `REMINDER_LATE_GRACE_MINUTES` late
  or the session has started (for example after worker downtime).
- **Cancel:** pending reminders are suppressed and `occurrence.cancelled` goes to each
  registrant. ICS `SEQUENCE` increases and the room closes.
- **Reschedule:** old reminders are superseded and new ones scheduled. Unsent
  `attendance.recorded` messages for the old run are cancelled
  (`superseded_by_reschedule`). `occurrence.rescheduled` carries the new times and
  `icsSequence`. Delivered messages cannot be recalled.
- **DST:** occurrences store an IANA zone plus a UTC instant. Times in a DST gap are
  rejected; repeated hours need `dstFold`. Reminder offsets are absolute minutes
  before the UTC start. Always render with `timezone`.

### 7.5 Suppression and privacy

- A destination receives a delivery only when every `required_consents` purpose is
  granted. This is re-checked at send time. Erased people get nothing further.
- **SMS:** send only when `identity.verified` is true **and**
  `consents.sms.granted` is true for that phone number. The platform sends no SMS.
- **Unverified registrations:** don't merge them into CRM records or overwrite fields.
  Store them as a separate unconfirmed lead or activity.
- Use `allowed_groups` to minimise data. An ESP needs `identity`, `profile`,
  `registration`, `event` and `consents`. An analytics CDP may need only `event`,
  `registration` and `attribution` with `required_consents:["analytics"]`.

---

## 8. CRM, CDP and email field mapping [Custom via `field_mapping`]

`field_mapping` adds a flat `mapped` object. Source paths must be canonical group
paths inside the allowed groups. Mappings never chain.

| Canonical path | HubSpot contact (example) | Customer.io person (example) | Attio person (example) | PostHog (example) |
|---|---|---|---|---|
| `identity.email` | `email` | `email` | `email_addresses` | `distinct_id` source (receiver-side) |
| `profile.firstName` / `lastName` | `firstname` / `lastname` | `first_name` / `last_name` | `name` (receiver composes) | person property |
| `profile.company` | `company` | `company` | company relation (receiver) | `$groups.company` (receiver) |
| `profile.jobTitle` | `jobtitle` | `job_title` | `job_title` | person property |
| `event.slug` / `event.title` | custom `webinar_slug` / `webinar_title` | event attribute | list entry attribute | event property |
| `event.occurrence.startsAt` | custom datetime | `webinar_start` (unix in receiver) | attribute | event property |
| `event.occurrence.managedRoomUrl` | custom `webinar_lobby_url` | `lobby_url` | — | **never send** |
| `registration.attendance` | custom enum | attribute | attribute | event `webinar_attended` |
| `attribution.registrationTouch.parameters.utm_source` | `utm_source` (custom) | attribute | attribute | `utm_source` |

These vendor names are **illustrations for a generic-webhook receiver or middleware you
build**. Whether a native adapter exists, and its exact behaviour, is documented in
[native-integrations.md](native-integrations.md) only. Do not assume one exists from
this table. Example destination mapping:

```json
{"identity.email": "email", "profile.firstName": "first_name", "event.title": "webinar_title",
 "event.occurrence.startsAt": "webinar_start", "event.occurrence.managedRoomUrl": "lobby_url"}
```

---

## 9. Attendance, check-in and recordings

- **Check-in ≠ viewing.** `attendance.checked_in` means a browser passed admission at
  `/w/<code>/enter`, which happens once per registration per run. It does not prove the
  person watched. If provider links are distributed directly (room off, with
  `include_private_links`), a missing check-in is not proof of absence.
- Finalization happens at room end, by the worker or the End button. Current
  check-ins become `attended` and the rest `no_show`, and `attendance.recorded` is
  emitted. Explicit attendance from your provider's reports goes through the
  management API (`attendance_write`). Attendance is **never inferred** otherwise.
- **Recordings:** publish an https URL with `…/occurrences/<id>/recording` (needs the
  `publication` scope and a repeat with `Webinar-Confirm-Token` after the 428). It then
  appears in public `recordingUrl` and `when=recorded`, and `recording.published` is
  sent to registrants. The platform **does not host, gate or check access** to the
  recording URL. Access control is your media provider's job.

### 9.1 Worker rule

Run one `worker` service. Claims use `SKIP LOCKED` leases, so extra workers are safe
for the outbox, but one worker keeps reminder and finalization timing simple to reason
about. If the worker is down, reminders past their grace period are skipped, not sent
late.

---

## 10. Agents (MCP)

`POST /mcp` is a stateless JSON-RPC endpoint authenticated with API keys, and
`/developers/mcp/` has setup notes. Scopes, tools, gated `dryRun`/`confirmToken`
actions, resources (including `webinar://playbook/implementation`) and prompts are
documented in [mcp.md](mcp.md), which is maintained separately. This guide does not
repeat that list. The guide itself is served to agents as
`webinar://guides/implementation-guide`.

Setup steps:

1. Create a key with only the scopes the agent needs (studio → API keys, or
   `create_api_key`). Start with `public_read`, or `config_read` for a read-only
   readiness review.
2. Configure the client with the URL and a Bearer header, as in `mcp.md` →
   "Client configuration". Keep the key in the client's secret store.
3. Treat gated tools' confirm tokens as **the same key holder asking twice, not human
   approval**. A person should review each preview before the agent confirms.

Limits to plan around (from `mcp.md`): MCP does not write environment settings,
provision hosts, manage Administrators or superusers, or return secrets, join links,
invitation links or registrant PII. Editor/Analyst invitations (`team_admin`) and a
synthetic test send (`send_test_delivery`, `delivery_admin`) exist and are gated.
Process settings stay in `.env` and need a restart. Anything that `mcp.md`
marks as instructions-only, not implemented or in progress is not available.

Tickets and hosting tools mirror the REST management routes and are gated where they
mutate: `payments_admin` (Stripe account with set-only secrets, prices, orders,
`mark_order_refunded` to record a refund already made in Stripe, `release_unpaid_order`),
`hosting_admin` (hosting accounts with set-only credentials) and `hosting_provision`
(execute, reconcile, manual links). These scopes are superuser/owner-granted only. No
tool moves money. Guides: `webinar://guides/tickets`, `webinar://guides/hosting`,
`webinar://guides/kits`.

### 10.1 Paid sessions and hosting [Supported] [Unverified]

- **Paid seats need mailbox proof.** For a paid session, `POST /registrations` answers
  `202 verification_required` until the buyer has verified the mailbox with a
  waiting-room code; a registered cookie alone is not enough. Enable the session's
  waiting room and configure SMTP (or preview) mail, otherwise `verification.available`
  is `false` and nobody can buy. Details: [tickets.md](tickets.md).
- Seats are held atomically and kept until Stripe confirms payment or checkout expiry.
  Show or hide the remaining count per session (`showRemaining`).
- Refunds are never automated: refund in Stripe, then record it. A Stripe refund or
  dispute blocks admission.
- Hosting ([hosting.md](hosting.md)): Zoom Webinars create/update/attach; Wistia and Sequel
  create only; Riverside attach with per-registrant links (paths unverified). No adapter is
  sandbox-verified, and none imports attendance.
- Website kits ([kits.md](kits.md)) are not production-tested; WordPress and framework
  compatibility is unverified.

---

## 11. Operations

Full procedures: [operations.md](operations.md).

- **Backups:** `pg_dump` the `pgdata` database on a schedule. Store
  `INTEGRATION_MASTER_KEY` **separately** from the backups. Restore has not been
  verified, so rehearse it before launch [Unverified].
- **Upgrades:** build the new image, back up, `docker compose run --rm migrate`,
  `manage sync_roles`, then `docker compose up -d`. Roll back only to a backup taken
  before the migration.
- **Key rotation:** for the master key, set the new `INTEGRATION_MASTER_KEY` and put
  the old one in `INTEGRATION_MASTER_KEY_PREVIOUS`, then re-set destination secrets.
  Rotate a webhook secret with `set_destination_secret --generate`; receivers should
  accept both secrets during the switch [Custom]. Rotate API keys by creating a new
  key, deploying it, then revoking the old one. Rotate `DJANGO_SECRET_KEY` during a
  maintenance window (it logs out sessions).
- **Retention:** `manage purge_personal_data --older-than-days N [--dry-run]` and
  `--person <uuid>` for erasure. Outbox payloads are purged after
  `OUTBOX_PAYLOAD_RETENTION_DAYS`. Schedule this with your own cron [Custom]; the
  platform has no built-in scheduler for it.

### 11.1 Launch acceptance (run on staging, record results in validation.md)

1. `/healthz` returns 200 through the edge, and HTTP redirects to HTTPS with no loop.
2. Rate limits at the edge reject bursts on `/webinar-api/registrations`, `/w/`,
   `/mcp` and the login pages. MFA or SSO protects `/admin/` and `/studio/`.
3. A draft event returns 404, an unlisted event is reachable by slug but not listed,
   and a public event is listed.
4. A form submission in a consent-denied browser carries no attribution. With consent
   granted it carries UTMs. A second visit hides known fields. "Not you?" shows the
   full form.
5. Replaying the same `idempotencyKey` returns the same body. A changed body with the
   same key returns 409.
6. The receiver gets a signed `registration.created`. A tampered body, a stale
   timestamp and a duplicate `deliveryId` are each rejected or ignored.
7. Rescheduling produces `occurrence.rescheduled` and the ICS `SEQUENCE` increases.
   Cancelling suppresses reminders.
8. With the room enabled, no `joinUrl` appears in any webhook, page source, ICS or API
   response. `POST /enter` after open returns 303 to the provider.
9. A backup restores into a scratch database. `purge_personal_data --dry-run` reports
   the expected counts.

---

## 12. SEO and accessibility [Custom]

- Server-render the event page from the public API, using a canonical URL on your
  site. Add `Event` structured data (`name`=`title`, `startDate`=`startsAt`,
  `eventAttendanceMode`=Online, `eventStatus` Cancelled or Rescheduled from `status`).
- Set `noindex` on unlisted events and on thank-you and lobby pages. Never index
  `/w/`.
- Use the SDK's accessible defaults (`role=alert` focus, `aria-invalid`, live status
  region). Give every input a visible `<label>`, use `autocomplete` tokens, never use
  placeholder-only labels, keep visible focus and keep the CTA text descriptive.
  Present times with the zone name. Image alt text comes from content, not file names.

---

## 13. Gaps

See [remaining-gaps.md](remaining-gaps.md). It separates what is implemented, what is
an external release gate, and what is genuinely unimplemented.
