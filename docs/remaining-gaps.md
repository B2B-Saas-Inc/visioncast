# Remaining gaps

Status as of 2026-10-27, reconciled against [validation.md](validation.md) (latest entries
dated 2026-10-27). **Release candidate `0.2.0-pre` is tagged and published to GHCR (private package,
digest `sha256:c81ad8b1…`, amd64 + arm64); every local gate has a recorded pass on the
release tree; what remains needs owner-held accounts, an owner decision, or the target
environment.** This page separates what the code implements, what blocks a release, what the
owner has requested but is not built yet, and what is optional or out of scope. Test evidence
lives only in [validation.md](validation.md); nothing on this page is a test result. The
ordered, tickable list is [release-checklist.md](release-checklist.md). Detailed behaviour is
owned by [mcp.md](mcp.md), [native-integrations.md](native-integrations.md),
[tickets.md](tickets.md), [hosting.md](hosting.md), [kits.md](kits.md) and
[operations.md](operations.md); where they disagree with this page on detail, theirs win.

## 0. Current evidence (summary; validation.md is authoritative)

| What | Latest recorded | Covers the release tree? |
|---|---|---|
| Python suite, Python 3.12.16 / PostgreSQL 16.15, Ruff, system check, migration drift, logical + application restore probe (`tests/run-pg16.sh`) | **1,945 passed**, all checks and the restore probe passed (2026-10-27) | Yes |
| Same suite locally on Python 3.12 / PostgreSQL 16 after the last merge | 1,945 passed (2026-10-27) | Yes |
| End-to-end acceptance (`tests/test_e2e_acceptance.py`): create → provision (Zoom) → register/pay (room, mailbox code, Stripe webhook) → verify → attend (check-in, and separately Zoom attendance import + no-shows) → CRM follow-up (native HubSpot + signed webhook) | 2 scenarios pass, fake vendors (2026-10-27) | Yes |
| Site (`site/`) tests, typecheck, production build | 50 passed, typecheck and build passed (2026-10-27) | Yes |
| SDK unit, proxy and kit tests | 63 + 9 passed (2026-10-27) | Yes |
| Mocked-backend SDK Chromium (`tests/browser-sdk.cjs`) incl. web calendar links, `payment_required`, `verification_required` | 11/11 (2026-10-27) | Yes |
| Actual-app Chromium: studio journey, Riverside controls, GTM screen | 13/13, 8/8, 10/10 (2026-10-27, local preview on the merged tree) | Yes |
| Website kits installed from fresh vendor scaffolds (WordPress 7.1 / PHP 8.2, Next 16, SvelteKit 3, Astro 7, Nuxt 4, static snippet behind nginx) with a 30-check Chromium flow; CMS schemas validated with vendor packages | All flows pass; five kit bugs fixed (2026-10-27) | Yes (`bash sdk/kits/test/fresh-install.sh` to repeat) |
| Versioned OCI image built from the release tree (`deploy/build-image.sh`) | arm64 `0.2.0-pre` (image ID `sha256:3ff6df0d…`, tar SHA-256 `11cd42aa…`) and an emulated amd64 build from the tagged tree `1462cb7` (image ID `sha256:c73a59ec…`), 2026-10-27 | Yes |
| Published image `ghcr.io/b2b-saas-inc/visioncast:0.2.0-pre` (tag `v0.2.0-pre`, workflow run 40555955145) | digest `sha256:c81ad8b1…`, linux/amd64 + linux/arm64, SBOM + provenance (2026-10-27). Private package | Yes, but the pushed manifest has not been pulled and run from here |
| Clean-server Compose first run, backup/restore, retention dry run, offline receiver contract, `nginx -t` + routing, in-place upgrade from `da04cc7` and rollback | All passed (2026-10-27, distribution entry; first run repeated on the final arm64 image and on the emulated amd64 image) | Upgrade/restore evidence is from commit `3ec60ad`; the first run was repeated on the release image |
| Base-image digest pins (`python:3.12-slim-bookworm`, `postgres:16-alpine`) | Pinned by `@sha256` (2026-10-27) | Yes |
| `pip-audit` on the hash lock | No known vulnerabilities; dev-only `pytest` bumped to 9.0.3 (2026-10-27) | Yes |
| Live demo | Frontend `visioncast-ten.vercel.app` (catalogue lists fourteen adapters only after the next deploy); backend is the 2026-10-26 build | No (owner-approved upgrade, C12; `deploy/upgrade.sh --image ghcr.io/b2b-saas-inc/visioncast:0.2.0-pre` once the host can pull it) |
| Real vendor, Stripe, hosting-provider, SMTP, registry or receiver traffic | None | — |

## 1. Implemented in code (gates in §2–§3)

| Area | Where |
|---|---|
| Public read API, registration, idempotency, dedupe, progressive profile, reset, same-origin direct endpoints | `webinars/api/views.py`, `queries.py`, `registration.py`, `identity.py`; [api-reference.md](api-reference.md) |
| Node first-party proxy with allowlisted projections and ICS rewrite | `sdk/proxy/server.js`; [browser-integration.md](browser-integration.md) |
| Browser SDK (GTM, consent, attribution, SPA `init`, ICS + validated web calendar links, `payment_required` / `verification_required` handling) | `webinars/static/webinar-sdk/webinar-form.js` |
| Management REST API with confirmation-token publication gates; `config_read` GET | `webinars/api/management.py`; [management-api.md](management-api.md) |
| Scheduling, reminders, DST validation, recordings | `webinars/scheduling.py`, `timeutils.py` |
| Signed outbox webhooks, retries, dead letters, SSRF policy, per-destination host pinning | `webinars/outbox.py`, `delivery.py`, `ssrf.py`; [webhooks.md](webhooks.md) |
| Hosted waiting room, mailbox verification, check-in, finalization | `webinars/waiting_room*.py`; [waiting-room.md](waiting-room.md) |
| Operational SMTP (room codes, invitations only) | `webinars/mail.py`; [email.md](email.md) |
| Publication readiness on studio, REST, MCP and Django admin | `webinars/services.py`, `admin.py` |
| Studio, admin, roles, audit, `purge_personal_data` | `webinars/studio/`, `admin.py`, `roles.py` |
| MCP `/mcp` incl. ticket, hosting and attendance-import tools (gated where they mutate); `serverInfo.version` from `VERSION` | `webinars/mcp.py`, `mcp_config.py`; [mcp.md](mcp.md) |
| **Fourteen native adapters**: PostHog, Customer.io, HubSpot, Attio, Segment, GA4, Amplitude, Mixpanel, RudderStack, Brevo, and (2026-10-27, one module each under `webinars/native_providers/`) **Salesforce, Pipedrive, ActiveCampaign, Mailchimp**. Contract points marked VERIFIED (official page fetched) or UNVERIFIED per adapter. **Mock-tested only** | `webinars/native_integrations.py`, `native_providers/`, `delivery.py`; [native-integrations.md](native-integrations.md) |
| Capacity and Stripe Checkout tickets with mailbox verification before holds. **Stripe mocked only** | `webinars/capacity.py`, `payments.py`, `api/tickets.py`; [tickets.md](tickets.md) |
| Hosting: Zoom create/update/attach; Wistia, Sequel create; Riverside attach + per-registrant links + reconciliation; StreamYard manual link. **No adapter sandbox-verified** | `webinars/hosting/`, `api/hosting.py`; [hosting.md](hosting.md) |
| **Provider attendance import** (2026-10-27): authenticated Zoom participant pull (documented endpoint, paginated), Riverside registrant pull (UNVERIFIED constants), JSON/CSV file import for every provider; id-then-email matching, dedupe, ambiguity review, idempotent apply through `record_attendance` (`attendance.recorded` once per registration), separate confirmed no-show pass; REST, MCP and studio. Migration `0011` | `webinars/attendance_import.py`, `api/attendance_imports.py`; [hosting.md](hosting.md) "Attendance import" |
| Website kits: WordPress plugin, framework/headless/proxy references, no-code and CMS schema references, fresh-install verification script | `sdk/kits/`; [kits.md](kits.md) |
| Distribution: `VERSION`, `show_version`, `/healthz` version, OCI labels, `deploy/build-image.sh`, `backup.sh`, `restore.sh`, `upgrade.sh`, `WEBINAR_IMAGE` in Compose, GHCR release workflow (prepared, not run) | `deploy/`, `Dockerfile`, `compose.yaml`, `.github/workflows/release.yml`; [operations.md](operations.md) |

## 2. Release blockers

Must be closed or explicitly accepted by the owner before production use.

### 2a. Evidence on the exact release tree and image

| # | Blocker |
|---|---|
| R1 | **Closed 2026-10-27:** 1,945 tests, checks and the restore probe pass under `tests/run-pg16.sh` (Python 3.12.16 / PostgreSQL 16.15) on the release tree. |
| R2 | **Closed.** The release image (`0.2.0-pre`) was built for arm64 (saved with a recorded SHA-256) and, from the tagged tree, for amd64 under emulation; both passed the clean-server Compose first run (migrate, `sync_roles`, `bootstrap_admin`, `create_api_key`, seed, web + worker healthy, `/healthz` with version, OpenAPI, `/mcp`, studio login page, retention dry run, backup). The published multi-arch manifest is `sha256:c81ad8b1…`. |
| R3 | **Closed:** SDK/kit tests, the actual-app Chromium journeys (studio 13/13, Riverside 8/8, GTM 10/10) and the mocked-backend SDK Chromium suite (11/11, now covering web calendar links and the paid-session 202 states) were rerun on the merged release tree. |
| R4 | **Closed:** `PYTHON_IMAGE` and the Compose Postgres image are pinned by digest; refresh procedure in README "Supply chain notes". |
| R5 | **Closed:** `pip-audit` on the hash lock reports no known vulnerabilities; the only finding was dev-only `pytest` (bumped to 9.0.3). |
| R6 | **Closed locally:** retention dry run, offline receiver contract (`tests/test_verify_webhook_example.py`: valid, tampered, stale, duplicate, wrong secret, rotation), `nginx -t` plus routing and rate-limit checks on both nginx files (two routing bugs fixed). A receiver over the internet is C5. |
| R24 | **Mostly closed:** `release.yml` ran on `v0.2.0-pre` and published amd64 + arm64 to GHCR; the amd64 first run passed under emulation from the same tagged tree. **Open:** pull the published manifest (`read:packages` token or a public package) and repeat the first run on a real amd64 host (C11 staging). |

### 2b. Known code defects and gaps still open

| # | Blocker |
|---|---|
| R7–R10 | **Closed** (see validation.md 2026-10-26 entries): outbox delivery metadata, studio preview refusals, Riverside reconciliation and personal-link binding. |
| R25 | **Closed 2026-10-27:** `sdk/kits/node/nginx-webinar-api.conf` used `location ^~`, which made every proxied kit route answer 404 behind nginx; `docker/nginx.conf.example` lacked locations for `my-registration`, `calendar.ics`, `/w/`, `/studio/login/`, `/mcp` and the Stripe webhook. Both fixed and verified with `nginx -t` and routing checks. |
| R26 | **Closed 2026-10-27:** the WordPress plugin dropped Django's cookie-expiry `Set-Cookie` on reset and emitted a doubled subdirectory path for a self-hosted SDK; the Node proxy had the same quoted-cookie bug; the Next.js reference page did not build under Next 16 `cacheComponents`; the Svelte component did not compile under Svelte 5 runes. All fixed by fresh-install verification. |

### 2c. Vendor contracts (owner accounts required)

| # | Blocker |
|---|---|
| R11 | **Native adapters stay disabled** until each is checked in an owner-approved sandbox: HubSpot (409 on email collision), Attio (`matching_attribute`, list entries), PostHog and Customer.io dedupe, Segment, GA4, Amplitude, Mixpanel, RudderStack (auth ambiguity, regional hosts), Brevo (campaign-eligible contacts under vendor defaults), **Salesforce** (client-credentials token flow unreadable in docs; composite upsert by External ID; `v62.0`), **Pipedrive** (v2 API; rate limits; `due_time` zone; activity dedupe scan limited to 500), **ActiveCampaign** (date custom-field format; regional hosts other than `api-us1.com`; account automations may fire), **Mailchimp** (`<dc>.api.mailchimp.com` form; whether API-created `pending` members get the opt-in mail; unknown merge tags). Vendor writes are never recalled; no vendor-side erasure. |
| R12 | **Stripe test mode** for the full paid flow: Checkout create, `expires_at` minimum, idempotency replay, expire endpoint, all five webhook events, refunds and disputes. Mocked only. |
| R13 | **Hosting provider sandboxes:** Zoom Webinars (create/update/attach **and** `past_webinars/{id}/participants` attendance pull), Wistia, Sequel, Riverside (attach, registrants, attendance: paths are our best reading). |
| R14 | **Real SMTP relay** — required, not optional, if paid tickets are sold (mailbox verification) or invitations / room codes are used. |
| R15 | **Real webhook receiver(s)** the owner will use (the receiver contract is verified offline only). |
| R16 | **Kit compatibility on the owner's real sites.** Fresh-install verification covered WordPress 7.1 / PHP 8.2 (block, shortcode, WP Super Cache, `/blog/` subdirectory), Next 16 self-hosted, SvelteKit 3 / Svelte 5, Astro 7 (behind nginx), Nuxt 4 (behind nginx), a static snippet behind nginx, and the four CMS schemas with vendor packages. **Not verified:** https hosts other than localhost, CDN caches in front of `/webinar-api`, Vercel route handlers, real Webflow/Framer hosting, paid sessions through a kit, Next 14/15, Nuxt 3, Svelte 4, Astro middleware mode, WordPress multisite. |
| R17 | **Web calendar deep links** (Google/Outlook/Microsoft 365) against the live services, or accept unverified. |

### 2d. Owner policy acceptances

| # | Decision |
|---|---|
| R18 | Staff publish without confirmation tokens; admin inline-add fails closed; residual cancel/reschedule race; `team_admin` ≥ `content_write` + `publication`. |
| R19 | Confirmation tokens are not human approval. |
| R20 | `config_read` reads drafts over REST, or request a `content_read` scope (none exists). |
| R21 | Direct-mode ICS rewrite default: Mode B edges must forward `/webinar-api/calendar.ics` (and `my-registration`) or set `WEBINAR_DIRECT_ICS_REWRITE=false`. |
| R22 | Paid sessions require the waiting room and mail; join links sent to webhook destinations before a refund cannot be recalled. |
| R23 | Residual behaviour in §5. |
| R27 | Attendance import never runs automatically and never guesses an ambiguous match; a Zoom pull is capped at 50 pages (5,000 participants) and marked incomplete beyond that; no-shows are a separate confirmed step. |
| R28 | Marketing adapters never subscribe anyone: Mailchimp members default to `transactional` (pending double opt-in needs an explicit acknowledgement; `subscribed` is impossible), ActiveCampaign contacts are never added to lists or automations. Whether the vendor's own automations react to contact or tag changes is the operator's responsibility. |

## 3. Operational gates (target environment; operator/owner)

- A production host (the demo droplet runs the 2026-10-26 backend without a worker; it is a
  demo, not a release target unless the owner says so). Provisioning is the operator's job.
- TLS, HTTP→HTTPS redirect, edge rate limits on `/webinar-api/*` (incl. the proxy's
  `my-registration`), `/w/`, `/mcp`, `/admin/`, `/studio/login/`, `/api/v1/payments/stripe/webhook`
  (`docker/nginx.conf.example` now covers all of them); MFA/SSO/VPN in front of `/admin/` and
  `/studio/`. Platform limits are per-process only.
- `RATE_LIMIT_TRUST_X_FORWARDED_FOR` only behind a trusted proxy chain of the configured depth.
- `INTEGRATION_MASTER_KEY` stored outside backups; scheduled `deploy/backup.sh` (encrypt with
  `--encrypt-to`); a restore drill with `deploy/restore.sh` on the target host.
- Worker process running (outbox, reminders, Stripe sweep, hosting sweep, stale operations
  including lost attendance pulls).
- `WEBHOOK_HOST_ALLOWLIST` entries for every enabled vendor (exact hosts for Salesforce My
  Domain, ActiveCampaign account host, Mailchimp data center and RudderStack data plane),
  Stripe and hosting hosts; https `PUBLIC_BASE_URL`; `PUBLIC_EVENT_URL_TEMPLATE`;
  `manage.py check` on every deploy.
- Stripe webhook endpoint registered with the five events in [tickets.md](tickets.md).
- Cron for `purge_personal_data` (now also blanks attendance-import rows).
- Existing deployments: `deploy/upgrade.sh --image <image>` (backup, `migrate` incl. 0011,
  restart, `sync_roles`, health).
- Launch acceptance in [implementation-guide.md §11.1](implementation-guide.md#111-launch-acceptance-run-on-staging-record-results-in-validationmd) on staging.
- Upgrading the live demo backend to the release tree is separate owner-approved work (C12).

## 4. Requested feature backlog

Committed scope for this release is closed. What the owner asked for and is **not** built:

| Item | State |
|---|---|
| Salesforce, Pipedrive, ActiveCampaign, Mailchimp native adapters | **Built** (mock-tested; sandbox verification R11). No further marketing destinations are committed; the catalogue's remaining "roadmap" entries (Marketo, HubSpot Marketing, Loops, Intercom, n8n, Zapier, Make) are not scheduled. |
| Transactional email adapters (Resend, Postmark, SES) | **Not built, by design** (§7: attendee confirmation/reminder email is delivered by the owner's ESP through signed webhooks, never by the platform). Remove them from the catalogue or accept them as "roadmap". |
| Provider attendance import | **Built** for Zoom (documented endpoint), Riverside (unverified) and file import for every provider. Wistia and Sequel expose no attendance API this release; their reports go through the file import. |
| Hosting update for Wistia/Sequel; Riverside create | Not built (Riverside create is undocumented by the vendor). |
| Live demo backend upgrade to the release tree | Not done; the frontend catalogue must be redeployed to show the four new adapters. |

## 5. Known residual behaviour (accepted or documented, not blockers)

| Item | Notes |
|---|---|
| Proxy `POST /webinar-api/registrations` 2xx body | Passed through apart from the `icsUrl` rewrite. |
| Thank-you `redirectUrl` | Any https URL; the proxy does not strip credentials. |
| "Recognized" oracle | `/profile` and `my-registration` reveal whether the browser holds a real capability. |
| `PUBLIC_EVENT_URL_TEMPLATE` check | `webinars.E101` does not run at Gunicorn start; run `manage.py check`. |
| Rate limits | Per-process, not global. |
| Stripe key rotation | Refused while orders are open. |
| `__Host-webinar_csrf` on plain http | Stored only in a secure context (https, `localhost`, `127.0.0.1`); other http hosts get `403 csrf_failed` on POST ([kits.md](kits.md)). |
| Native events are at-least-once | Mailchimp member events, Brevo and Customer.io events, Pipedrive activities beyond the 500-item dedupe scan can repeat on retry; consumers dedupe on the delivery ID. |

## 6. Optional / not planned (no commitment)

| Item | Notes / workaround |
|---|---|
| `/lobby/<event-slug>` route | Use REST `waitingRoom.url` or webhook `managedRoomUrl`. |
| Dedicated `content_read` scope | Alternative to R20; [continuation-api.md §5.1](continuation-api.md). |
| SDK calling `my-registration` | Sites wire it themselves. |
| Web calendar links following reschedules | Only ICS `UID`/`SEQUENCE` updates. |
| Waiting room under another prefix | Templates hard-code `/w/<code>/`. |
| Content-change webhooks | Use time-based revalidation. |
| Vendor-side erasure; native "send test to a chosen record" | Test sends cancel with `native_test_send_unsupported`. |
| Automated refunds, coupons, taxes, subscriptions, quantity > 1, Connect | [tickets.md](tickets.md) "Not supported". |
| Teams, Google Meet hosting | Not supported. |
| Automatic attendance pulls after a session | Imports are always a person's (or key's) explicit, confirmed action. |
| ActiveCampaign site tracking (`trackcmp.net`), Mailchimp `subscribed` status | Not implemented, by policy. |
| Global rate limiting, built-in TLS/MFA, built-in retention scheduler | Edge / cron. |
| MCP SSE/sessions/stdio | Stateless JSON over HTTP. |
| Over MCP: environment settings, host provisioning, Administrators/superusers, secrets, join/invitation links, registrant PII | Excluded by design ([mcp.md](mcp.md)). |

## 7. Out of scope by design (do not build)

- Streaming, video/recording hosting, gating or view tracking. Check-in and imported
  attendance measure admission or provider-reported presence, not viewing.
- Attendee confirmation/reminder/follow-up email and SMS sending (ESP via webhooks or the
  native marketing adapters' own automations; SMS consent evidence only).
- Cross-device recognition and account recovery ([security.md](security.md)).
- Multi-tenancy.
