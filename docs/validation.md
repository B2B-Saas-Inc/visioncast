# Preview validation

Implemented with parallel Claude CLI `claude-opus-5-5` orchestrators and bounded subagents, independent security review and corrective work. Public demonstration deployed; not production certification. No live payment/provider sends or registry publication.

## Distribution: versioned image, first run, backup/restore, upgrade — 2026-10-27T17:35Z

Everything below ran on one developer machine (macOS, Apple Silicon: Docker 29.8.2, Compose v5.5.0, buildx v0.37.2, uv 0.12.13) in disposable `wdist-*` Compose projects that were removed afterwards. The image was built **natively for linux/arm64**; no linux/amd64 image was built or run (the prepared release workflow builds both, but has not been executed). Nothing was pushed to a registry or deployed to any host.

- **Image** (`deploy/build-image.sh`, target `production`, built from commit `3ec60adf8dd290ada5bd876200805ae7c0f126bf`, the commit before this evidence entry): version `0.2.0-pre` (`VERSION`), image ID / local repo digest `sha256:32aa5c7c5d569a76741197a6c622f32aa8e5037b2c8e6b8e2b0abd09d5213af8`, created `2026-10-27T17:22:42Z`, platform linux/arm64, OCI labels verified (`version`, `revision`, `created`, `source`). `docker save` tarball SHA-256 `b4f7363a53131ae535dbe31e850966dbfe45115dd5b7fdc5507b41378e2cac02` (`artifacts/wdist-webinar-platform-0.2.0-pre.tar`, 100,323,840 bytes; not committed). The test build used the repository name `wdist-webinar-platform`; a production build with the default name produces `webinar-platform:0.2.0-pre` and `webinar-platform:0.2.0-pre-3ec60adf8dd2` from the same inputs. The image reports the version at `/healthz`, `manage.py show_version --verbose` (version, revision, build time) and the MCP `serverInfo`; it runs as uid 10001.
- **A7 base-image digests** (resolved 2026-10-27 with `docker buildx imagetools inspect`): `python:3.12-slim-bookworm` index `sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753` (linux/amd64 manifest `sha256:72c1d56207fb14ca388f9b3855ac0e55ab557703928180ec40aa9d4c4f5d5f80`, image created 2026-10-26T22:46:30Z) pinned in `Dockerfile`; `postgres:16-alpine` index `sha256:0cefaabd882e42bcb2a2f3dc56d3ce4770560ba62fd7f197f1a2e008fd5731ab` (linux/amd64 manifest `sha256:cbf42f0edc946c0bc1b8eeed62403734e1f25dc67a3491455d07a92fdb558b28`, PostgreSQL 16.15) pinned in `compose.yaml`. Index digests are used so one pin serves amd64 and arm64.
- **A8 dependency audit**: `pip-audit 2.10.1` (throwaway `uv venv`) on `requirements.txt` with `--require-hashes`: **no known vulnerabilities**. The three dev-only pins were audited separately (pip-audit cannot mix hashed and unhashed requirements): `pytest 8.4.2` flagged PYSEC-2026-1845 (fix 9.0.3; dev tool, never in the production image) → `requirements-dev.txt` now pins `pytest==9.0.3`; `pytest-django 4.11.1` and `ruff 0.17.2` clean. `requirements.txt` was **not** regenerated (runtime lock unchanged). The lock installs with hashes (`uv pip install --require-hashes -r requirements.txt` into a scratch venv) and the image build verifies every hash.
- **Python suite after the code changes** (version module, `/healthz` version, MCP `serverInfo`, OpenAPI schema, `show_version` command, new receiver test): **1,403 passed** on Python 3.12.14 / PostgreSQL 16.15 (local `webinar-pg16`, database `ws_dist`) with pytest 9.0.3; `manage.py check`, `makemigrations --check --dry-run` ("No changes detected") and `ruff check .` clean. One pre-existing Django warning remains.
- **A9/A10 clean-server first run** (project `wdist-a10`, web published on 127.0.0.1:18000, generated `.env` with random secrets, `DJANGO_ALLOWED_HOSTS=localhost,web` — `web` added only so `tests/image_smoke.py` can address the service by name — `WEBINAR_ALLOWED_ORIGINS=http://localhost:18000`, `PUBLIC_BASE_URL=https://localhost`, `DJANGO_TRUST_X_FORWARDED_PROTO=1`, `WEBINAR_IMAGE=<built image>`, `WEBINAR_PULL_POLICY=never`): `docker compose config` valid; `run --rm migrate` applied 28 migrations (webinars 0001–0010); `up -d`; `manage sync_roles` (Administrator 92 / Editor 22 / Analyst 10 permissions); `manage bootstrap_admin` created the superuser and printed the password once (a second call was refused); `manage create_api_key --scope config_read` printed a `wbk_` key once; `manage seed_public_demo` created 6 events / 24 objects; web and worker **healthy**; running web container: user 10001:10001, read-only rootfs, `cap_drop ALL`, `no-new-privileges`. `GET /healthz` → `{"status": "ok", "version": "0.2.0-pre"}`; plain HTTP without `X-Forwarded-Proto` → 301 to https; `GET /api/v1/openapi.json` → OpenAPI 3.1.0, 56 paths, `/healthz` schema lists `status` and `version`; `POST /mcp initialize` with the key → 200, protocol `2025-06-18`, `serverInfo.version 0.2.0-pre`; `tools/list` with the key → 200, without a key → 401; `GET /studio/login/` → 200 with a CSRF form; studio login with the bootstrapped password → 302 to `/studio/`, authenticated `GET /studio/` → 200; `GET /api/v1/events?when=upcoming` → 4 upcoming `demo-*` events. `tests/image_smoke.py` run inside the image on the internal `backend` network: **all 13 checks true** (uid, production settings, no outgoing mail/webhooks, egress blocked, PostgreSQL 16, migrations applied, healthz, TLS redirect, synthetic seed, public list, packaged OpenAPI and MCP docs, registration API). `show_version --verbose` in the running web container: version 0.2.0-pre, revision 3ec60ad…, built 2026-10-27T17:22:42Z, Django 5.2.17. Worker log: PII-free start line only.
- **A11 backup/restore**: `deploy/backup.sh` (project `wdist-a10`, after creating a disabled destination with an encrypted credential and signing secret) → custom-format dump, 179,192 bytes, mode 0600, `PGDMP` magic and `pg_restore --list` verified; `--encrypt-to` not exercised (age not installed on the host). `deploy/restore.sh` into project `wdist-a11` (same image and `.env`, fresh volume, web on 127.0.0.1:18200): target reported empty, `pg_restore --exit-on-error` restored 28 migration rows, `migrate` → "No migrations to apply", web and worker healthy. On the restored stack: 5 upcoming events (4 demo + the smoke event), studio login with the A10 password → 302/200, 10 applied `webinars` migrations, destination credential and signing secret **decrypt with the original `INTEGRATION_MASTER_KEY`** and a freshly generated key is **rejected with `DecryptionError`**. A second `restore.sh` without `--force` was **refused** ("target database is not empty (45 tables)", exit 1); `--force --no-start` dropped, recreated and restored again; web/worker healthy afterwards.
- **A12 retention dry run** (`wdist-a10`): `purge_personal_data --older-than-days 1 --dry-run` → `[dry-run] persons=0 housekeeping={'ticket_payloads_purged': 0, 'idempotency_deleted': 0, 'outbox_payloads_purged': 0, 'outbox_stale_pending_cancelled': 0, 'outbox_orphaned_purged': 0, 'verifications_deleted': 0, 'preview_messages_deleted': 0}` — counts only, no personal data in the output.
- **A13 webhook receiver contract (offline)**: `tests/test_verify_webhook_example.py` loads `examples/verify_webhook.py` and feeds it bodies and headers built exactly as `webinars.delivery.deliver` builds them, signed with `webinars.crypto.sign_webhook`: valid → `(200, "processed")`; tampered body → 401 (and the genuine delivery still processes afterwards); stale timestamp (±301 s) → 401, boundary (300 s) accepted; replayed `Webinar-Delivery-Id` with a re-signed retry → `(200, "duplicate delivery ignored")`; wrong secret → 401; rotation header with two signatures accepted; the example's `--self-test` passes. 8 tests, part of the 1,403. A receiver reachable over the internet remains the owner's check (C5).
- **A14 edge config syntax**: `nginx -t` in `nginx:stable` (1.30.6) passes with `docker/nginx.conf.example` and `sdk/kits/node/nginx-webinar-api.conf` mounted verbatim inside an `http {}` wrapper (self-signed certificate at the placeholder paths). Routing in front of an echo upstream (only the upstream addresses substituted): platform host forwards `/healthz`, `/webinar-api/{profile,registrations,reset,my-registration,calendar.ics}` (path and query verbatim, `X-Forwarded-Proto: https`, `X-Forwarded-For` overwritten), `/w/…`, `/mcp`, `/api/v1/payments/stripe/webhook`, `/api/v1/events`, `/studio/login/`; `/webinar-api/other` → 404; HTTP → 301 https. Limits: 14 rapid POSTs to `/webinar-api/registrations` → 11×200 then 429; `/studio/login/` → 6×200 then 429 (`/admin/login/` shares the zone); 24 rapid GETs on `/webinar-api/profile` → 429 after the burst. **Two fixes were needed**: the example lacked locations for `my-registration`, `calendar.ics`, `/w/`, `/studio/login/`, `/mcp` and the Stripe webhook (added with limits), and the kit snippet's `location ^~ /webinar-api/ { return 404; }` shadowed its regex location so **every kit route answered 404** before the fix (`^~` removed; all five routes now forward verbatim, unknown paths 404). Not exercised: TLS termination with a real certificate or the real application behind nginx.
- **Upgrade path** (project `wdist-up`, web on 127.0.0.1:18100): previous release `da04cc7` built from `git archive` as `wdist-webinar-platform:old` (its unpinned Dockerfile), migrated (28 migrations), seeded, `bootstrap_admin`, a disabled destination with an encrypted credential created, healthy, `/healthz` → `{"status": "ok"}` (no version field). Switched the same project and volume to the current tree with `deploy/upgrade.sh --image <built image>`: pre-upgrade `deploy/backup.sh` dump written, `migrate` → "No migrations to apply" (**no schema change between da04cc7 and this tree**), web and worker recreated on `0.2.0-pre` and healthy, `sync_roles`, `show_version --verbose` printed the new version/revision, `.env` updated with `WEBINAR_IMAGE=…` and `WEBINAR_PULL_POLICY=never`. After the upgrade: `/healthz` shows `0.2.0-pre`, 10 applied `webinars` migrations, `migrate --check` exit 0, 4 upcoming events, the destination credential decrypts, studio login with the pre-upgrade password → 302/200. **Rollback (no-schema-change case)** by switching `WEBINAR_IMAGE` back to the old image and `up -d web worker`: healthy, data intact; rolled forward again. The schema-change rollback case was **not rehearsed** (no reversible-migration pair exists between the two trees); it is documented in operations.md §5 as reverse-migrate-with-the-new-image or restore-from-backup. A first attempt revealed that `docker compose run` calls made without `WEBINAR_IMAGE` fall back to `webinar-platform:local` and rebuild it through `migrate`'s `pull_policy: build`; `upgrade.sh` therefore now records the image in `.env`, and the re-run confirmed no fallback image was built.
- **Publishing**: `.github/workflows/release.yml` (GHCR, `v*` tags, amd64+arm64, SBOM and provenance) is prepared but **has not been run**; no tag was pushed and **no image has been published**. `compose.yaml` gains `WEBINAR_IMAGE` (default `webinar-platform:local`) and `WEBINAR_PULL_POLICY` so a pulled image can replace the local build; only the local-build path was exercised.
- **Cleanup**: all `wdist-*` containers, networks, volumes and images, the `nginx:stable` image and the base image pulled for the build were removed and their absence verified; the pre-existing `webinar-pg16` container, `postgres:16-alpine`, `python:3.12-alpine` and `nginx:1.27-alpine` images were left untouched. Scratch files (generated `.env`s, dumps, logs) live outside the repository.

## Actual built-image smoke — 2026-10-26T22:30:20Z

- **Passed** managed runtime smoke of `artifacts/pi-build-fec1905b-e8cb-4147-b39d-7ed3f21564bd/image.tar` with disposable PostgreSQL16. Ran migrations, web, probe and worker once; no application-source mount.
- Verified UID10001, production settings, TLS redirect, health, public event list, registration API, packaged OpenAPI/MCP guides, applied migrations and blocked external egress. Synthetic data only; no outgoing email/webhooks.
- Evidence: `.runs/image-smoke/pi-wimg-d8d1d699-5c11-46e2-b600-e2308b9b0e78/result.json`. All owned containers, network, volume, newly imported application image and newly pulled PostgreSQL image removed; absence verified.
- Prior manifest/config identity mismatch was fixed and its leftover image removed with explicit owner approval. This closes the basic actual-image-runtime blocker, **not** Compose deployment, image-based restore, live vendor validation or production readiness.

## Riverside real-browser verification — 2026-10-26T19:23:06Z

- Managed Chromium: existing studio **12/12**, new Riverside **8/8** scenarios pass. Covers masked registrant listing, forged-token rejection, confirmed personal-link reconciliation, Editor denial, recognised attendee automatic admission without a shared URL, missing-link denial, and unknown-browser refusal.
- Missing-link denial uses JavaScript disabled to exercise the server response deterministically; automatic retry behaviour for that case remains unverified. Synthetic fixtures and credentials exist only in the disposable test container.
- Harness correction: initial routing did not intercept HTTP redirect hops and contacted public Riverside pages using a synthetic link. No vendor API or real credentials were used. Final harness fetches admission responses with redirects disabled and aborts external Locations before navigation.
- Re-ran **1,320 tests on Python3.12.15/PostgreSQL16.15**; Ruff/system/migration and logical/application restore checks pass. Existing Django warning remains. No production code/image change or deployment; actual-image runtime remains unverified.

## Riverside admission and event binding — 2026-10-26T19:04:38Z

- Enabled attached Riverside rooms can open without a shared join URL. Admission still requires an eligible registration and valid personal link; disabled accounts, missing/malformed links and payment/status gates deny access.
- Migration0010 binds each provider registrant to account plus external event ID. Reattached events cannot reuse stale links; late provider outcomes are fenced. Legacy blank bindings fail closed and require operator reconciliation, not automatic backfill.
- **1,320 tests pass on Python3.12.15/PostgreSQL16.15**, Ruff/system/migration and logical/application restore checks pass. Existing Django warning remains. No live-provider or new browser verification.
- Managed amd64 OCI: `artifacts/pi-build-fec1905b-e8cb-4147-b39d-7ed3f21564bd/image.tar`. Cleanup verified. No deploy, push, image runtime launch or vendor sends.

## Production version-series source verification — 2026-10-26T18:41:07Z

- `tests/run-pg16.sh` now installs Python3.12 through uv within the disposable container and asserts the interpreter series before testing.
- **1,274 tests pass on Python3.12.15/PostgreSQL16.15**; Ruff/system/migration checks pass. Logical/application restore, credential decryption, wrong-key rejection and authenticated admin request pass. Existing Django deprecation remains.
- Existing real-app Chromium journey rerun: **12/12 passed**. This does not exercise the new Riverside reconciliation UI specifically.
- Managed test cleanup succeeded. No application source change, deployment or vendor sends. Source verification is not built-image/Compose runtime verification; that still needs a managed archive-load/run lifecycle.

## Riverside reconciliation controls — 2026-10-26T18:15:56Z

- Added REST, studio and MCP stalled-registrant reconciliation: scoped authorization, state-bound confirmations, no private links in responses/previews, and attempt fencing under the existing service lock. OpenAPI and hosting guide updated.
- **1,274 PostgreSQL15/Python3.12 tests passed**, Ruff/system/migration clean. New controls have not yet had a real browser or live Riverside sandbox run. Previous PostgreSQL16 evidence predates these controls.
- Managed amd64 OCI: `artifacts/pi-build-9241e8f3-5ad3-4e42-8f40-125563e55247/image.tar`; SHA256 `63f96ba909df09efb0d7ec2f08f03b3863ef885d1b267ea83fc387b97e3a12da`. Owned build resources removed and verified. No image load/push or backend deployment.

## Preview refusal and current PostgreSQL16 verification — 2026-10-26T17:53:44Z

- Studio now reports bounded native-preview refusal codes/guidance without exposing secrets or vendor exception text. The alleged missing delivery-metadata validation defect was disproven: metadata is always retained and delivery serialization restores its ID. Regression added.
- **1,253 tests pass on PostgreSQL15/Python3.12 and PostgreSQL16/Python3.11**, Ruff/system/migration checks clean; existing Django transitional-setting warning remains.
- PostgreSQL16 logical/application restore passes: schema, synthetic records, credential decryption, wrong-key rejection and authenticated admin request. Not a production-volume or built-image restore drill.
- Managed amd64 OCI: `artifacts/pi-build-140dadcb-350e-4f69-b2f4-7fb766a507c7/image.tar`; build resources removed and verified. No registry push, deployment or vendor sends. Exact production runtime pair and image smoke test remain open.

## Native policy changes and catalogue — 2026-10-26T14:36:34Z

- Independent review found that a native multi-step send could continue with stale configuration. Delivery now stops subsequent steps if credentials, field mapping, groups, private-link policy or other plan inputs change. Consent cancellation and disabled-destination parking are preserved.
- **1,244 PostgreSQL15/Python3.12 tests passed**, Ruff/system/migration clean. Frontend **50 tests**, typecheck and production build passed.
- Updated public integration catalogue deployed at https://visioncast-ten.vercel.app/integrations. HTTP200 and presence of Brevo/Mixpanel/RudderStack confirmed. No backend deployment or live vendor sends.
- OCI archive: `artifacts/pi-build-d49e1bea-a7ab-4752-88c4-7b59259f0a08/image.tar`; owned build resources removed and verified. Native sandbox checks remain outstanding; Brevo contact marketability warning documented.

## Brevo adapter — 2026-10-26T04:42:45Z

- Added Brevo contact upsert and lifecycle events using official API references. Requires explicit processing/email-match acknowledgements; does not subscribe to lists, clear blocklists, force-merge contacts or send SMS.
- Credentialed native transport now requires explicit host restrictions for case-insensitive `api-key` headers as well as `Authorization`.
- **1,235 PostgreSQL15/Python3.12 tests passed**, Ruff/system/migration checks clean; existing Django deprecation remains. No live Brevo calls; event retries may duplicate vendor automation triggers.
- Managed amd64 OCI built: `artifacts/pi-build-598a3628-0af3-425d-84c7-0b4576f2fd22/image.tar`; cleanup verified. Not deployed, loaded or pushed.

## Analytics contract hardening — 2026-10-25T17:28:50Z

- Checked official Mixpanel Import Events and RudderStack HTTP API documentation; evidence and remaining ambiguities are in [native-integrations.md](native-integrations.md).
- Added exact serialized UTF-8 request-size limits, conservative scalar/property validation, timestamp validation and numeric Mixpanel `ip=0` to prevent server-IP geolocation. No live accounts used.
- **1,158 tests passed** on PostgreSQL15/Python3.12; Ruff/system/migration checks clean. Existing Django deprecation remains.
- Managed amd64 OCI build succeeded: `artifacts/pi-build-855b97e5-b233-47ea-9b83-b60fb2b949b9/image.tar`. Cleanup verified; no publication or deployment. Vendor sandbox tests remain outstanding.

## Payment webhook fix and analytics adapters — 2026-10-25T17:10:23Z

- Independent static review found that disabling Stripe blocked signed completion, refund and dispute events for existing orders. Webhooks now authenticate against the configured account regardless of enabled state; new sales remain disabled. Eight new regression cases cover this distinction and signature rejection.
- Mixpanel and RudderStack adapters implemented, with per-destination credentialed-host restrictions and mocked tests. Their vendor contracts and live accounts remain unverified; do not enable them on the strength of mock tests alone.
- Final PostgreSQL15/Python3.12 suite: **1,142 passed**; Ruff, system checks and migration drift clean. One existing Django deprecation warning remains. Supersedes earlier unfinished review/adapter status below.
- Managed amd64 OCI build: `artifacts/pi-build-5139c50f-1700-4f45-b3c9-35a069fa93ca/image.tar`; owned build resources removed and verified. Not loaded, published or deployed. The live demo backend and catalogue have not been updated for these two adapters.

## Tickets, hosting and MCP expansion — 2026-10-25T04:49:16Z

- Final PostgreSQL15/Python3.12 suite: **1,049 passed**, Ruff/system/migration checks clean. One existing Django transitional-setting deprecation remains.
- SDK/reference-kit tests: **60 passed**. Next.js site: **48 passed**, typecheck and production build passed.
- Real-app Chromium studio journey: **12/12 passed**. Live `/features` and `/integrations` passed HTTP, heading and horizontal-overflow checks at 390px and 1440px. These are not paid checkout browser or vendor sandbox checks.
- Demo frontend deployed at https://visioncast-ten.vercel.app; immutable deployment https://visioncast-nkorzlkfr-b2b-saas.vercel.app. Backend on the live demo has NOT been upgraded by this expansion; explanatory feature pages do not imply enabled payments or integrations.
- Managed amd64 OCI build succeeded: `artifacts/pi-build-38e81328-db73-4550-bde0-e8487281ad2d/image.tar`. Builder/container/cache cleanup verified; archive not loaded, published or runtime-smoke-tested.
- Evidence: `artifacts/rebuild-pg.log`, `expansion-sdk-kits.log`, `expansion-site-tests.log`, `expansion-site-types.log`, `expansion-site-build.log`, `rebuild-browser.log`, `live-features-*.png`, `live-integrations-*.png`.
- Segment, Amplitude and GA4 adapters are mock-tested only. Mixpanel/RudderStack follow-up and a fresh independent security review timed out with no result or observed implementation. Neither is credited as complete. Remaining provider/account testing and adapter backlog are still open.


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
