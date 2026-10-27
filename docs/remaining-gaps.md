# Remaining gaps

Status as of 2026-10-26, reconciled against [validation.md](validation.md) (latest entry
2026-10-26T14:36:34Z). **Preview, not production-ready.** This page separates what the
code implements, what blocks a release, what the owner has requested but is not built
yet, and what is optional or out of scope. Test evidence lives only in
[validation.md](validation.md); nothing on this page is a test result. The ordered,
tickable list is [release-checklist.md](release-checklist.md). Detailed behaviour is
owned by [mcp.md](mcp.md), [native-integrations.md](native-integrations.md),
[tickets.md](tickets.md), [hosting.md](hosting.md) and [kits.md](kits.md); where they
disagree with this page on detail, theirs win.

## 0. Current evidence (summary; validation.md is authoritative)

| What | Latest recorded | Covers the current tree? |
|---|---|---|
| Python suite, PostgreSQL 15 / Python 3.12, Ruff, system check, migration drift | **1,244 passed** (2026-10-26T14:36Z) | Yes |
| Site (`site/`) tests, typecheck, production build | **50 tests** passed (2026-10-26T14:36Z) | Yes |
| OCI image | `artifacts/pi-build-d49e1bea-a7ab-4752-88c4-7b59259f0a08/image.tar` (2026-10-26) | Built from the current tree. **No SHA-256 recorded; never loaded, run or smoke-tested** |
| Python suite on PostgreSQL 16 + logical and application restore probe (`tests/run-pg16.sh`, `tests/restore_probe.py`) | 1,274 passed, restore/decrypt/wrong-key/admin probe passed (2026-10-26, Python3.12.15/PostgreSQL16.15) | **Yes, from source.** Does not prove built-image runtime |
| Transitive hash lock (`requirements.in` → hashed `requirements.txt`), hash-enforced install | Adopted 2026-10-24 | Yes, unless dependencies changed since; dev packages not hash-locked |
| `pip-audit` | No known vulnerabilities (2026-10-24, 837 tree) | Rerun for the release tree |
| SDK + kit tests | 60 passed (2026-10-25T04:49Z) | Not rerun at the 1,244 tree |
| Actual-app Chromium studio journey | 12/12 (2026-10-25T04:49Z) | Not rerun at the 1,244 tree |
| Mocked-backend SDK Chromium (`tests/browser-sdk.cjs`) | 8/8 in an early wave | No; predates web-calendar links and paid/verification SDK states |
| Live demo | Frontend `visioncast-ten.vercel.app` updated 2026-10-26 (catalogue lists ten adapters) | **Backend is the 2026-10-24 build** (no tickets, hosting or the newer adapters); no worker, email or destinations |
| Real vendor, Stripe, hosting-provider, SMTP or receiver traffic | None | — |

## 1. Implemented in code (preview only; gates in §2–§3)

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
| Publication readiness on studio, REST, MCP and Django admin (incl. same-POST inline sessions) | `webinars/services.py`, `admin.py` |
| Studio, admin, roles, audit, `purge_personal_data` | `webinars/studio/`, `admin.py`, `roles.py` |
| MCP `/mcp` incl. ticket/hosting parity tools (gated where they mutate) | `webinars/mcp.py`, `mcp_config.py`; [mcp.md](mcp.md) |
| Ten native adapters: PostHog, Customer.io, HubSpot, Attio, Segment, GA4, Amplitude, Mixpanel, RudderStack, Brevo. Multi-step sends stop if destination config changes mid-send (2026-10-26 fix). **Mock-tested only** | `webinars/native_integrations.py`, `delivery.py`; [native-integrations.md](native-integrations.md) |
| Capacity and Stripe Checkout tickets with mailbox verification before holds. **Stripe mocked only** | `webinars/capacity.py`, `payments.py`, `api/tickets.py`; [tickets.md](tickets.md) |
| Hosting: Zoom create/update/attach; Wistia, Sequel create; Riverside attach + per-registrant links; StreamYard manual link; operation reconciliation. **No adapter sandbox-verified** | `webinars/hosting/`, `api/hosting.py`; [hosting.md](hosting.md) |
| Website kits: WordPress plugin, framework/headless/proxy references, no-code and CMS schema references. Invariant tests pass; **not run against real WordPress, frameworks or CMSs** | `sdk/kits/`; [kits.md](kits.md) |

## 2. Release blockers

Must be closed or explicitly accepted by the owner before production use.

### 2a. Evidence on the exact release tree and image (Pi can do locally)

| # | Blocker |
|---|---|
| R1 | **Closed for source verification, 2026-10-26:** Python3.12.15/PostgreSQL16.15 passes 1,274 tests plus logical/application restore, decryption, wrong-key rejection and authenticated admin request. Built-image runtime proof remains outstanding (R2). |
| R2 | **Image digest and runtime.** Record the SHA-256 of the d49e1bea archive (or of a rebuild), then load it and run the Compose smoke test (`compose.yaml`, `postgres:16-alpine`): migrate, `sync_roles`, `create_api_key`, web and worker healthchecks, `/healthz`, studio login, OpenAPI, `/mcp`. Never done for any image. |
| R3 | **Rerun SDK/kit tests, actual-app Chromium and mocked-backend SDK Chromium** on the release tree; add browser scenarios for web calendar links and the paid / verification-required SDK states (unit tests only today). |
| R4 | **Base-image digest pins.** `Dockerfile` `PYTHON_IMAGE` and `compose.yaml` Postgres are still tags. Resolve, pin, rebuild. |
| R5 | **`pip-audit` on the release lock**, and record findings. |
| R6 | **Local operational rehearsals:** retention dry run, local webhook receiver contract (`examples/verify_webhook.py`: valid, tampered, stale, duplicate), `nginx -t` on `docker/nginx.conf.example` (untested). |

### 2b. Known code defects and gaps still open

| # | Blocker |
|---|---|
| R7 | **Closed: review finding disproven.** Delivery metadata is always retained by the outbox, and delivery serialization restores the delivery ID. Regression coverage confirms it survives omission from destination groups; no extra enable-time restriction is needed. |
| R8 | **Closed:** studio previews now show bounded refusal codes and guidance without exposing exception text, secrets or personal data. PostgreSQL15 and PostgreSQL16 suites pass after this fix (1,253 tests each). |
| R9 | **Closed in source:** REST, studio and MCP now expose stalled-registrant reconciliation with scoped authorization, private-link redaction, state-bound confirmation and an attempt fence under the service row lock. 1,274 PostgreSQL15 tests pass; no live Riverside account verification. |
| R10 | **Closed:** enabled attached Riverside sessions can open without a shared link. Admission requires a valid personal link bound to the current provider account and event. Migration0010 leaves legacy links unbound: operators must reconcile them; no blind backfill. 1,320 PG16/Python3.12 tests pass. |

### 2c. Vendor contracts (owner accounts required)

| # | Blocker |
|---|---|
| R11 | **Native adapters stay disabled** until each is checked in an owner-approved sandbox: HubSpot (409 on email collision), Attio (`matching_attribute`, list entries), PostHog and Customer.io dedupe, Segment, GA4, Amplitude, Mixpanel, RudderStack (auth ambiguity, regional hosts) and Brevo (contacts may become campaign-eligible under vendor defaults; event retries may duplicate automation triggers). Vendor writes are never recalled; no vendor-side erasure. |
| R12 | **Stripe test mode** for the full paid flow: Checkout create, `expires_at` minimum, idempotency replay, expire endpoint, all five webhook events, refunds and disputes. Mocked only. |
| R13 | **Hosting provider sandboxes:** Zoom Webinars, Wistia Webinars, Sequel, Riverside (endpoint paths are our best reading). |
| R14 | **Real SMTP relay** — required, not optional, if paid tickets are sold (mailbox verification) or invitations / room codes are used. |
| R15 | **Real webhook receiver(s)** the owner will use. |
| R16 | **Kit compatibility:** install the WordPress plugin on a real WordPress ≥ 6.3 / PHP ≥ 7.4 site (incl. subdirectory install, caching plugin), and exercise at least the frameworks the owner will ship (Next.js self-hosted and Vercel, Astro, Nuxt, SvelteKit). Webflow/Framer reverse-proxy feasibility, Astro middleware, Nuxt `routeRules`, Payload 2 vs 3, Prismic Slice Machine and Contentful CLI formats are unverified. |
| R17 | **Web calendar deep links** (Google/Outlook/Microsoft 365) against the live services, or accept unverified. The endpoint is duplicated in `calendar_links.py`, `sdk/proxy/server.js` and the SDK. |

### 2d. Owner policy acceptances

| # | Decision |
|---|---|
| R18 | Staff publish without confirmation tokens; admin inline-add fails closed; residual cancel/reschedule race; `team_admin` ≥ `content_write` + `publication`. |
| R19 | Confirmation tokens are not human approval. |
| R20 | `config_read` reads drafts over REST, or request a `content_read` scope (none exists). |
| R21 | Direct-mode ICS rewrite default: Mode B edges must forward `/webinar-api/calendar.ics` (and `my-registration`) or set `WEBINAR_DIRECT_ICS_REWRITE=false`. |
| R22 | Paid sessions require the waiting room and mail; join links sent to webhook destinations before a refund cannot be recalled. |
| R23 | Residual behaviour in §5. |

## 3. Operational gates (target environment; operator/owner)

- A production host (the demo droplet runs the old backend without a worker; it is a demo,
  not a release target unless the owner says so). Provisioning is the operator's job.
- TLS, HTTP→HTTPS redirect, edge rate limits on `/webinar-api/*` (incl. the proxy's
  `my-registration`), `/w/`, `/mcp`, `/admin/`, `/studio/login/`, `/api/v1/payments/stripe/webhook`;
  MFA/SSO/VPN in front of `/admin/` and `/studio/`. Platform limits are per-process only.
- `RATE_LIMIT_TRUST_X_FORWARDED_FOR` only behind a trusted proxy chain of the configured depth.
- `INTEGRATION_MASTER_KEY` stored outside backups; scheduled `pg_dump`; a restore drill on
  the target host (the synthetic PG16 probe is not a production-volume drill).
- Worker process running (outbox, reminders, Stripe sweep, hosting sweep, stale operations).
- `WEBHOOK_HOST_ALLOWLIST` entries for every enabled vendor, Stripe and hosting host; https
  `PUBLIC_BASE_URL`; `PUBLIC_EVENT_URL_TEMPLATE`; `manage.py check` on every deploy.
- Stripe webhook endpoint registered with the five events in [tickets.md](tickets.md).
- Cron for `purge_personal_data`.
- Existing deployments: `migrate` (0008, 0009 are new), then `sync_roles`.
- Launch acceptance in [implementation-guide.md §11.1](implementation-guide.md#111-launch-acceptance-run-on-staging-record-results-in-validationmd) on staging.
- Upgrading the live demo backend to the current tree is separate owner-approved work.

## 4. Requested feature backlog (owner asked for these; not yet built)

These are part of the requested scope (CONTINUITY "full scope still OPEN"), not optional.

| Item | State |
|---|---|
| Further CRM / marketing-automation / transactional-email native adapters (Salesforce, Pipedrive, other marketing-automation beyond Brevo, transactional email) | Not built. [native-integrations.md](native-integrations.md) lists them as the next wave. Signed webhooks are the interim path. |
| Riverside registrant reconciliation route (REST, studio, MCP) | Implemented and regression-tested; live-provider verification remains required. |
| Provider attendance import (Zoom, Wistia, Sequel, Riverside) | Not built. |
| Hosting update for Wistia/Sequel; Riverside create | Not built (Riverside create is undocumented by the vendor). |
| Live demo backend upgrade to the current tree | Not done; frontend already advertises the features. |

## 5. Known residual behaviour (accepted or documented, not blockers)

| Item | Notes |
|---|---|
| Proxy `POST /webinar-api/registrations` 2xx body | Passed through apart from the `icsUrl` rewrite. |
| Thank-you `redirectUrl` | Any https URL; the proxy does not strip credentials. |
| "Recognized" oracle | `/profile` and `my-registration` reveal whether the browser holds a real capability. |
| `PUBLIC_EVENT_URL_TEMPLATE` check | `webinars.E101` does not run at Gunicorn start; run `manage.py check`. |
| Rate limits | Per-process, not global. |
| Stripe key rotation | Refused while orders are open. |

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
| Global rate limiting, built-in TLS/MFA, built-in retention scheduler | Edge / cron. |
| MCP SSE/sessions/stdio | Stateless JSON over HTTP. |
| Over MCP: environment settings, host provisioning, Administrators/superusers, secrets, join/invitation links, registrant PII | Excluded by design ([mcp.md](mcp.md)). |

## 7. Out of scope by design (do not build)

- Streaming, video/recording hosting, gating or view tracking. Check-in measures admission.
- Attendee confirmation/reminder/follow-up email and SMS sending (ESP via webhooks; SMS
  consent evidence only).
- Cross-device recognition and account recovery ([security.md](security.md)).
- Multi-tenancy.
