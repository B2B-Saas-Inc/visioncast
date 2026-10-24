# Release checklist

Status as of 2026-10-23 (after the 18:23Z review follow-up). **Nothing on this page is
evidence.** Results are recorded only in [validation.md](validation.md), and the
blockers they close are described in [remaining-gaps.md](remaining-gaps.md) (B1–B9).
The "Last recorded" column quotes validation.md or the named artifact log. "None" means
no run has been recorded.

The page has three parts:

- **A. Local executable checks.** Pi or a release validator can run these on a workstation in
  disposable, lifecycle-managed containers. They send no messages and use no third-party
  services. Some need a package index or registry to fetch dependencies, as noted.
- **B. Owner approvals and decisions.** Only the owner can do or accept these. Several
  involve external calls, real messages or deployment and must not run without explicit
  approval.
- **C. Operator tasks.** Edge and hosting setup on the target environment.

Release only when every A item has a recorded pass on the exact tree and image being
released, every B item is approved or explicitly accepted, and every C item is in place.

## A. Local executable checks

Re-run all of A1–A4 after **any** code change. This includes the in-progress
operational-settings change (`config/settings.py`, `.env.example`,
`tests/test_operational_settings.py`), which no recorded run covers yet.

| # | Check | How | Pass criteria | Last recorded |
|---|---|---|---|---|
| A1 | Python suite on PostgreSQL 15, plus Django system check, migration drift, collectstatic and Ruff | `tests/run-pg.sh` inside a managed `python:3.12-slim-bookworm` container (fetches apt and PyPI packages). Logs: `artifacts/rebuild-pg.log`, `rebuild-checks.log`, `rebuild-static.log`, `rebuild-lint.log` | 0 failed. `System check identified no issues`. `No changes detected`. Ruff `All checks passed!`. The only allowed warning is the known Django transitional-setting deprecation | 803 passed, 1 warning; checks clean; Ruff passed (validation.md 18:23Z; `artifacts/rebuild-pg.log`) |
| A2 | SDK and proxy unit tests | In `sdk/`, `npm test`, which runs `node --test test/proxy.test.js test/webinar-form.test.js` (Node ≥ 18.17; no dependencies). Log: `artifacts/sdk-tests.log` | `fail 0` | 46 tests, 46 pass, including the web-calendar projection and rendering tests (`artifacts/sdk-tests.log`) |
| A3 | Actual-app Chromium marketer journey | `tests/browser-studio.cjs` against a disposable app instance. Log: `artifacts/rebuild-browser.log` | `12/12 steps passed` | 12/12 (validation.md 18:23Z) |
| A4 | Mocked-backend SDK Chromium scenarios | `tests/browser-sdk.cjs`. Log: `artifacts/browser-sdk.log` | All scenarios pass. The ICS selector `a[data-webinar-calendar]` still matches only the ICS link | 8/8 in an earlier wave. **Not recorded after the SDK web-calendar change.** There is no browser scenario for the web links yet |
| A5 | Python suite on PostgreSQL 16 (the Compose target) | Same as A1, with a PG16 cluster. `run-pg.sh` hard-codes cluster `15`, so this needs a PG16 variant of the script | Same as A1 | None |
| A6 | Rebuild the production OCI image from the released tree and record its digest | Managed linux/amd64 build of the `Dockerfile` default target (fetches the base image and PyPI wheels) | Build succeeds. Record the archive path **and SHA-256** in validation.md | `artifacts/pi-build-df010e6b-a961-4760-9f52-ee0b8b069d7d/image.tar` was built at 18:23Z. **No SHA-256 recorded.** Predates any later change |
| A7 | Built-image runtime and Compose smoke test | Locally: `docker compose` with that image and `postgres:16-alpine`. Then `migrate`, `sync_roles`, `create_api_key`, the web and worker healthchecks, `/healthz`, `/studio/` login, `/api/v1/openapi.json`, `/mcp` with a key. Use only local, sample data. No SMTP and no webhook destinations | Every step succeeds; the containers are removed afterwards | None |
| A8 | Backup and restore rehearsal | `pg_dump` from the A7 database, restore into a scratch database, then start the app against it. Confirm the restored instance decrypts destination secrets only with `INTEGRATION_MASTER_KEY` supplied separately | The restored app starts, data matches and secrets decrypt | None |
| A9 | Retention dry run | `manage.py purge_personal_data --older-than-days N --dry-run` on the A7 data | Reports the expected counts and changes nothing | None |
| A10 | Webhook receiver contract, local | Run a local receiver built on `examples/verify_webhook.py` as a destination in A7. Note: the SSRF policy may refuse private addresses unless they are allowed for the test. Send a valid delivery, a tampered body, a stale timestamp and a duplicate `deliveryId` | Valid delivery accepted; the other three rejected or ignored | None |
| A11 | Edge config syntax | `nginx -t` against `docker/nginx.conf.example` in a disposable nginx container, adapted to the real hostnames | Syntax OK. The config forwards `/webinar-api/calendar.ics` and `/webinar-api/my-registration` (B8) and defines rate limits for the B6 paths | None; the example is untested |
| A12 | Dependency lock and vulnerability review | Verify the `requirements.txt` pins exist, then generate a hash-locked transitive set (`uv pip compile --generate-hashes`) and install it with `--require-hashes`. Run a vulnerability scan (for example `pip-audit`). Needs PyPI and advisory-database access | Every hash resolves, the build works with `--require-hashes`, and no unaccepted advisories remain. Findings are recorded | None. Direct pins only, and the header says they are unverified against PyPI |
| A13 | Base image digests | Resolve and pin `python:3.12-slim-bookworm@sha256:…` (`PYTHON_IMAGE` build arg) and `postgres:16-alpine@sha256:…` (`compose.yaml`). Needs registry access | Digests recorded and used by A6/A7 | None |

## B. Owner approvals and decisions

Each item is either approved, with the scope and date recorded, or explicitly accepted
as a risk. None is approved by this document.

| # | Decision or approval | Gap | Notes |
|---|---|---|---|
| B-1 | Keep native adapters (PostHog, Customer.io, HubSpot, Attio) **disabled**, or approve vendor sandbox tests with real vendor calls | B3 | Vendor writes are never recalled, and no erasure is propagated to vendors. Enable each adapter only after its sandbox check passes and is recorded |
| B-2 | Accept that staff publish without confirmation tokens, and the residual admin race and inline-add behaviour | B4 | `team_admin` is at least as powerful as `content_write` + `publication` |
| B-3 | Accept that confirmation tokens are not human approval | B5 | Add any human sign-off outside the platform |
| B-4 | Accept `config_read` draft reads over REST, or request a dedicated `content_read` scope | B7 | Existing and delegated `config_read` keys gained draft reads silently |
| B-5 | Approve verification of the Google/Outlook/Microsoft 365 deep links against the live services, or accept them as unverified | B9 | External calls; use test calendars only |
| B-6 | Approve a real SMTP relay test, if waiting-room email verification or invitations will be used | §3 | Sends real email; restrict it to owner-controlled mailboxes |
| B-7 | Approve tests against the real CRM, ESP or webhook receivers | §3 | Real external delivery |
| B-8 | Approve a staging host and run the launch acceptance checks in [implementation-guide.md §11.1](implementation-guide.md#111-launch-acceptance-run-on-staging-record-results-in-validationmd) | §3 | Deployment. Record the results in validation.md |
| B-9 | Accept the A12 dependency and vulnerability findings and the A13 digests | §3 | Needs A12 and A13 recorded first |
| B-10 | Accept the known residual behaviour in remaining-gaps.md §4 (recognized oracle, proxy POST passthrough, per-process limits) | §4 | — |
| B-11 | Approve production deployment, any registry push of the image, and any preview renewal | — | None is authorized. The earlier one-hour preview approvals have expired and do not carry over |

## C. Operator tasks (target environment)

| # | Task | Gap |
|---|---|---|
| C-1 | TLS termination, with an HTTP→HTTPS redirect and no loop | B6 |
| C-2 | Edge rate limits on `/webinar-api/*` (including the proxy's `my-registration` route), `/w/`, `/mcp`, `/admin/` and `/studio/login/` | B6 |
| C-3 | MFA, SSO or a VPN in front of `/admin/` and `/studio/` | B6 |
| C-4 | Mode B edges forward `/webinar-api/calendar.ics` (and `my-registration` if used), or set `WEBINAR_DIRECT_ICS_REWRITE=false` | B8 |
| C-5 | Set `RATE_LIMIT_TRUST_X_FORWARDED_FOR` only behind a trusted proxy chain whose depth matches the configured trusted-proxy count | B6 |
| C-6 | Store `INTEGRATION_MASTER_KEY` outside the backups; schedule `pg_dump` | §3 |
| C-7 | Schedule `purge_personal_data` with cron | §3 |
| C-8 | Existing deployments: `migrate`, then `sync_roles`; send marketers to `/studio/` | §3 |
| C-9 | Run `manage.py check` on every deploy (catches a bad `PUBLIC_EVENT_URL_TEMPLATE`, which Gunicorn start does not) | §4 |
