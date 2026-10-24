# Continuation API: read-only management, direct-mode calendar, my registration

Status: implemented and wired (routes, OpenAPI, settings, Node proxy); **tests written but
not yet run** (`tests/test_continuation_api.py`, `tests/test_continuation_glue.py`).
Nothing here sends email or webhooks or changes models or migrations.

## 1. Read-only management scope

`GET` on these management endpoints accepts a `config_read` key as well as `content_write`:

| Endpoint | Read-only (`config_read`) | Write (`content_write`) |
|---|---|---|
| `GET /api/v1/manage/events` | yes | yes |
| `GET /api/v1/manage/events/<id>` | yes | yes |
| `GET /api/v1/manage/occurrences/<id>` | yes | yes |
| `GET /api/v1/manage/occurrences/<id>/waiting-room` | yes | yes |
| Every `POST`, `PATCH`, `DELETE`, `PUT`, `HEAD` | **403** | as before |

Rules (`webinars/api/management.py`, `_endpoint(..., read_scopes=CONTENT_READ)`):

- The scope check depends on the method. `config_read` is accepted only when the method is
  `GET` and the endpoint allows `GET`. Any other method needs the endpoint's write scope, so
  a read-only key gets 403 before any body is parsed or any logic runs.
- `view.scopes` is still the write scope. `management.invoke` (the MCP in-process path) is
  unchanged and still requires `content_write` for every method.
- Provider links (`joinUrl`, `waitingRoomUrl`) are never serialized for anyone. Callers get
  only the `hasJoinUrl` and `hasWaitingRoomUrl` flags.
- For keys **without** `content_write`, the attendee-facing managed room URL
  (`waitingRoom.url`, `/w/<code>/`) is returned as `null`. Every key named `url` in the
  response is nulled. This fails closed if a new `url` field is added.
- Other scopes (`public_read`, `reporting`, `proxy`, `register`, `trusted`,
  `attendance_write`, `publication`, `delivery_admin`, `keys_admin`, `team_admin`) still get
  403 on management reads.
- Responses are `Cache-Control: no-store`, and contain no person data, as before.

**Why `config_read`?** It is the existing non-privileged, redacted read scope. It can be
delegated by `keys_admin`, and over MCP it already lists hosts, topics, form versions and
series (`list_catalog`). Reading draft events and sessions without links fits "configuration
read (redacted). No secrets or personal data." **Side effect:** existing `config_read` keys can
now also read draft event content over REST. If the owner does not want that, use the
dedicated scope in §5.1 instead.

## 2. Direct-mode calendar route

`GET /webinar-api/calendar.ics?occurrenceId=<uuid>` (`views.direct_calendar`)

- Returns the same bytes, content type and `Content-Disposition` as
  `/api/v1/occurrences/<id>/calendar.ics` (shared `_ics_response`). Only public and unlisted
  events are served; anything else is 404.
- The query must be exactly one `occurrenceId` UUID: missing, malformed, repeated or extra
  parameters get 400. This is the same rule as the Node proxy.
- It reads no cookie and no key, and sets no cookie. There is deliberately no origin check:
  the content is public, and calendar links may be opened from mail clients.
- `GET` only (other methods get 405).

**Rewriting `icsUrl` in direct registrations is on by default.** With
`WEBINAR_DIRECT_ICS_REWRITE` (environment variable, `config/settings.py`, default `true`),
successful `POST /webinar-api/registrations` and direct `my-registration` responses return
`calendar.icsUrl = /webinar-api/calendar.ics?occurrenceId=<uuid>`. This is the same rewrite
the Node proxy does. An unexpected `icsUrl` is dropped, never passed through. Idempotent
replays are rewritten on a copy, and the stored replay body is unchanged. Key-authenticated
`/api/v1/registrations` and `/api/v1/my-registration` responses are never rewritten.

**Compatibility.** Before this change, direct-mode responses carried
`/api/v1/occurrences/<id>/calendar.ics`, which works only if the edge also routes
`/api/v1/occurrences/` to the platform. An edge that forwards the whole `/webinar-api/`
prefix needs no change. An edge set up from the earlier Mode B instructions forwards only
`/webinar-api/{profile,registrations,reset}`: after upgrading it must add
`/webinar-api/calendar.ics` (and `/webinar-api/my-registration` if used), or set
`WEBINAR_DIRECT_ICS_REWRITE=false` to keep the old link. Otherwise new "Add to calendar"
links 404 at the edge.
Old links already handed out keep working wherever `/api/v1/occurrences/` is routed; the
SDK renders whatever `icsUrl` it receives.

## 3. My registration (capability-authenticated status)

| Route | Auth | Notes |
|---|---|---|
| `GET /webinar-api/my-registration?occurrenceId=<uuid>` | capability cookie | same-origin direct mode |
| `GET /api/v1/my-registration?occurrenceId=<uuid>` | `proxy` key + forwarded cookie | proxy-target twin |

Response (`Cache-Control: no-store`):

```json
{"occurrenceId": "…", "recognized": true, "registered": true, "status": "registered",
 "thankYou": {"mode": "message", "message": "…"},
 "calendar": {"title": "…", "startsAt": "…Z", "endsAt": "…Z", "timezone": "…",
              "icsUrl": "/webinar-api/calendar.ics?occurrenceId=…",
              "googleUrl": "https://calendar.google.com/…", "outlookUrl": "https://outlook.live.com/…",
              "office365Url": "https://outlook.office.com/…", "uid": "…", "sequence": 0}}
```

- `status` is `"registered"`, `"cancelled"` or `null`. `thankYou` and `calendar` appear only
  when `status == "registered"`. They are the event's public data (`registration.build_response_body`),
  the same block the registration response returns. The direct route uses the direct
  calendar path unless `WEBINAR_DIRECT_ICS_REWRITE=false`. The proxy-key route returns the
  platform path, which the Node proxy rewrites (`sdk/proxy/server.js`, route
  `/webinar-api/my-registration`, which also requires the double-submit CSRF token and
  projects the body to the documented keys).
- The person is identified **only** by the capability cookie (`identity.resolve_capability`,
  which handles expiry, revocation and erasure). Email, person id or registration id
  parameters are rejected with 400, because only `occurrenceId` is allowed.
- No profile, consent, attendance, check-in, organization, person/registration id, join link
  or room URL is ever returned. The endpoint never issues, rotates or clears the cookie.
- An unrecognized browser and a recognized person without a registration get the same shape.
  A browser that received the decoy cookie (unknown browser plus existing email) is
  "not recognized", so this endpoint does not confirm that a registration exists.
- Direct-route CSRF and origin checks: the GET origin rules from `/webinar-api/profile`
  (Origin, if present, must be allowed; `Sec-Fetch-Site`, if present, must be `same-origin`)
  **plus** the POST custom-header rule (`X-Webinar-Request: 1` or the `X-Webinar-CSRF`
  double-submit token). The custom header forces a CORS preflight cross-site, and that
  preflight is never granted.
- Rate limit on the direct route: 60 requests per 60 s per client address, per process
  (`ratelimit.hit_key`, bucket `my_registration`, 429 `rate_limited`). Edge limits on
  `/webinar-api/*` still apply. The proxy-key route has no in-app limit, like
  `/api/v1/profile`, because all its requests come from the proxy's address.
- Only public and unlisted occurrences are served (404 otherwise, independent of the person).

## 4. Known limits

- The Node proxy forwards `/webinar-api/my-registration`, but the browser SDK does not call
  it; a site uses it from its own script. Calling it from the SDK is not built.
- There was already a "recognized" oracle: after an anonymous registration,
  `/webinar-api/profile` says `recognized:false` for an existing email (decoy cookie) and
  `true` for a new one. `my-registration` exposes the same bit and no more. Fixing it is an
  identity design change, out of scope here.
- The per-process in-app rate limit is a coarse brake, not a global quota (see `ratelimit.py`).

## 5. Glue status

Applied (glue pass, 2026-10-23; tests in `tests/test_continuation_glue.py` and
`sdk/test/proxy.test.js`, not yet run): items 2 to 6 below. `WEBINAR_DIRECT_ICS_REWRITE`
is an environment setting that defaults to **true** (see §2 Compatibility), not off as
originally proposed. Item 1 is **not applied**: `config_read` remains the read-only scope
until the owner decides.

1. **Optional dedicated scope** (`webinars/models.py`, no migration: `scopes` is a JSONField
   without choices). Add `CONTENT_READ = "content_read", "Content read (drafts, sessions; no links)"`
   to `ApiKey.Scope`. Then change `READ_ONLY = (ApiKey.Scope.CONFIG_READ,)` to
   `(ApiKey.Scope.CONTENT_READ,)` in `webinars/api/management.py`. Add it to
   `mcp_config.DELEGABLE_SCOPES` and `studio/views_settings.SCOPE_HELP`.
2. **`webinars/openapi.py`**: document the read scope on the four GETs (description
   "content_write or config_read (read-only: waitingRoom.url is null)"). Add paths
   `/webinar-api/calendar.ics` (GET, `security=[]`, 200 `text/calendar`, 400, 404),
   `/webinar-api/my-registration` and `/api/v1/my-registration` (GET, query `occurrenceId`
   required, 200 schema `MyRegistration` with properties
   `occurrenceId, recognized, registered, status, thankYou, calendar`, plus 400/403/404/429
   and 400/401/403/404 respectively).
3. **`webinars/studio/views_settings.py` `SCOPE_HELP["config_read"]`**: mention "read
   management events and sessions (no links)".
4. **`sdk/proxy/server.js`**: add the route
   `'/webinar-api/my-registration': { method: 'GET', upstream: '/api/v1/my-registration' }`,
   forwarding the cookie and passing `occurrenceId` the way `/profile` does, and run
   `rewriteRegistrationBody` on its response.
5. **Docs**: `docs/management-api.md`, `docs/api-reference.md`, `README.md` scope table
   (`config_read` is no longer MCP-only), `docs/implementation-guide.md` §6.2 / Mode B
   (platform now serves `/webinar-api/calendar.ics`; `WEBINAR_DIRECT_ICS_REWRITE`), and
   `docs/remaining-gaps.md` rows for these three items.
6. **`config/settings.py`** (optional): `WEBINAR_DIRECT_ICS_REWRITE = env_bool(..., False)`
   if it should be set from the environment. Today it is read with `getattr` and defaults to off.
