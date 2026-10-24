# Remaining gaps

Status as of 2026-10-23, reconciled against the review follow-up recorded in
[validation.md](validation.md) (18:23Z). **Preview, not production-ready.** This page
separates what the code implements, what blocks a release, what must happen outside the
code, and what is not implemented. Test evidence lives only in
[validation.md](validation.md); nothing on this page is a test result. The ordered,
tickable list of what remains is [release-checklist.md](release-checklist.md). Detailed MCP
and native-integration behaviour is owned by [mcp.md](mcp.md) and
[native-integrations.md](native-integrations.md); where those docs and this page
disagree on detail, theirs win.

Earlier versions of this page said the completion and continuation waves had never been
executed and that the image predated them. That is superseded: validation.md records
788 and then 803 PostgreSQL 15 passes, SDK/proxy and actual-app Chromium passes, and an
OCI archive built after the review fixes. What that evidence does **not** cover is listed
in §2 and §3.

## 1. Implemented in code (verified in preview only; release gates in §2 and §3)

| Area | Where |
|---|---|
| Public read API (list/filter/paginate, event by slug, form schema, ICS) | `webinars/api/views.py`, `queries.py`; [api-reference.md](api-reference.md) |
| Registration, idempotency, dedupe, progressive profile, reset | `webinars/registration.py`, `identity.py` |
| Same-origin direct endpoints `/webinar-api/{profile,registrations,reset,my-registration,calendar.ics}` | `webinars/api/views.py` |
| Node first-party proxy, incl. `/webinar-api/calendar.ics` rewrite and allowlisted `my-registration` projection (nested `thankYou`/`calendar` keys; web calendar links must match the exact service endpoint, else `null`) | `sdk/proxy/server.js`; [browser-integration.md](browser-integration.md) |
| Browser SDK (GTM, consent, attribution, SPA `init`); thank-you state shows the ICS link plus validated Google/Outlook/Microsoft 365 links (text-only, new tab, `noopener noreferrer`, accessible label) | `webinars/static/webinar-sdk/webinar-form.js` (`_renderCalendar`, `calendarWebLinks`) |
| Management REST API (`content_write`, `attendance_write`; `publication` for visibility changes and recordings, with 428 + `Webinar-Confirm-Token` and `409 not_ready`); `GET` also accepts `config_read` (B7) | `webinars/api/management.py`; [management-api.md](management-api.md) |
| Scheduling: reminders, late skip, cancel, reschedule, DST validation, recordings | `webinars/scheduling.py`, `timeutils.py` |
| Signed outbox webhooks, retries, dead letters (admin requeue), SSRF policy, send-time re-filtering | `webinars/outbox.py`, `delivery.py`, `ssrf.py`; [webhooks.md](webhooks.md) |
| Hosted waiting room `/w/<code>/`, check-in, finalization | `webinars/waiting_room*.py`; [waiting-room.md](waiting-room.md) |
| Operational SMTP (room codes, invitations only) | `webinars/mail.py`; [email.md](email.md) |
| Publication readiness (`services.publish_problems`) on every publish path: studio, REST, MCP and Django `/admin/`. In admin, `AdminEventForm` checks the submitted event, and `OccurrenceInlineFormSet` re-checks the upcoming-session rule on the sessions as submitted in the same POST (closes review F1) | `webinars/services.py`, `admin.py`, `api/management.py`, `mcp_config.py` |
| `my-registration` rate limit: always on the direct route; on `/api/v1/my-registration` (proxy target) only when `RATE_LIMIT_TRUST_X_FORWARDED_FOR` is on (review F3) | `webinars/api/views.py` `_my_registration_within_limit` |
| Calendar: configurable event page URL in ICS (`PUBLIC_EVENT_URL_TEMPLATE`, system check `webinars.E101`); Google/Outlook/Microsoft 365 links in public occurrences (`calendarLinks`) and registration responses; join links stay private | `webinars/calendar_links.py`, `ics.py`, `apps.py`, `registration.py` |
| Studio, Django admin, roles, audit, `purge_personal_data` (persons, attribution, org IDs, capabilities, queued and in-flight payloads) | `webinars/studio/`, `admin.py`, `roles.py`, `management/commands/purge_personal_data.py` |
| MCP `/mcp`: content, publication (gated), attendance, reporting, redacted readiness, destinations and synthetic test send (gated), bounded key delegation, Editor/Analyst team management (gated), resources (incl. packaged `webinar://guides/implementation-guide`), prompts | `webinars/mcp.py`, `mcp_config.py`, `mcp_resources.py`, `confirmation.py`, `invitations.py`; [mcp.md](mcp.md) |
| Single-use confirmation tokens (bound to action, key, arguments and state; 10 minutes) | `webinars/confirmation.py`, migration `0007_confirmation_use` |
| Privileged scopes (`trusted`, `publication`, `delivery_admin`, `keys_admin`, `team_admin`) granted only by a superuser or `manage.py create_api_key` | `webinars/services.py` `PRIVILEGED_SCOPES` |
| Native adapters: PostHog, Customer.io, HubSpot, Attio on the bounded vendor-host-pinned transport, with data-processing and match-strategy confirmations and per-step consent re-checks | `webinars/native_integrations.py`, `delivery.py`, `ssrf.request_json_bounded`; [native-integrations.md](native-integrations.md). **Vendor contracts unverified; not production-ready (B3).** |

## 2. Release blockers

These must be closed or explicitly accepted by the owner before any production use.
[release-checklist.md](release-checklist.md) turns each into a checkable step.

| # | Blocker | Owner |
|---|---|---|
| B1 | **Re-run evidence after any later change.** [validation.md](validation.md) records 803 PostgreSQL 15 passes, Ruff, `manage.py check`, `makemigrations --check`, SDK/proxy tests and actual-app Chromium 12/12 for the tree as of 18:23Z (review fixes F1–F3 and the SDK web calendar links included). Any code change after that, including the in-progress operational-settings change (`config/settings.py`, `.env.example`, `tests/test_operational_settings.py`), is unverified until a new run is recorded. The mocked-backend SDK Chromium run (`tests/browser-sdk.cjs`, last recorded 8/8 in an earlier wave) is not recorded after the SDK calendar change, and no browser test exercises the new web links (unit tests only). | Pi / release validator |
| B2 | **Image provenance and runtime.** The latest archive is `artifacts/pi-build-df010e6b-a961-4760-9f52-ee0b8b069d7d/image.tar`. validation.md records no SHA-256 for it, and it has never been loaded, run, Compose-started or smoke-tested. Rebuild after any B1 change and record the digest. | Pi |
| B3 | **Native adapters must stay disabled** until each vendor's request shapes are checked against current official docs and in an owner-approved sandbox (HubSpot 409 on email collision, Attio `matching_attribute` and list-entry semantics, PostHog dedupe, Customer.io event dedupe). Vendor writes are never recalled, and there is no vendor-side erasure propagation. | Owner |
| B4 | **Human publication authority is an accepted policy, not a gate.** Staff with `change_event` (Editors and Administrators) publish in the studio and in Django `/admin/` without confirmation tokens. The readiness check is enforced on all paths, including same-POST inline session edits in admin. Residuals: a session *added* inline in the publishing POST fails closed (save as draft first), and a concurrent cancel/reschedule between validation and save can still leave a just-published event without an upcoming session (same window as REST/MCP; post-publish cancellation is allowed anyway). A `team_admin` key can invite Editors, so it is at least as powerful as `content_write` + `publication`. The owner must accept this. | Owner |
| B5 | **Confirmation tokens are not human approval.** They prove the same key asked twice. Any process that needs a human sign-off must add it outside the platform. | Owner |
| B6 | **Edge controls** (§3): TLS, edge rate limits, MFA/SSO/VPN for `/admin/` and `/studio/`. The platform has per-process limits only. `/api/v1/my-registration` behind the Node proxy has **no** backend limit unless `RATE_LIMIT_TRUST_X_FORWARDED_FOR` is on with a correctly configured trusted proxy, so the edge must limit the proxy's `my-registration` route. | Operator |
| B7 | **`config_read` reads drafts over REST.** The read-only management scope reuses `config_read`, so existing `config_read` keys can read unpublished event copy and `internalName` (never links or person data). The owner must accept this or ask for a dedicated `content_read` scope ([continuation-api.md §5.1](continuation-api.md)); none exists in the code. | Owner |
| B8 | **Direct-mode calendar link changed.** `WEBINAR_DIRECT_ICS_REWRITE` defaults to true, so direct registrations link `/webinar-api/calendar.ics`. Mode B edges built from the earlier guide (forwarding only `profile`, `registrations`, `reset`) must add that path (and `my-registration` if used) or set the flag to false ([implementation-guide.md §5.1](implementation-guide.md#51-choose-a-proxy-mode)). | Operator |
| B9 | **Web calendar deep links are unverified against the live services.** Google/Outlook/Microsoft 365 URL formats are tested only for shape. The endpoint origin/path is duplicated in `webinars/calendar_links.py`, `sdk/proxy/server.js` and the SDK, so a change to one must be made in all three or links are silently dropped (`null`). Verifying against the live services needs owner approval (external calls). | Owner |

## 3. External release gates (no code change expected; must be done and recorded)

- Built-image runtime and Compose smoke test (`compose.yaml` targets `postgres:16-alpine`);
  a PostgreSQL 16 test run (all evidence so far is PostgreSQL 15; `tests/run-pg.sh`
  hard-codes cluster 15).
- TLS termination, edge rate limits (`/webinar-api/*` incl. `my-registration`, `/w/`,
  `/mcp`, `/admin/`, `/studio/login/`), MFA, SSO or a VPN in front of `/admin/` and
  `/studio/`. `docker/nginx.conf.example` is untested.
- Pinning base images by digest (`Dockerfile` `PYTHON_IMAGE`, `compose.yaml` Postgres);
  a hash-locked transitive dependency set (`requirements.txt` has direct pins only, not
  verified against PyPI per its header) and a vulnerability review, including Pillow.
- Backup **and restore** rehearsal; storing `INTEGRATION_MASTER_KEY` outside backups.
- A real SMTP relay test (if waiting-room email verification or invitations are used).
- Real webhook receivers: signature, replay and dedupe check against `examples/verify_webhook.py`.
- Vendor sandbox checks before enabling any native adapter (owner approval required; B3).
- A host or server to run on. Provisioning is the operator's job; the platform and MCP
  provide instructions only.
- A cron or scheduler for `purge_personal_data` retention runs.
- Existing deployments: migrate and run `sync_roles`.
- The launch acceptance checks in [implementation-guide.md §11.1](implementation-guide.md#111-launch-acceptance-run-on-staging-record-results-in-validationmd).

## 4. Known residual behaviour (accepted or documented, not blockers)

| Item | Notes |
|---|---|
| Proxy `POST /webinar-api/registrations` 2xx body | Passed through apart from the `icsUrl` rewrite; only the SDK validates web calendar links on that path. Direct (no proxy) mode relies on the SDK check alone. |
| Thank-you `redirectUrl` in proxy projection | Any https URL (backend-validated); the proxy does not strip credentials from it. |
| "Recognized" oracle | `/profile` and `my-registration` reveal whether the browser holds a real capability. A browser that just registered with an email that already existed gets `registered:false` from `my-registration` although registration returned 201. Changing this is an identity design change. |
| `PUBLIC_EVENT_URL_TEMPLATE` check timing | System check `webinars.E101` does not run at Gunicorn start; a bad template logs one warning and falls back safely. Run `manage.py check` on deploy. |
| Rate limits | Per-process (LocMemCache), not global. |

## 5. Not implemented (wish list, no commitment)

Custom work on your site or receiver can cover most of these; the guide says where.

| Item | Notes / workaround |
|---|---|
| `/lobby/<event-slug>` route | Only `/w/<code>/` exists. Resolve on your server using REST management `waitingRoom.url` (needs `content_write`; `config_read` gets `null` by design) or a mapping built from webhook `managedRoomUrl`. |
| Dedicated `content_read` scope | Optional alternative to B7; patch described in [continuation-api.md §5.1](continuation-api.md). Not applied. |
| SDK use of `my-registration` | The SDK renders ICS and web calendar links from the registration response but never calls `my-registration`; sites wire that themselves. |
| Web calendar links following reschedules | They add a copy; only the ICS `UID`/`SEQUENCE` updates an existing entry. |
| Mounting the waiting room under another prefix | Templates hard-code `/w/<code>/…`. |
| Content-change webhooks | For CDN/ISR revalidation. Use time-based revalidation. |
| Vendor-side erasure propagation; "send test to a chosen record" for native destinations | Native test sends are cancelled with `native_test_send_unsupported`. |
| Global (cross-process) rate limiting; built-in TLS/MFA | Edge responsibility. |
| Env-configurable `RATE_LIMIT_TRUSTED_PROXY_COUNT`, platform-wide waiting-room caps, `WEBHOOK_TOTAL_TIMEOUT_SECONDS` | Read via settings with code defaults; not environment variables (`WAITING_ROOM_MAX_CODE_ATTEMPTS` and `WAITING_ROOM_MAX_CODES_PER_EMAIL` already are). A change is in progress; update this row only when [validation.md](validation.md) records it passing. |
| Built-in retention scheduler | Use cron. |
| MCP over SSE/sessions/subscriptions, stdio transport | Stateless JSON over HTTP only. |
| Over MCP: writing environment settings, provisioning hosts, managing Administrators or superusers, returning secrets, join links, invitation links or registrant PII | Excluded by design per [mcp.md](mcp.md). |

## 6. Out of scope by design (do not build)

These are explicit responsibilities of other systems under the approved scope
(`SPEC.md`), not gaps:

- Streaming, video hosting, recording hosting, gating or view tracking: the media
  provider's job. Check-in measures admission, not viewing.
- Attendee confirmation, reminder and follow-up email, and any SMS sending: done by your
  ESP or automation via webhooks. The platform stores SMS consent and withdrawal
  evidence only.
- Cross-device recognition and account recovery: not supported until verification
  exists ([security.md](security.md)).
- Multi-tenancy.
