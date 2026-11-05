# Remaining gaps

Current release: `0.4.10-pre`, published to GHCR as a private package on every tag (`release.yml` has run on each tag from `v0.2.0-pre` through `v0.4.10-pre`). The sections below were reconciled on 2026-11-05 against `0.4.10-pre`. **What remains needs vendor accounts, an operator's decision, or a real event with real people.** Results from any particular deployment are recorded in [validation.md](validation.md) only; this page describes the project. This page separates what the code implements, what blocks a release, what the
owner has requested but is not built yet, and what is optional or out of scope. Test evidence
lives only in [validation.md](validation.md); nothing on this page is a test result. The
ordered, tickable list is [release-checklist.md](release-checklist.md). Detailed behaviour is
owned by [mcp.md](mcp.md), [native-integrations.md](native-integrations.md),
[tickets.md](tickets.md), [hosting.md](hosting.md), [kits.md](kits.md) and
[operations.md](operations.md); where they disagree with this page on detail, theirs win.

## 0. Current evidence (summary; validation.md is authoritative)

| What | Latest recorded | Covers the release tree? |
|---|---|---|
| Python suite, Ruff, system check, migration drift, on PostgreSQL 16 | **2,557 passed, 1 skipped** (the LiveKit stack test), lint and `makemigrations --check` clean on `8607c72` = `v0.4.10-pre` (2026-11-04) | Yes |
| `tests/run-pg16.sh` harness with the logical + application restore probe | 2,372 passed and the restore probe passed on `24e3493` (2026-10-27) | No: last run on the 0.3.0-pre tree; the suite has run on every tree since, the restore probe has not |
| End-to-end acceptance (`tests/test_e2e_acceptance.py`): create → provision (Zoom) → register/pay → verify → attend → CRM follow-up | 2 scenarios, fake vendors; part of the suite above | Yes |
| Live service (`live/`): HTTP and unit tests | **72 + 18 pass**, clippy clean on touched files (2026-11-04, `v0.4.9-pre`) | Yes |
| Live service in real browsers on a real LiveKit stack: every role 36/36, newer features 24/24, Chromium/Firefox/WebKit/phone screens 8/8, attack suite 26/26, load 1,000 event streams + 100 media viewers, captions 11.2% WER | All on 2026-11-01 on branch `livekit-rust`, merged unchanged into `0.4.0-pre`; all on one machine | Yes for 0.4.0-pre; later live changes (brand font, clean addresses, tab icon) covered by the HTTP tests only |
| Site (`site/`) tests, typecheck, production build | 51 passed (2026-10-27); the site's test count was refreshed for 0.4.7-pre | Partly: not re-recorded in validation.md since 2026-10-27 |
| SDK unit, proxy, kit and automation-recipe tests; mocked-backend SDK Chromium (11/11); Riverside and GTM Chromium (8/8, 10/10); website kits from fresh vendor scaffolds | All passed 2026-10-27 on the 0.3.0-pre tree | No: not rerun since. The studio Chromium journey was rerun 13/13 on 2026-10-29 (0.3.7-pre) |
| Versioned OCI image | Built and published by `release.yml` on every tag through `v0.4.10-pre` (run 41891908002, amd64 + arm64, SBOM + provenance) | Yes, but no published manifest has been pulled and run from here |
| Clean-server Compose first run, backup/restore, retention dry run, offline receiver contract, `nginx -t` + routing, upgrade and rollback in Docker | All passed 2026-10-27 (0.3.0-pre tree) | No: not rerun since; migrations 0012–0017 were added after that run |
| Base-image digest pins, `pip-audit` on the hash lock | Pinned; no known vulnerabilities (2026-10-27) | Not rerun since 2026-10-27 |
| Real vendor traffic from this repository's test suites | **None.** Every vendor, Stripe, hosting-provider, SMTP, registry and receiver call is mocked or runs against a local stand-in. What an operator has exercised live on their own deployment is in [validation.md](validation.md) only | — |

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
| **Seventeen native adapters**: PostHog, Customer.io, HubSpot, Attio, Segment, GA4, Amplitude, Mixpanel, RudderStack, Brevo, and (2026-10-27, one module each under `webinars/native_providers/`) **Salesforce, Pipedrive, ActiveCampaign, Mailchimp, beehiiv** (newsletter subscription with explicit acknowledgements), **Resend and Amazon SES** (transactional attendee email rendered by the platform; stdlib SigV4 for SES). Provider modules may declare their own event types and required groups. Contract points marked VERIFIED (official page fetched) or UNVERIFIED per adapter. **Mock-tested only** | `webinars/native_integrations.py`, `native_providers/`, `delivery.py`; [native-integrations.md](native-integrations.md), [email.md](email.md) |
| **Workflow-automation presets** for Zapier, Make and n8n over the unchanged signed webhook (URL rules, pinned defaults, recipe links) plus importable verification recipes exercised by a Node simulation | `webinars/webhook_presets.py`, `sdk/kits/automation/`; [webhooks.md](webhooks.md) |
| Capacity and Stripe Checkout tickets with mailbox verification before holds. **Stripe mocked only** | `webinars/capacity.py`, `payments.py`, `api/tickets.py`; [tickets.md](tickets.md) |
| Hosting: Zoom create/update/attach; Wistia, Sequel create; Riverside attach + per-registrant links + reconciliation; StreamYard manual link. **No adapter sandbox-verified** | `webinars/hosting/`, `api/hosting.py`; [hosting.md](hosting.md) |
| **Provider attendance import** (2026-10-27): authenticated Zoom participant pull (documented endpoint, paginated), Riverside registrant pull (UNVERIFIED constants), JSON/CSV file import for every provider; id-then-email matching, dedupe, ambiguity review, idempotent apply through `record_attendance` (`attendance.recorded` once per registration), separate confirmed no-show pass; REST, MCP and studio. Migration `0011` | `webinars/attendance_import.py`, `api/attendance_imports.py`; [hosting.md](hosting.md) "Attendance import" |
| Website kits: WordPress plugin, framework/headless/proxy references, no-code and CMS schema references, fresh-install verification script | `sdk/kits/`; [kits.md](kits.md) |
| Distribution: `VERSION`, `show_version`, `/healthz` version, OCI labels, `deploy/build-image.sh`, `backup.sh`, `restore.sh`, `upgrade.sh`, `WEBINAR_IMAGE` in Compose, GHCR release workflow (runs on every tag) | `deploy/`, `Dockerfile`, `compose.yaml`, `.github/workflows/release.yml`; [operations.md](operations.md) |
| **Production deployment layout** for one DigitalOcean droplet behind Cloudflare: provision, bootstrap, package a tag, `update.sh` (backup → migrate → restart → `sync_roles` → health), `verify.sh`, nightly encrypted backups, retention cron | `deploy/production/`; [deploy/production/README.md](../deploy/production/README.md) |
| **Live add-on** (0.4.0-pre): self-hosted LiveKit studio with roles, green room, personal links, engagement, captions, recordings, brand, RTMP out; its own update and backup scripts | `live/`, `deploy/live/`; [live/README.md](../live/README.md) |
| **Brand** (0.4.2-pre): any colour and a brand font on the studio, waiting room and live rooms; pushed to the live add-on, whose tab icon follows it (0.4.9-pre) | `webinars/brand.py`, `live/src/broadcast.rs`; [branding.md](branding.md) |
| **Email through PostHog** (0.4.3–0.4.4-pre): `EMAIL_DELIVERY=posthog` for the platform's own mail and a PostHog transactional-email destination for attendee mail | `webinars/mail.py`, `native_providers/`; [email.md](email.md) |
| **Automatic rooms and full delete** (0.4.7-pre): a hosting account marked `auto_provision` gives every new session its room and waiting room on commit; events and sessions can be deleted at any visibility while nobody is registered | `webinars/hosting/`, `services.py`; [hosting.md](hosting.md) |
| **Card image** (0.4.8-pre) and **dashboard setup checklist** hide / auto-dismiss (0.4.10-pre); calendars carry the waiting room link (0.4.5-pre) | `webinars/models.py`, `studio/views_content.py`; [studio.md](studio.md) |

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
| R24 | **Mostly closed:** `release.yml` has published amd64 + arm64 to GHCR on every tag through `v0.4.10-pre`; the amd64 first run passed under emulation from the tagged tree. **Open:** pull a published manifest (`read:packages` token or a public package) and run it on a real amd64 host. |

### 2b. Known code defects and gaps still open

| # | Blocker |
|---|---|
| R7–R10 | **Closed** (see validation.md 2026-10-26 entries): outbox delivery metadata, studio preview refusals, Riverside reconciliation and personal-link binding. |
| R25 | **Closed 2026-10-27:** `sdk/kits/node/nginx-webinar-api.conf` used `location ^~`, which made every proxied kit route answer 404 behind nginx; `docker/nginx.conf.example` lacked locations for `my-registration`, `calendar.ics`, `/w/`, `/studio/login/`, `/mcp` and the Stripe webhook. Both fixed and verified with `nginx -t` and routing checks. |
| R26 | **Closed 2026-10-27:** the WordPress plugin dropped Django's cookie-expiry `Set-Cookie` on reset and emitted a doubled subdirectory path for a self-hosted SDK; the Node proxy had the same quoted-cookie bug; the Next.js reference page did not build under Next 16 `cacheComponents`; the Svelte component did not compile under Svelte 5 runes. All fixed by fresh-install verification. |

### 2c. Vendor contracts (owner accounts required)

| # | Blocker |
|---|---|
| R11 | **Native adapters stay disabled** until each is checked in a sandbox the operator approves (any adapter already exercised live is noted in [validation.md](validation.md)): HubSpot (409 on email collision), Attio (`matching_attribute`, list entries), PostHog and Customer.io dedupe, Segment, GA4, Amplitude, Mixpanel, RudderStack (auth ambiguity, regional hosts), Brevo (campaign-eligible contacts under vendor defaults), **Salesforce** (client-credentials token flow unreadable in docs; composite upsert by External ID; `v62.0`), **Pipedrive** (v2 API; rate limits; `due_time` zone; activity dedupe scan limited to 500), **ActiveCampaign** (date custom-field format; regional hosts other than `api-us1.com`; account automations may fire), **Mailchimp** (`<dc>.api.mailchimp.com` form; whether API-created `pending` members get the opt-in mail; unknown merge tags), **beehiiv** (what a create returns for an existing email; tag re-add idempotency; API-key page unreadable), **Resend** (text/html size limits; 409 after a template change), **Amazon SES** (signer proven only against the AWS test vectors; non-ASCII subjects; sandbox/quotas; no idempotency, so a retry after a timeout can send twice). Vendor writes are never recalled; no vendor-side erasure. |
| R12 | **Stripe test mode** for the full paid flow: Checkout create, `expires_at` minimum, idempotency replay, expire endpoint, all five webhook events, refunds and disputes. Mocked only. |
| R13 | **Hosting provider sandboxes:** Zoom Webinars (create/update/attach **and** `past_webinars/{id}/participants` attendance pull), Wistia, Sequel, Riverside (attach, registrants, attendance: paths are our best reading). **LiveKit (self-hosted)** is covered by the live add-on's own tests against a real LiveKit stack; what it still lacks is in the live add-on section below. |
| R14 | **Real email delivery** — required, not optional, if paid tickets are sold (mailbox verification) or invitations / room codes are sent: an SMTP relay, or `EMAIL_DELIVERY=posthog` with a PostHog project. Both are mock-tested only in this repository. |
| R15 | **Real webhook receiver(s)** the owner will use (the receiver contract is verified offline only). The **Zapier, Make and n8n recipes** ran only as a Node simulation on backend-signed fixtures: import each on a real account/instance (Make blueprint structure and n8n node parameter names are the likeliest to need adjustment). |
| R16 | **Kit compatibility on the owner's real sites.** Fresh-install verification covered WordPress 7.1 / PHP 8.2 (block, shortcode, WP Super Cache, `/blog/` subdirectory), Next 16 self-hosted, SvelteKit 3 / Svelte 5, Astro 7 (behind nginx), Nuxt 4 (behind nginx), a static snippet behind nginx, and the four CMS schemas with vendor packages. **Not verified here:** https hosts other than localhost, CDN caches in front of `/webinar-api`, Vercel route handlers, real Webflow/Framer hosting, paid sessions through a kit, Next 14/15, Nuxt 3, Svelte 4, Astro middleware mode, WordPress multisite. |
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
| R28 | Marketing adapters never subscribe anyone: Mailchimp members default to `transactional` (pending double opt-in needs an explicit acknowledgement; `subscribed` is impossible), ActiveCampaign contacts are never added to lists or automations. **beehiiv is the deliberate exception**: its only record is the subscription, so it subscribes registrants (free tier, welcome email off and no double-opt-in override by default) behind an explicit `subscribe_ack`. Whether the vendor's own automations react to contact or tag changes is the operator's responsibility. |
| R29 | **Transactional attendee email** (Resend, Amazon SES) is now sent by the platform when such a destination is enabled: confirmation, reminder, new time, cancellation, attended / no-show, recording, follow-up. Plain-text templates with an allowlisted placeholder set, HTML generated from the text, no operator HTML; the join link is the managed room URL, or the provider link only when private links are allowed, else the public event page. This reverses the earlier out-of-scope decision (§7); the ESP-via-webhooks path still works and the operational SMTP for room codes is unchanged. |

## 3. Operational gates (target environment; operator/owner)

- A production host. Provisioning is the operator's job; `deploy/production/` is one complete
  layout (DigitalOcean droplet behind Cloudflare) and `deploy/quickstart/` the one-step installer.
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
- Existing deployments: `deploy/upgrade.sh --image <image>` (backup, `migrate` through 0017,
  restart, `sync_roles`, health); with the `deploy/production/` layout, `update.sh`.
- Launch acceptance in [implementation-guide.md §11.1](implementation-guide.md#111-launch-acceptance-run-on-staging-record-results-in-validationmd) on staging.

## 4. Requested feature backlog

Committed scope for this release is closed. What the owner asked for and is **not** built:

| Item | State |
|---|---|
| Salesforce, Pipedrive, ActiveCampaign, Mailchimp, **beehiiv** native adapters | **Built** (mock-tested; sandbox verification R11). Intercom, Loops and Marketo were explicitly skipped by the owner on 2026-10-27 and stay "roadmap" with HubSpot Marketing; nothing further is committed. |
| Transactional email adapters | **Resend and Amazon SES built** (owner-approved scope change, R29). Postmark is not committed and stays "roadmap". |
| Workflow automation (Zapier, Make, n8n) | **Built as destination presets plus verification recipes** over the signed webhook; recipes simulated in Node only (R15). |
| Provider attendance import | **Built** for Zoom (documented endpoint), Riverside (unverified) and file import for every provider. Wistia and Sequel expose no attendance API this release; their reports go through the file import. |
| Hosting update for Wistia/Sequel; Riverside create | Not built (Riverside create is undocumented by the vendor). |
| Self-hosted live studio (LiveKit) | **Built and released** (0.4.0-pre). |
| Brand colour and font, PostHog email, calendar waiting-room links, automatic rooms, delete at any visibility, card image, setup-checklist hide / auto-dismiss, live tab icon | **Built** (0.4.2-pre through 0.4.10-pre). |

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

- Streaming, video/recording hosting, gating or view tracking in the core application: the
  optional `live/` add-on does this on its own server (section on it below). In the core app,
  check-in and imported attendance measure admission or provider-reported presence, not viewing.
- SMS sending (consent evidence only). Attendee email is **no longer** out of scope: it is sent
  only when a Resend or SES destination is enabled (R29); otherwise the ESP sends it from
  webhooks as before.
- Cross-device recognition and account recovery ([security.md](security.md)).
- Multi-tenancy.

## Your own live server (LiveKit add-on)

Released in 0.4.0-pre. Tested on a real LiveKit stack in real browsers, on one machine ([validation.md](validation.md)).

**Needs a public server, a real event, or your own accounts.** Each has a script or a checklist:

- Certificates, firewalls and real networks: `deploy/live/verify-public.sh`.
- People on networks that only allow port 443: `live/tests/turn/check.cjs` against your server (a TURN relay over TLS on 443 through Traefik, with LiveKit's credentials).
- The studio talking to a live server by its public name and certificate; locally it's only been exercised over the test transport.
- A real session with real attendees.
- A real stream to YouTube, Meta, X and LinkedIn. So far only a local RTMP server has received one, over RTMP and RTMPS.
- A real bucket. A local S3-compatible server that checks every signature has taken recordings and captions, but Amazon S3 and DigitalOcean Spaces haven't.
- A load test on your own server: `live/tests/load/run.sh`. So far, on a laptop sharing its CPU with the load: 1,000 event streams and 100 simulated media viewers ([validation.md](validation.md)). No real browsers at that scale, and no recording under load.
- Safari itself, and real phones. So far: Playwright's WebKit build, including screen sharing, and phone screens in desktop browsers. Publishing a camera or microphone from Safari, and a transparent WebM graphic on a Safari viewer's screen, haven't been checked.
- Caption accuracy with your own speakers, names and jargon.

**Built, with limits you should know:**

- Slides lose animations and transitions. Notes come from PowerPoint files, not PDFs. A deck the converter never picks up shows as converting for up to 20 minutes, then fails. This happened once in testing and wasn't explained.
- RTMP input is RTMP only, not RTMPS, and an encoder is on stage for as long as its stream runs. There's no holding it in the green room.
- Translated captions are live only. Saved SRT and transcript files stay in the spoken language. Translation needs a language pack per language on your server.
- Speakers' own recordings stay on the live server's disk, listed with the recordings. They aren't copied to the bucket. A browser that can't keep up loses its own copy, not the main recording.
- `engagement.recorded` reaches webhook destinations and workflow presets. The native connectors (Salesforce, Pipedrive and the rest) don't send it yet.
- Surveys appear to people still on the page when the session ends. Nothing emails them afterwards unless you set that up from the event.
- Breakout rooms aren't recorded, captioned or included in attendance.
- `--local` on the installer shows the pages, but a session's room can't be created from the studio without real certificates.
- A personal link is in every request's address, so a proxy's logs see it and a copied download link carries it.
- Two-factor sign-in isn't built, for the studio or `/admin`. Put a VPN or SSO proxy in front.
- Removing someone from the studio's Team doesn't remove their host link from a session. Remove them under Crew.
- A replaced crew link's old LiveKit credential can watch for up to 15 minutes. It can't publish unless that person is on stage.
- A host or producer who deliberately modifies their browser could send their voice as a screen's audio.
- Go live is one press, with no undo.
- A poll's counts are in the event stream even with counts hidden on screen, so someone reading the stream can see them.
- Guests, and attendees brought on stage, see attendees' names in crew chat.
- A person can hold a breakout seat through `/act` without connecting, until the rooms close.
- Slides waiting in the converter's queue can be read by another job inside that container.
- iOS autoplay hasn't been checked on a real phone. If a browser blocks sound, the page offers a button to turn it on.
- After a page redraw, keyboard focus is restored for typing but not always for buttons.

**Not implemented:** captions from screen-share sound or video played on stage; pressing Go live on a platform for you; clustering across several LiveKit servers.
