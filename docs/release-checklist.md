# Release checklist

Status as of 2026-11-05 (release candidate `0.4.10-pre`, tag `v0.4.10-pre` at `8607c72`). Since `0.3.7-pre`: the self-hosted live studio on LiveKit (`0.4.0-pre`), brand colour and font (`0.4.2-pre`), email through PostHog (`0.4.3`–`0.4.4-pre`), calendar links to the waiting room (`0.4.5-pre`), live add-on update and backup scripts (`0.4.6-pre`), automatic rooms and delete at any visibility (`0.4.7-pre`), card images (`0.4.8-pre`), the live tab icon (`0.4.9-pre`) and the dashboard checklist (`0.4.10-pre`). Every tag published to GHCR by `release.yml`. **Nothing on
this page is evidence.** Results are recorded only in [validation.md](validation.md); the
gaps they close are in [remaining-gaps.md](remaining-gaps.md) (R-numbered). "Last recorded"
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
| A1 | Python suite, system check, migration drift, Ruff | `tests/run-pg.sh` (PG15) or `tests/run-pg16.sh` | **2,557 passed, 1 skipped** (LiveKit stack test), lint and `makemigrations --check` clean on `8607c72` (2026-11-04, PostgreSQL 16). Release tree |
| A2 | Site tests, typecheck, production build | `site/`: `npm test`, `npm run typecheck`, `npm run build` | **51 passed**, typecheck and build passed (2026-10-27, 0.3.0-pre tree). Not re-recorded since |
| A3 | SDK, proxy, kit and automation-recipe tests | `npm test` in `sdk/`; `node --test sdk/kits/test/kits.test.js sdk/kits/automation/test/automation.test.js` | **63 + 22 passed** (2026-10-27, 0.3.0-pre tree). Not rerun since |
| A4 | Actual-app Chromium: studio journey, Riverside, GTM | `tests/browser-studio.cjs`, `browser-riverside.cjs` (+ `riverside_browser_seed.py`), `browser-gtm.cjs` against the preview settings | Studio **13/13** (2026-10-29, 0.3.7-pre); Riverside **8/8** and GTM **10/10** (2026-10-27). Not rerun on 0.4.x |
| A5 | Mocked-backend SDK Chromium incl. web calendar links, `payment_required`, `verification_required` | `tests/browser-sdk.cjs` | **11/11** (2026-10-27, 0.3.0-pre tree). Not rerun since |
| A6 | Python suite on PostgreSQL 16 + logical and application restore probe | `tests/run-pg16.sh` (includes `tests/restore_probe.py`) | Restore/decrypt/wrong-key/admin probe passed on `24e3493` (2026-10-27, Python 3.12.16 / PostgreSQL 16.15). The probe has not rerun on 0.4.x; the suite has (A1) |
| A7 | Base-image digests | `PYTHON_IMAGE` (`Dockerfile`) and Postgres (`compose.yaml`) pinned by `@sha256` | **Pinned** (2026-10-27, not refreshed since); refresh procedure in README |
| A8 | Dependency audit | `pip-audit` on the hash lock; dev pins separately | **No known vulnerabilities** (2026-10-27, not rerun since); dev `pytest` 9.0.3 |
| A9 | OCI image and digest | `deploy/build-image.sh --save`; `release.yml` | Published by `release.yml` on every tag through `v0.4.10-pre` (run 41891908002, 2026-11-04; amd64 + arm64, SBOM + provenance). Earlier: `0.3.0-pre` arm64 from `24e3493` image ID `sha256:8934f6ef…`, published digest `sha256:fa723b88…` |
| A10 | Built-image runtime and Compose first run | README quick start against `WEBINAR_IMAGE` | **Passed on the `0.3.0-pre` arm64 image** (2026-10-27) and earlier on the `0.2.0-pre` arm64 and emulated amd64 images: migrate, `sync_roles`, `bootstrap_admin`, `create_api_key`, seed, web + worker healthy, `/healthz` version, OpenAPI, `/mcp`, studio login page, uid 10001. Published manifests not pulled from here (no `read:packages` token) |
| A11 | Backup/restore from the A10 database | `deploy/backup.sh`, `deploy/restore.sh` | **Passed** (2026-10-27, image from `3ec60ad`): restore into a fresh project, migrate no-op, secrets decrypt only with the original key, `--force` guard. Backup repeated on the release image |
| A12 | Retention dry run | `purge_personal_data --older-than-days N --dry-run` on A10 data | **Passed** (2026-10-27), counts only |
| A13 | Webhook receiver contract (offline) | `tests/test_verify_webhook_example.py` | **8 tests pass** (valid, tampered, stale, duplicate, wrong secret, rotation) |
| A14 | Edge config syntax and routing | `nginx -t` on `docker/nginx.conf.example` and `sdk/kits/node/nginx-webinar-api.conf`; routing and rate-limit checks | **Passed after fixes** (2026-10-27, nginx 1.30 / 1.27) |
| A15 | WordPress plugin lint and fresh install | `php -l`; `bash sdk/kits/test/fresh-install.sh` (WordPress 7.1 / PHP 8.2 in Docker, block + shortcode + cache + subdirectory) | **Passed** (2026-10-27) |
| A16 | Framework and CMS kits from fresh scaffolds | `bash sdk/kits/test/fresh-install.sh` (Next 16, SvelteKit 3, Astro 7, Nuxt 4, static snippet, Sanity/Payload/Prismic/Contentful validation) | **Passed** (2026-10-27) |
| A17 | End-to-end acceptance workflow | `tests/test_e2e_acceptance.py` (two scenarios: waiting-room attendance; Zoom attendance import + no-shows) | **Passed** (2026-10-27). Release tree |
| A18 | In-place upgrade and rollback | `deploy/upgrade.sh --image …` from the previous release image; rollback by `WEBINAR_IMAGE` | **Passed** `da04cc7` → `3ec60ad` (no schema change, 2026-10-27). Not rerun since; migrations 0012–0017 postdate it. Rollback after a migrating upgrade never exercised |
| A19 | Live service tests and clippy | `cd live && cargo test`; `cargo clippy` | **72 HTTP + 18 unit pass** on `v0.4.9-pre` (2026-11-04). Real-browser, cross-browser, attack and load runs: 2026-11-01 on the branch merged into 0.4.0-pre |

## B. Code fixes required

None open.

## C. Owner accounts, approvals and decisions

None is approved by this document. External calls need explicit approval and owner-held accounts.

| # | Item | Gap | Needs owner account |
|---|---|---|---|
| C1 | Native adapter sandboxes, one per adapter to be enabled (HubSpot, Attio, PostHog, Customer.io, Segment, GA4, Amplitude, Mixpanel, RudderStack, Brevo, Salesforce, Pipedrive, ActiveCampaign, Mailchimp, beehiiv, Resend, Amazon SES); otherwise keep disabled | R11 | Yes |
| C2 | Stripe test-mode run of the full paid flow, webhooks, refunds, disputes | R12 | Yes (Stripe) |
| C3 | Hosting sandboxes: Zoom Webinars licence (provisioning and participant report), Wistia, Sequel, Riverside (attach, registrants, attendance) | R13 | Yes |
| C4 | Real email delivery test to the operator's mailboxes: an SMTP relay, or PostHog transactional email (mandatory for paid tickets and invitations) | R14 | Yes |
| C5 | Real webhook receiver test over the internet; import the Zapier, Make and n8n recipes on real accounts | R15 | Yes |
| C6 | Kit compatibility on the operator's real sites and hosts (https hosts, CDN, Vercel, Webflow/Framer plans, paid sessions through a kit) | R16 | Yes (hosting / CMS / plan accounts) |
| C7 | Web calendar deep links against live Google/Outlook/Microsoft 365, or accept unverified | R17 | Yes (test calendars) |
| C8 | Accept publication authority, confirmation-token, `config_read` draft-read, paid-flow, attendance-import, marketing-status (incl. the beehiiv subscription exception) and transactional-email policies | R18–R22, R27–R29 | No |
| C9 | Accept residual behaviour (remaining-gaps §5) and the A7 digests | R23 | No |
| C10 | Scope decisions recorded 2026-10-27: beehiiv, Resend, Amazon SES, Zapier, Make and n8n built; Intercom, Loops and Marketo skipped; Postmark and HubSpot Marketing not committed (remaining-gaps §4) | §4 | No |
| C11 | Approve a staging host and run launch acceptance ([implementation-guide.md §11.1](implementation-guide.md#111-launch-acceptance-run-on-staging-record-results-in-validationmd)) | §3 | Yes (host) |
| C12 | Approve a production deployment (`deploy/production/` or `deploy/quickstart/`) and record its first run and each upgrade in [validation.md](validation.md) | §3 | Yes (host) |
| C13 | Decide package visibility for `ghcr.io/b2b-saas-inc/visioncast` or grant deploy hosts a `read:packages` token, then run a published manifest on a real amd64 host | R24 | Yes (GitHub) |

## D. Operator tasks (target environment)

For a DigitalOcean droplet behind Cloudflare, [deploy/production/README.md](../deploy/production/README.md)
covers D1, D2, D5, D6, D9, D10, D12 and D13 with scripts and
[deploy/production/cloudflare.md](../deploy/production/cloudflare.md) the Cloudflare part of D1–D3.

| # | Task | Gap |
|---|---|---|
| D1 | TLS with HTTP→HTTPS redirect | §3 |
| D2 | Edge rate limits on `/webinar-api/*` (incl. proxy `my-registration`), `/w/`, `/mcp`, `/admin/`, `/studio/login/`, `/api/v1/payments/stripe/webhook` (`docker/nginx.conf.example`) | §3 |
| D3 | MFA, SSO or VPN in front of `/admin/` and `/studio/` | §3 |
| D4 | Mode B edges forward `/webinar-api/calendar.ics` (and `my-registration`), or set `WEBINAR_DIRECT_ICS_REWRITE=false` | R21 |
| D5 | `RATE_LIMIT_TRUST_X_FORWARDED_FOR` only behind a trusted chain of matching depth | §3 |
| D6 | Worker process running | §3 |
| D7 | `WEBHOOK_HOST_ALLOWLIST` for generic webhook receivers, self-hosted RudderStack data planes and n8n hosts, `api.stripe.com`, hosting hosts (native vendor hosts are pinned in code and need no entry unless `NATIVE_VENDOR_HOSTS_IMPLICIT=0`); https `PUBLIC_BASE_URL`; `PUBLIC_EVENT_URL_TEMPLATE` | §3 |
| D8 | Stripe webhook endpoint with the five events from [tickets.md](tickets.md) | §3 |
| D9 | `INTEGRATION_MASTER_KEY` outside backups; scheduled `deploy/backup.sh --encrypt-to …`; restore drill with `deploy/restore.sh` on the target host | §3 |
| D10 | Cron for `purge_personal_data` | §3 |
| D11 | Existing deployments: `deploy/upgrade.sh --image <image>` (migrates through 0017, `sync_roles`); marketers to `/studio/` | §3 |
| D12 | `manage.py check` on every deploy | §5 |
| D13 | Pin `WEBINAR_IMAGE` to the exact published tag (or the locally built `<version>-<commit>` tag) in `.env`; never `latest` | §3 |
