# Release checklist

Status as of 2026-10-29 (release candidate `0.3.7-pre`, tag `v0.3.7-pre`: studio visual pass incl. callout styling, on top of `0.3.5-pre` (PostHog, Amplitude and Mixpanel identity by e-mail with anonymous-visitor merge), `0.3.3-pre` (native hosts need no allowlist entry, mandatory consent on save), `0.3.2-pre` (integration form) and `0.3.1-pre` (host deletion); `0.3.0-pre` tag `v0.3.0-pre` at `12c96f4`). **Nothing on
this page is evidence.** Results are recorded only in [validation.md](validation.md); the
gaps they close are in [remaining-gaps.md](remaining-gaps.md) (R1–R28). "Last recorded"
quotes validation.md.

- **A. Local checks** a release validator can run in disposable containers (no messages, no
  third-party services; some fetch packages or images).
- **B. Code fixes** still required before release.
- **C. Owner accounts, approvals and decisions.**
- **D. Operator tasks** on the target environment.

Release only when every A item has a recorded pass on the exact tree and image being
released, every B item is fixed and retested, every C item is approved or explicitly
accepted, and every D item is in place. Any code change (including B) re-opens A.

## A. Local executable checks

| # | Check | How | Last recorded |
|---|---|---|---|
| A1 | Python suite, system check, migration drift, Ruff | `tests/run-pg.sh` (PG15) or `tests/run-pg16.sh` | **2,372 passed**, clean, under the PG16 harness on `24e3493` (2026-10-27). Release tree |
| A2 | Site tests, typecheck, production build | `site/`: `npm test`, `npm run typecheck`, `npm run build` | **51 passed**, typecheck and build passed (2026-10-27). Release tree |
| A3 | SDK, proxy, kit and automation-recipe tests | `npm test` in `sdk/`; `node --test sdk/kits/test/kits.test.js sdk/kits/automation/test/automation.test.js` | **63 + 22 passed** (2026-10-27). Integrations tree |
| A4 | Actual-app Chromium: studio journey, Riverside, GTM | `tests/browser-studio.cjs`, `browser-riverside.cjs` (+ `riverside_browser_seed.py`), `browser-gtm.cjs` against the preview settings | **13/13, 8/8, 10/10** (2026-10-27). Release tree |
| A5 | Mocked-backend SDK Chromium incl. web calendar links, `payment_required`, `verification_required` | `tests/browser-sdk.cjs` | **11/11** (2026-10-27). Release tree |
| A6 | Python suite on PostgreSQL 16 + logical and application restore probe | `tests/run-pg16.sh` (includes `tests/restore_probe.py`) | **2,372 passed**, restore/decrypt/wrong-key/admin probe passed (2026-10-27, Python 3.12.16 / PostgreSQL 16.15). Release tree |
| A7 | Base-image digests | `PYTHON_IMAGE` (`Dockerfile`) and Postgres (`compose.yaml`) pinned by `@sha256` | **Pinned** (2026-10-27); refresh procedure in README |
| A8 | Dependency audit | `pip-audit` on the hash lock; dev pins separately | **No known vulnerabilities** (2026-10-27); dev `pytest` 9.0.3 |
| A9 | OCI image and digest | `deploy/build-image.sh --save`; `release.yml` | `0.3.0-pre` arm64 from `24e3493`: image ID `sha256:8934f6ef…`, tar SHA-256 `e08db362…` (2026-10-27). Published `0.2.0-pre`: digest `sha256:c81ad8b1…`; published `0.3.0-pre` digest `sha256:fa723b88…` (amd64 + arm64) |
| A10 | Built-image runtime and Compose first run | README quick start against `WEBINAR_IMAGE` | **Passed on the `0.3.0-pre` arm64 image** (2026-10-27) and earlier on the `0.2.0-pre` arm64 and emulated amd64 images: migrate, `sync_roles`, `bootstrap_admin`, `create_api_key`, seed, web + worker healthy, `/healthz` version, OpenAPI, `/mcp`, studio login page, uid 10001. Published manifests not pulled from here (no `read:packages` token) |
| A11 | Backup/restore from the A10 database | `deploy/backup.sh`, `deploy/restore.sh` | **Passed** (2026-10-27, image from `3ec60ad`): restore into a fresh project, migrate no-op, secrets decrypt only with the original key, `--force` guard. Backup repeated on the release image |
| A12 | Retention dry run | `purge_personal_data --older-than-days N --dry-run` on A10 data | **Passed** (2026-10-27), counts only |
| A13 | Webhook receiver contract (offline) | `tests/test_verify_webhook_example.py` | **8 tests pass** (valid, tampered, stale, duplicate, wrong secret, rotation) |
| A14 | Edge config syntax and routing | `nginx -t` on `docker/nginx.conf.example` and `sdk/kits/node/nginx-webinar-api.conf`; routing and rate-limit checks | **Passed after fixes** (2026-10-27, nginx 1.30 / 1.27) |
| A15 | WordPress plugin lint and fresh install | `php -l`; `bash sdk/kits/test/fresh-install.sh` (WordPress 7.1 / PHP 8.2 in Docker, block + shortcode + cache + subdirectory) | **Passed** (2026-10-27) |
| A16 | Framework and CMS kits from fresh scaffolds | `bash sdk/kits/test/fresh-install.sh` (Next 16, SvelteKit 3, Astro 7, Nuxt 4, static snippet, Sanity/Payload/Prismic/Contentful validation) | **Passed** (2026-10-27) |
| A17 | End-to-end acceptance workflow | `tests/test_e2e_acceptance.py` (two scenarios: waiting-room attendance; Zoom attendance import + no-shows) | **Passed** (2026-10-27). Release tree |
| A18 | In-place upgrade and rollback | `deploy/upgrade.sh --image …` from the previous release image; rollback by `WEBINAR_IMAGE` | **Passed** `da04cc7` → `3ec60ad` (no schema change, 2026-10-27). **Real host:** 0.3.0-pre → 0.3.1-pre on events.growthcast.app via `update.sh` (2026-10-29, no schema change, verify 12/12); rollback not exercised there |

## B. Code fixes required

None open. Closed this cycle (retest covered by A):

| # | Fix | Gap |
|---|---|---|
| B1–B4 | Closed 2026-10-26 (outbox metadata disproven, studio refusal UX, Riverside reconciliation, personal-link binding) | R7–R10 |
| B5 | nginx kit snippet `^~` shadowing and missing example locations | R25 |
| B6 | WordPress cookie-expiry/subdirectory SDK path, Node proxy quoted cookie, Next 16 build, Svelte 5 runes | R26 |
| B7 | Hosting transport tests updated for native `GET`/`PATCH` lookups; `CREDENTIAL_HEADERS` extended (`api-token`, `x-api-token`) | — |

## C. Owner accounts, approvals and decisions

None is approved by this document. External calls need explicit approval and owner-held accounts.

| # | Item | Gap | Needs owner account |
|---|---|---|---|
| C1 | Native adapter sandboxes, one per adapter to be enabled (HubSpot, Attio, PostHog, Customer.io, Segment, GA4, Amplitude, Mixpanel, RudderStack, Brevo, Salesforce, Pipedrive, ActiveCampaign, Mailchimp, beehiiv, Resend, Amazon SES); otherwise keep disabled | R11 | Yes |
| C2 | Stripe test-mode run of the full paid flow, webhooks, refunds, disputes | R12 | Yes (Stripe) |
| C3 | Hosting sandboxes: Zoom Webinars licence (provisioning and participant report), Wistia, Sequel, Riverside (attach, registrants, attendance) | R13 | Yes |
| C4 | Real SMTP relay test to owner mailboxes (mandatory for paid tickets and invitations) | R14 | Yes |
| C5 | Real webhook receiver test over the internet; import the Zapier, Make and n8n recipes on real accounts | R15 | Yes |
| C6 | Kit compatibility on the owner's real sites and hosts (https hosts, CDN, Vercel, Webflow/Framer plans, paid sessions through a kit) | R16 | Yes (hosting / CMS / plan accounts) |
| C7 | Web calendar deep links against live Google/Outlook/Microsoft 365, or accept unverified | R17 | Yes (test calendars) |
| C8 | Accept publication authority, confirmation-token, `config_read` draft-read, paid-flow, attendance-import, marketing-status (incl. the beehiiv subscription exception) and transactional-email policies | R18–R22, R27–R29 | No |
| C9 | Accept residual behaviour (remaining-gaps §5) and the A7 digests | R23 | No |
| C10 | Scope decisions recorded 2026-10-27: beehiiv, Resend, Amazon SES, Zapier, Make and n8n built; Intercom, Loops and Marketo skipped; Postmark and HubSpot Marketing not committed (remaining-gaps §4) | §4 | No |
| C11 | Approve a staging host and run launch acceptance ([implementation-guide.md §11.1](implementation-guide.md#111-launch-acceptance-run-on-staging-record-results-in-validationmd)) | §3 | Yes (host) |
| C12 | Approve live demo backend upgrade and frontend catalogue redeploy, production deployment, any preview renewal. **growthcast.app Events pages live 2026-10-28** (unavailable state until `WEBINAR_API_BASE` and `WEBINAR_PROXY_KEY` are set on the Vercel project and the studio has a published event) | — | Yes |
| C13 | **Done:** `v0.2.0-pre`, `v0.3.0-pre` and `v0.3.1-pre` tags pushed; `ghcr.io/b2b-saas-inc/visioncast:0.3.0-pre` (digest `sha256:fa723b88…`) and `:0.3.1-pre` (run 40784413037) published (amd64 + arm64, private). **Remaining:** decide package visibility or grant deploy hosts a `read:packages` token; run the published manifest on a real amd64 host (C11) | R24 | Yes (GitHub) |

## D. Operator tasks (target environment)

For the `events.growthcast.app` droplet install, [deploy/production/README.md](../deploy/production/README.md)
covers D1, D2, D5, D6, D9, D10, D12 and D13 with scripts (rehearsed locally only) and
[deploy/production/cloudflare.md](../deploy/production/cloudflare.md) the Cloudflare part of D1–D3.

| # | Task | Gap |
|---|---|---|
| D1 | TLS with HTTP→HTTPS redirect | §3 | **In place** on events.growthcast.app (Let's Encrypt origin, Full strict + Always Use HTTPS at Cloudflare, 2026-10-29)
| D2 | Edge rate limits on `/webinar-api/*` (incl. proxy `my-registration`), `/w/`, `/mcp`, `/admin/`, `/studio/login/`, `/api/v1/payments/stripe/webhook` (`docker/nginx.conf.example`) | §3 | **In place**: nginx zones plus one Cloudflare rate-limiting rule (Free plan) on events.growthcast.app, 2026-10-29
| D3 | MFA, SSO or VPN in front of `/admin/` and `/studio/` | §3 | **In place**: Cloudflare Access (One-time PIN, owner policy) on events.growthcast.app, verify `--expect-access` 12/12, 2026-10-29
| D4 | Mode B edges forward `/webinar-api/calendar.ics` (and `my-registration`), or set `WEBINAR_DIRECT_ICS_REWRITE=false` | R21 |
| D5 | `RATE_LIMIT_TRUST_X_FORWARDED_FOR` only behind a trusted chain of matching depth | §3 |
| D6 | Worker process running | §3 |
| D7 | `WEBHOOK_HOST_ALLOWLIST` for generic webhook receivers, self-hosted RudderStack data planes and n8n hosts, `api.stripe.com`, hosting hosts (native vendor hosts are pinned in code and need no entry unless `NATIVE_VENDOR_HOSTS_IMPLICIT=0`); https `PUBLIC_BASE_URL`; `PUBLIC_EVENT_URL_TEMPLATE` | §3 |
| D8 | Stripe webhook endpoint with the five events from [tickets.md](tickets.md) | §3 |
| D9 | `INTEGRATION_MASTER_KEY` outside backups; scheduled `deploy/backup.sh --encrypt-to …`; restore drill with `deploy/restore.sh` on the target host | §3 |
| D10 | Cron for `purge_personal_data` | §3 |
| D11 | Existing deployments: `deploy/upgrade.sh --image <image>` (migrates 0008–0011, `sync_roles`); marketers to `/studio/` | §3 |
| D12 | `manage.py check` on every deploy | §5 |
| D13 | Pin `WEBINAR_IMAGE` to the exact published tag (or the locally built `<version>-<commit>` tag) in `.env`; never `latest` | §3 |
