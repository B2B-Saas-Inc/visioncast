# Preview validation — 2026-10-23

Implemented with parallel Claude CLI `claude-opus-5-5` orchestrators and bounded subagents, independent security review and corrective work. No production deployment, external messages or registry publication.

## Application restore probe — 2026-10-24T20:50:02Z

- Extended the PostgreSQL16 logical restore rehearsal with synthetic application records (`tests/restore_probe.py`). Restored a user and disabled integration destination, decrypted its credential and signing secret with the separately supplied master key, verified wrong-key rejection, authenticated the restored user and served an authenticated admin HTTP200 response.
- **837 tests**, Ruff/system/migration checks passed. Resources cleaned. Evidence: `artifacts/pg16-restore.log`.
- Supersedes the earlier lack of application credential restore evidence for this synthetic case. Not a production-volume backup test, target-host recovery drill, or built-image runtime test. No real credentials or outbound messages used.

## PostgreSQL16 and logical restore — 2026-10-24T20:34:44Z

- Managed `postgres:16-bookworm` environment: **837 tests passed**, Ruff/system/migration checks passed. Uses Debian Python3.11; separate existing Python3.12/PG15 coverage does not prove the exact production combination.
- `tests/run-pg16.sh` migrates the app, exports a custom-format logical backup, restores to a scratch DB and verifies a synthetic durable row and migration schema (25 migration records). Logs: `artifacts/pg16-tests.log`, `pg16-checks.log`, `pg16-restore.log`.
- **Limited restore evidence:** not yet an application data/credential decrypt restore rehearsal or built-image runtime test.
- The image lacks initial CA certificates. For reproducibility supply an operator-trusted public CA bundle at `artifacts/test-ca-bundle.pem`; the script bootstraps TLS from it, then installs the distribution CA package. TLS verification is never disabled. Host packages unchanged; disposable resources removed.

## Runtime hash enforcement — 2026-10-24T20:07:11Z

- Adopted transitive `requirements.txt` hashes; direct inputs are `requirements.in`. Regenerate using `uv pip compile requirements.in --generate-hashes -o requirements.txt` in a managed container.
- Fresh hash-enforced binary installation succeeded (`artifacts/hash-install.log`). Image wheel acquisition and runtime installation explicitly require hashes. Dev-only packages are not hash-locked.
- **837 PostgreSQL15 tests**, Ruff/system/migration checks passed.
- Latest amd64 OCI `artifacts/pi-build-941276f7-7f55-4bb1-b1da-b98ff225f896/image.tar`, SHA256 `fa39217317edcc3d74614bd953d7da13b6d3f466adee81ee2f5ba0b1f7dc9e26`; cleanup verified. Supersedes earlier hash-lock/image status. Runtime smoke, PG16, restore, base-image digest pins and external gates remain open.

## Dependency remediation — 2026-10-24T19:59:56Z

- Upgraded Django 5.2.7→5.2.17, cryptography 46.0.2→50.0.0 and Pillow 11.3.0→13.0.0 following audit findings; target versions resolved on PyPI.
- **837 PostgreSQL15 tests passed**, Ruff/system/migration checks passed, SDK/proxy tests passed and actual-app Chromium **12/12** passed. One existing Django transitional-setting warning remains.
- `pip-audit`: **No known vulnerabilities found** (`artifacts/dependency-audit-upgraded.json`, `.log`). This is scanner evidence, not a guarantee of no vulnerabilities. Candidate transitive hashes regenerated at `artifacts/requirements-candidate.lock`; hash enforcement remains open.
- Latest amd64 OCI `artifacts/pi-build-ccb07881-b621-4649-81bd-a0fb5e618824/image.tar`, SHA256 `176d65d417cdb4a18741bb9a84e97abc35875b05b8c8893e9654e9fd32546717`; cleanup verified. Requirements comments updated afterwards; dependency values unchanged. No image push/load/deployment.
- PostgreSQL16, built-image runtime/Compose smoke, restore, vendor validation and other external release gates remain open. No GitHub commit/push authorized until production-ready.

## Review follow-up — 2026-10-23T18:23:15Z

- Two parallel CLI workers verified `claude-opus-5-5`, no permission denials. Closed same-submission admin inline readiness bypass; nested proxy status projection now allowlisted. Proxy status rate limiting applies when trusted forwarded client addresses are configured; otherwise edge limits remain required (per-process limits are not global).
- SDK thank-you UI now renders validated Google/Outlook/Office365 links with accessible labels, while preserving ICS. Live provider deep-link compatibility remains unverified.
- **803 PostgreSQL15 tests passed**, Ruff/system/migration checks passed, SDK/proxy tests passed, actual-app Chromium **12/12 passed**. Existing Django transitional-setting warning remains.
- Latest amd64 OCI: `artifacts/pi-build-df010e6b-a961-4760-9f52-ee0b8b069d7d/image.tar`. Managed cleanup verified. Supersedes prior archive; no runtime image smoke/deployment/external sends.
- Other release gates below remain open. Prior review findings are historical, superseded by this section where fixed.

## Continuation verification — 2026-10-23T17:55:40Z

- Supersedes earlier totals and image: **788 PostgreSQL15 tests passed**, system/migration checks and Ruff passed. One existing Django transitional-setting deprecation remains.
- SDK/proxy tests passed; actual-app Chromium **12/12** steps passed. Logs: `artifacts/sdk-tests.log`, `artifacts/rebuild-browser.log`, `artifacts/rebuild-pg.log`.
- Parallel CLI workers, glue and independent reviewer all verified `claude-opus-5-5`, no permission denials. Review `.runs/continuation/review-result.md` found no blockers; residuals: same-submission admin inline readiness edge, nested proxy response projection hardening, and edge rate limiting for proxy status route. Existing recognized-email oracle remains documented.
- Read-only `config_read` now includes draft REST reads. Direct calendar rewrite defaults on: existing first-party edges must forward `/webinar-api/calendar.ics` or disable `WEBINAR_DIRECT_ICS_REWRITE`.
- Built amd64 OCI `artifacts/pi-build-f0df17cc-e229-4693-95aa-737c19eaa445/image.tar`, SHA256 `37e980ec5376fc9489090e2cc823f28d9a4b088ad5fa32de0194fc8f445b51bd`. Managed build cleanup verified; no push/load/deployment. The implementation guide is now packaged for MCP.
- Native vendor sandbox, real SMTP/receivers, built-image/Compose runtime smoke, PG16, dependency release review and backup restore remain unverified. No preview renewal or external sends.

## UX follow-up verification — 2026-10-23T16:33Z

- Configurable normal/confirm CTA, safe raster cover upload, Host/Hosts wording, per-session embed snippet with served SDK, and waiting-room navigation implemented. Integration shortlist is proposal-only in `native-integrations.md`.
- PostgreSQL15: **503 passed**; system/migration checks and Ruff passed (same deprecation warning). SDK/proxy: **38 passed**. Actual-app Chromium: **12/12 passed**, including cover upload, CTA preview, snippet content and fetching its SDK.
- Review corrected PNG metadata retention and made the SDK's canonical app-static source available in both production and preview; `sdk/src/webinar-form.js` is a symlink, not a duplicated script.
- Latest amd64 OCI: `artifacts/pi-build-dd1f2f13-4929-43d1-a2f3-d359971c9f3d/image.tar`, SHA256 `f31b4bd9f1034afeaa2591867865aa5e551f3b5232f97540caf865fcdf030517`. Managed cleanup verified. Supersedes the image below; not loaded, pushed or runtime-smoke-tested.
- Existing persistent preview was not restarted; it still has the preceding code snapshot. New Pillow pin requires the same dependency release review noted below.

## Earlier verified rebuild

- PostgreSQL 15: **467 tests passed** (`artifacts/rebuild-pg.log`, reproducible with `tests/run-pg.sh` in a disposable Python container). Includes attendance concurrency, identity, permissions, invitation, MCP and OpenAPI regression checks.
- Django system checks, migration drift check and project Ruff rules passed. One Django 6.0 transitional-setting deprecation warning remains (`FORMS_URLFIELD_ASSUME_HTTPS`).
- Real Chromium with the actual app: **10/10 marketer journey steps passed** (`artifacts/rebuild-browser.log`): login, starter event, editing, session, visual form builder/reordering, publishing, preview, waiting-room configuration, email-first attendee flow and manual admission control. Local test email only; no provider delivery. This is bounded workflow coverage, not exhaustive usability certification.
- Screenshots: `artifacts/studio-dashboard.png`, `artifacts/studio-editor.png`, `artifacts/waiting-room-mobile.png`, `artifacts/waiting-room-countdown.png`.
- Earlier unchanged SDK/proxy: **37 unit tests**, **8 mocked-backend Chromium scenarios** passed. These are separate from the actual-app journey above.
- Rebuilt Linux amd64 production OCI archive; managed cleanup verified removal of owned builder, container, cache volume and newly pulled builder image. No image loaded or pushed.

## Image artifact

`artifacts/pi-build-58ec6dda-6320-49a8-813c-dee77b027a84/image.tar`

SHA-256: `84cbe93e0664b5104428274f00efb301d23f207b39b1e7a543865c590756a160`

Supersedes the earlier `pi-build-98e4075a-f43c-45b8-ba37-5514e2c791c3` archive. This is a local OCI artifact, not a published registry tag. Deployment instructions build from source.

## Remaining release gates

- Compose startup and built-image runtime smoke testing remain unexercised. Tests used PostgreSQL 15; Compose targets PostgreSQL 16.
- Dependencies have direct pins, not a complete transitive hash lock. Vulnerability review and digest-pinned base images remain release gates.
- TLS, distributed edge rate limits and administrator MFA/SSO/VPN remain operator responsibilities. The nginx example is untested.
- No real CRM/email receiver, SMTP provider delivery, backup/restore rehearsal or production deployment tested.
- Waiting-room email verification and teammate invitations require configured SMTP. Preview uses only a local test mailbox; ordinary registration cookies are device recognition, not proof of mailbox ownership. See `waiting-room.md` for explicit identity and admission limits.
- Check-in measures admission, not video viewing. A redirect reveals the viewing URL to the attendee; provider authentication is needed to prevent sharing.
- Existing deployments must migrate and run `sync_roles`; the marketer entry point is `/studio/`, not `/admin/`.
- No new persistent preview was started as part of this verification. Prior one-hour preview authorization is not renewed.

Treat this as a tested preview, not production certification or a measured minutes-to-deploy guarantee.
