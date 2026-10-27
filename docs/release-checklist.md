# Release checklist

Status as of 2026-10-26 (after the 14:36Z native-policy entry in
[validation.md](validation.md)). **Nothing on this page is evidence.** Results are
recorded only in validation.md; the gaps they close are in
[remaining-gaps.md](remaining-gaps.md) (R1–R23). "Last recorded" quotes validation.md.

- **A. Local checks** Pi or a release validator can run in disposable containers (no
  messages, no third-party services; some fetch packages or images).
- **B. Code fixes** still required before release.
- **C. Owner accounts, approvals and decisions.**
- **D. Operator tasks** on the target environment.

Release only when every A item has a recorded pass on the exact tree and image being
released, every B item is fixed and retested, every C item is approved or explicitly
accepted, and every D item is in place. Any code change (including B) re-opens A.

## A. Local executable checks

| # | Check | How | Last recorded |
|---|---|---|---|
| A1 | Python suite PG15/Py3.12, system check, migration drift, Ruff | `tests/run-pg.sh` (managed `python:3.12-slim-bookworm`) | **1,244 passed**, clean (2026-10-26T14:36Z). Current tree |
| A2 | Site tests, typecheck, production build | `site/` | **50 passed**, typecheck/build passed (2026-10-26T14:36Z). Current tree |
| A3 | SDK, proxy and kit tests | `npm test` in `sdk/`; `node --test sdk/kits/test/kits.test.js` | 60 passed (2026-10-25T04:49Z). Rerun on release tree |
| A4 | Actual-app Chromium studio journey | `tests/browser-studio.cjs` | 12/12 (2026-10-25T04:49Z). Rerun on release tree |
| A5 | Mocked-backend SDK Chromium | `tests/browser-sdk.cjs`; add scenarios for web calendar links, `payment_required`, `verification_required` | 8/8 in an early wave only |
| A6 | Python suite on PostgreSQL 16 + logical and application restore probe | `tests/run-pg16.sh` (includes `tests/restore_probe.py`) | 1,274 passed with restore/decrypt/wrong-key/admin probe (2026-10-26, Python3.12.15/PostgreSQL16.15). Production version series tested from source; built-image runtime still outstanding |
| A7 | Base-image digests | Pin `PYTHON_IMAGE` (`Dockerfile`) and Postgres (`compose.yaml`) by `@sha256` | Not done; both are tags |
| A8 | Dependency audit | Hash lock is adopted (`requirements.in` → hashed `requirements.txt`, hash-enforced install, 2026-10-24). Rerun `pip-audit` on the release lock | No known vulnerabilities on the 2026-10-24 lock. Rerun |
| A9 | OCI image and digest | Managed amd64 build of the default target after A7 | `artifacts/pi-build-d49e1bea-a7ab-4752-88c4-7b59259f0a08/image.tar` built from the current tree. **No SHA-256 recorded**; rebuild needed after A7 or B fixes |
| A10 | Built-image runtime and Compose smoke | Load the A9 image; `docker compose` with Postgres 16; `migrate`, `sync_roles`, `create_api_key`, web + worker healthchecks, `/healthz`, studio login, `/api/v1/openapi.json`, `/mcp`. Local sample data, no SMTP, no destinations | Never done for any image |
| A11 | Backup/restore from the A10 database | `pg_dump` → scratch DB → start the image against it; secrets decrypt only with separately supplied `INTEGRATION_MASTER_KEY` | Synthetic source-tree probe only (A6). Not done with the built image |
| A12 | Retention dry run | `purge_personal_data --older-than-days N --dry-run` on A10 data | None |
| A13 | Local webhook receiver contract | Receiver from `examples/verify_webhook.py` as a destination in A10: valid, tampered, stale, duplicate `deliveryId` | None |
| A14 | Edge config syntax | `nginx -t` on `docker/nginx.conf.example` and `sdk/kits/node/nginx-webinar-api.conf`; confirm `calendar.ics`/`my-registration` forwarding and B6-path rate limits | None |
| A15 | WordPress plugin lint and local install | `php -l` on `sdk/kits/wordpress`; optionally a disposable local WordPress container with sample data | None |

## B. Code fixes required (Pi can implement; then rerun A)

| # | Fix | Gap |
|---|---|---|
| B1 | CLOSED — disproven: outbox always preserves delivery metadata and serialization restores delivery ID; regression passes | R7 |
| B2 | DONE — studio shows bounded refusal code/guidance; secret-redaction and valid/refused preview regressions pass | R8 |
| B3 | DONE in source — Riverside REST/studio/MCP reconciliation, scoped confirmations, private-link redaction and attempt fencing; OpenAPI/docs and 21 new regressions included in 1,274 passing PG15 tests | R9 |
| B4 | DONE — no shared-link prerequisite; personal links require current account/event binding. Apply migration0010 and reconcile legacy unbound links before use | R10 |

## C. Owner accounts, approvals and decisions

None is approved by this document. External calls need explicit approval and owner-held accounts.

| # | Item | Gap | Needs owner account |
|---|---|---|---|
| C1 | Native adapter sandboxes, one per adapter to be enabled (HubSpot, Attio, PostHog, Customer.io, Segment, GA4, Amplitude, Mixpanel, RudderStack, Brevo); otherwise keep disabled | R11 | Yes |
| C2 | Stripe test-mode run of the full paid flow, webhooks, refunds, disputes | R12 | Yes (Stripe) |
| C3 | Hosting sandboxes: Zoom Webinars licence, Wistia, Sequel, Riverside | R13 | Yes |
| C4 | Real SMTP relay test to owner mailboxes (mandatory for paid tickets and invitations) | R14 | Yes |
| C5 | Real webhook receiver test | R15 | Yes |
| C6 | Kit compatibility on real WordPress and the frameworks/CMSs to be supported; Webflow/Framer plan feasibility | R16 | Yes (hosting / CMS / plan accounts) |
| C7 | Web calendar deep links against live Google/Outlook/Microsoft 365, or accept unverified | R17 | Yes (test calendars) |
| C8 | Accept publication authority, confirmation-token, `config_read` draft-read and paid-flow policies | R18–R22 | No |
| C9 | Accept residual behaviour (remaining-gaps §5) and A8 findings / A7 digests | R23 | No |
| C10 | Decide scope for the requested backlog (remaining-gaps §4): which further CRM/marketing adapters and attendance import ship in this release vs the next | §4 | No |
| C11 | Approve a staging host and run launch acceptance ([implementation-guide.md §11.1](implementation-guide.md#111-launch-acceptance-run-on-staging-record-results-in-validationmd)) | §3 | Yes (host) |
| C12 | Approve live demo backend upgrade, production deployment, registry push, any preview renewal | — | Yes |

## D. Operator tasks (target environment)

| # | Task | Gap |
|---|---|---|
| D1 | TLS with HTTP→HTTPS redirect | §3 |
| D2 | Edge rate limits on `/webinar-api/*` (incl. proxy `my-registration`), `/w/`, `/mcp`, `/admin/`, `/studio/login/` | §3 |
| D3 | MFA, SSO or VPN in front of `/admin/` and `/studio/` | §3 |
| D4 | Mode B edges forward `/webinar-api/calendar.ics` (and `my-registration`), or set `WEBINAR_DIRECT_ICS_REWRITE=false` | R21 |
| D5 | `RATE_LIMIT_TRUST_X_FORWARDED_FOR` only behind a trusted chain of matching depth | §3 |
| D6 | Worker process running | §3 |
| D7 | `WEBHOOK_HOST_ALLOWLIST` for enabled vendors, `api.stripe.com`, hosting hosts; https `PUBLIC_BASE_URL`; `PUBLIC_EVENT_URL_TEMPLATE` | §3 |
| D8 | Stripe webhook endpoint with the five events from [tickets.md](tickets.md) | §3 |
| D9 | `INTEGRATION_MASTER_KEY` outside backups; scheduled `pg_dump`; restore drill on the target host | §3 |
| D10 | Cron for `purge_personal_data` | §3 |
| D11 | Existing deployments: `migrate` (incl. 0008, 0009), `sync_roles`; marketers to `/studio/` | §3 |
| D12 | `manage.py check` on every deploy | §5 |
