# Webinar Platform

> **Pre-release — not production-certified.** Published for evaluation and development, not as a production release. See [validation evidence](docs/validation.md) and [remaining release gates](docs/remaining-gaps.md). Publishing this repository does not deploy the application.

A self-hosted, single-tenant webinar registration platform built with Django 5.2 LTS
and PostgreSQL. One container image runs both the web process and a durable
background worker; deployment is a single Docker Compose file. Licensed under
[Apache-2.0](LICENSE).

It is aimed at two audiences:

* **Developers** read events through a public JSON API, register people through a
  scoped registration API (directly, or from the browser via a first-party proxy),
  write events/occurrences/attendance through a scoped management API, and consume
  signed webhooks.
* **Marketing teams** manage events in the Django admin with Administrator / Editor /
  Analyst roles.

> **Status: preview — not production-ready.** Parts of the code have been exercised
> by automated tests (see [docs/validation.md](docs/validation.md)), but nothing has
> been run as an internet-facing deployment. The platform has **no built-in TLS or
> admin MFA**, and only coarse per-process rate limits on a few public routes. TLS,
> edge rate limiting and MFA must be provided before any internet exposure (see
> [Production prerequisites](#production-prerequisites)).
>
> **Start here:** [docs/implementation-guide.md](docs/implementation-guide.md) covers
> deployment, website, form, lobby and webhook integration from start to finish.

## What it is — and is not

The platform stores events, schedules and registrations and emits events to
systems you already run. It is **not**:

* an attendee email or SMS sender — reminders, confirmations and follow-ups are
  emitted as webhook events (`reminder.due`, `registration.created`, …) for your
  external email/marketing automation to act on. Optional SMTP
  (`EMAIL_DELIVERY=smtp`, [docs/email.md](docs/email.md)) is used only for
  waiting-room verification codes and teammate invitations; there is no SMS;
* a media or streaming provider — join and recording URLs point at your existing
  webinar/video service. The optional hosted waiting room (`/w/<code>/`,
  [docs/waiting-room.md](docs/waiting-room.md)) only controls when the join link is
  disclosed and records check-ins;
* multi-tenant, or a CRM. It does not verify phone numbers, and verifies email
  addresses only by waiting-room code (required before a paid seat; see below).

## Features

* **Events**: internal name, slug, public title, hero image, AIDA copy (attention,
  interest, decision, action), learning outcomes, hosts, topics,
  draft/public/unlisted visibility and revision history.
* **Scheduling**: recurring series of occurrences with IANA timezones, UTC instants
  and DST gap/overlap validation; cancellation, reschedule, recording publication,
  attendance/no-show/follow-up events; ICS files with stable UID and SEQUENCE, an
  optional public event page URL (`PUBLIC_EVENT_URL_TEMPLATE`) and Google/Outlook
  "add to calendar" links (never private join links);
  reminder events that are skipped when late, suppressed on cancellation and
  replaced on reschedule.
* **Registrations**: durably committed before acknowledgement; idempotency keys with
  conflict detection; per-occurrence dedupe that never discloses or updates an
  existing person's profile.
* **Progressive profiling**: opaque, hashed, revocable browser capability cookie;
  the profile endpoint reveals only *missing field keys*; "Not you?" reset.
* **Forms**: canonical camelCase fields, snake_case aliases, explicit per-form
  mappings, conflict rejection, conservative email and E.164 phone rules, namespaced
  custom fields — see [docs/field-mapping.md](docs/field-mapping.md).
* **Consent and attribution**: SMS/analytics/advertising consent stored as client
  claims (never server-verified), allowlisted and consent-gated attribution, separate
  first-touch and registration-touch records.
* **APIs**: public read API, registration API, browser proxy API and a management
  (write) API — see [docs/api-reference.md](docs/api-reference.md) and
  [docs/management-api.md](docs/management-api.md).
* **Integrations**: transactional outbox, signed timestamped webhooks with stable
  delivery IDs, lease-based workers with a total per-send deadline, retries and
  dead-letter requeue, SSRF-safe HTTPS allowlist with DNS/IP validation,
  per-destination field groups and consent restrictions, Fernet-encrypted credentials.
* **Admin**: Django admin with Administrator / Editor / Analyst roles, PII masking for
  Analysts, PII-free audit log, minimal reporting (attendance is never inferred).
* **Privacy**: `purge_personal_data` retention/erasure command.
* **Browser integration**: dependency-free GTM script and a stdlib-only Node.js
  first-party proxy in [`sdk/`](sdk/), documented in
  [docs/browser-integration.md](docs/browser-integration.md) and
  [docs/integration.md](docs/integration.md). Website kits (WordPress plugin,
  framework and proxy references, [docs/kits.md](docs/kits.md)) are **not
  production-tested**; WordPress and framework compatibility is unverified.
* **Capacity and paid tickets** (preview, Stripe mocked in tests only;
  [docs/tickets.md](docs/tickets.md)): hard seat limits with atomic holds kept until
  Stripe confirms payment or checkout expiry; remaining-seat count shown or hidden
  per session. A paid seat needs mailbox proof first: the API answers
  `202 verification_required` and the buyer verifies via the waiting-room code; a
  registered cookie alone is not enough. Needs SMTP (or preview mail) and an
  enabled waiting room. Refunds are never automated; a Stripe refund or dispute
  blocks admission.
* **Hosting providers** (preview, no sandbox verification;
  [docs/hosting.md](docs/hosting.md)): Zoom Webinars create/update/attach; Wistia
  and Sequel create only; Riverside attach with per-registrant links (unverified
  paths). No provider attendance import.
* **Native integrations** (disabled until vendor-verified;
  [docs/native-integrations.md](docs/native-integrations.md)): PostHog,
  Customer.io, HubSpot, Attio, Segment, GA4, Amplitude, Mixpanel, RudderStack and
  Brevo, implemented and tested against mocked transports only; none is live-verified
  or production-ready (Mixpanel, RudderStack and Brevo request shapes were compared
  with fetched vendor-doc facts, not with real accounts). Each destination may only
  send to its own configured host. Salesforce, Pipedrive, other marketing-automation
  and transactional tools are not built.

## Identity model (read before integrating)

There is **no email verification for free registrations**. A browser is recognised
only by the HttpOnly capability cookie that was issued to the browser that created the
person. Paid seats are the exception: the buyer must prove the mailbox with a
waiting-room code before any hold or Checkout ([docs/tickets.md](docs/tickets.md)).

* **No account recovery and no cross-device recognition.** A second browser or
  device is never recognised for an existing person.
* **An existing email from an unknown browser never updates that person.** The
  registration is linked to the existing person only for per-occurrence dedupe and is
  marked `identity_verified=false`. The person's profile and consent records are not
  changed; the submission is kept on the registration only.
* **"Not you?" reset does not recover an identity.** It revokes the browser's
  capability and shows the full form. Submitting an email that already exists
  afterwards is treated as unverified, as above.
* **A replayed request never re-issues a cookie.** If the response that carried a new
  person's cookie is lost, an idempotent retry returns the stored body with a
  random, unbound decoy cookie; that browser stays unrecognised.
* **Downstream obligations.** Webhook payloads mark unverified registrations with
  `identity.verified=false`. Receivers must not merge those profiles into verified
  CRM records and must not send SMS unless the payload is verified and grants SMS
  consent for the phone number being messaged. See [docs/webhooks.md](docs/webhooks.md).

Full rules: [docs/architecture.md](docs/architecture.md#identity-rules) and
[docs/security.md](docs/security.md).

## Roles and API key scopes

| Admin role | Can |
|---|---|
| Administrator | Everything in the webinars app (events, people, destinations, API keys, outbox, audit log, reports). **Can view, but not create or change, users and groups.** |
| Editor | Events, occurrences, series, hosts, topics, forms, attendance entries. No PII models. |
| Analyst | Read-only catalogue and registrations (PII masked), reports. |

Only **superusers** manage users, groups and role membership. `bootstrap_admin`
creates the first account as a superuser in the Administrator group; give other
staff a role group, not superuser status. After upgrading an existing deployment run
`sync_roles` so role permissions converge (older Administrator groups had user
management rights).

| API key scope | Allows |
|---|---|
| `register` | `POST /api/v1/registrations` from a server (no browser cookie). |
| `proxy` | Registration plus profile, reset and my-registration status, forwarding the browser cookie (first-party proxy). |
| `trusted` | **Privileged.** Registration from a trusted server; organisation IDs stored as verified. |
| `content_write` | Management API: events, hosts, topics, forms, occurrences, cancellation. |
| `attendance_write` | Management API: attendance only. |
| `publication` | **Privileged.** REST event visibility changes (with `content_write`) and recordings, both with 428 confirmation; MCP publication tools. |
| `config_read` | Read-only `GET` on management events, sessions and waiting rooms (drafts included; `waitingRoom.url` null; never a write). MCP redacted configuration. |
| `public_read`, `reporting` | MCP only (`/mcp`: public read tools and resources; report summary). No REST endpoint checks them, and public REST reads need no key. |
| `delivery_admin`, `keys_admin`, `team_admin` | **Privileged.** MCP only; see [docs/mcp.md](docs/mcp.md). |
| `payments_admin` | **Privileged.** Stripe account (set-only secrets), prices, orders, recording refunds made in Stripe (REST and MCP; [docs/tickets.md](docs/tickets.md)). No money moves through the API. |
| `hosting_admin`, `hosting_provision` | **Privileged.** Hosting accounts with set-only credentials; provider session create/update/attach, reconcile and manual links (REST and MCP; [docs/hosting.md](docs/hosting.md)). |

No scope implies another. Keys are created with `create_api_key` and stored hashed.
Privileged scopes are granted only by a superuser (studio) or `manage.py create_api_key`.

## Quick start (local build, Docker Compose)

Either pull the published image (see "Published image" below) or build it locally from
this repository (`VERSION` is `0.3.0-pre`). Requirements: Docker with Compose v2, git, and
Python 3 only for generating secrets. These commands were run end to end on
2026-10-27 against the built image ([docs/validation.md](docs/validation.md)).

```sh
cp .env.example .env && chmod 600 .env
# Generate and paste secrets into .env:
python3 -c "import secrets; print(secrets.token_urlsafe(50))"                             # DJANGO_SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(32))"                             # POSTGRES_PASSWORD
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"  # INTEGRATION_MASTER_KEY
# Edit hosts/origins (DJANGO_ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, PUBLIC_BASE_URL,
# WEBINAR_ALLOWED_ORIGINS, THANK_YOU_REDIRECT_HOSTS, WEBHOOK_HOST_ALLOWLIST).

deploy/build-image.sh                                   # webinar-platform:<version>, OCI labels, evidence file
echo "WEBINAR_IMAGE=webinar-platform:$(cat VERSION)" >> .env
docker compose run --rm migrate
docker compose run --rm web manage sync_roles
docker compose run --rm web manage bootstrap_admin --username admin --email admin@example.com
docker compose run --rm web manage create_api_key --name "site proxy" --scope proxy
docker compose up -d
docker compose ps                                       # web and worker "healthy"
```

* `deploy/build-image.sh` builds the `production` target from the committed tree and
  tags `webinar-platform:<version>` and `:<version>-<commit>`; `docker compose build`
  (image `webinar-platform:local`, the default `WEBINAR_IMAGE`) also works but carries
  no version labels.
* `bootstrap_admin` prints a generated password once (or reads
  `BOOTSTRAP_ADMIN_PASSWORD`; remove it from `.env` afterwards).
* The API key is printed once. Configure it in the first-party proxy
  (`sdk/proxy/`), never in GTM or page source.
* The web port is bound to `127.0.0.1:8000`. The admin is at `/admin/`, the studio at
  `/studio/`, the health endpoint at `/healthz` (`{"status": "ok", "version": "…"}`).
* There are no default passwords; the stack refuses to start without the required
  secrets.
* Upgrade later with `deploy/upgrade.sh --image webinar-platform:<new version>`
  (backup, migrate, restart, `sync_roles`, health); back up with `deploy/backup.sh`,
  restore with `deploy/restore.sh` — [docs/operations.md](docs/operations.md) §3–§5.

Container roles (`docker/entrypoint.sh`): `web` (gunicorn), `worker`
(`run_worker`), `migrate` (explicit, never automatic), `manage <args>` (pass-through).
Other management commands: `show_version [--verbose]`, `run_worker`,
`purge_personal_data --person <uuid> | --older-than-days N [--dry-run]`,
`set_destination_secret --destination <id> --generate`, `sync_roles`,
`seed_public_demo`. Day-2 operations: [docs/operations.md](docs/operations.md).

### Published image

`.github/workflows/release.yml` builds linux/amd64 and linux/arm64 from the pinned
bases on a `v<VERSION>` tag and pushes `ghcr.io/<owner>/<repo>:<version>` and
`:<version>-<sha>` with OCI labels, an SBOM and provenance. The current published image is
**`ghcr.io/b2b-saas-inc/visioncast:0.3.0-pre`** (tag `v0.3.0-pre`, digest
`sha256:fa723b88ee7be73b8a1c8184471b9c0c0ed8218aedeeb272547feeaaf148e96d`, 2026-10-27;
`0.2.0-pre` remains available; the package is private, so pulling needs a token with
`read:packages`). Deploy it instead
of building:

```sh
docker pull ghcr.io/<owner>/<repo>:<version>
# in .env:  WEBINAR_IMAGE=ghcr.io/<owner>/<repo>:<version>  and  WEBINAR_PULL_POLICY=never
deploy/upgrade.sh --image ghcr.io/<owner>/<repo>:<version>     # or, first run: docker compose run --rm migrate && docker compose up -d
```

## Production prerequisites

Before exposing the platform to the internet you must provide, at the edge:

1. **TLS termination** that always overwrites `X-Forwarded-Proto`, then set
   `DJANGO_TRUST_X_FORWARDED_PROTO=1`.
2. **Rate limiting** on registration, profile, reset, admin/studio login, `/w/` and
   `/mcp`. The application's limits are per-process only (`webinars/ratelimit.py`), and
   the example Node proxy has none.
3. **MFA (or SSO/VPN) for `/admin/` and `/studio/`** — passwords only.

[`docker/nginx.conf.example`](docker/nginx.conf.example) is a starting point for
TLS, trusted forwarding headers and per-client rate limits. Its syntax and routing
were checked in `nginx:stable` against an echo upstream (2026-10-27), not in front of
the real application on an internet-facing host; tune and test it for your traffic. Also pin base images by digest and use the
hash-locked requirements once generated (see [Supply chain](#supply-chain-notes)).

## Validation status

Test runs, image builds and their dates are recorded only in
[docs/validation.md](docs/validation.md) (logs under `artifacts/`, not committed). This
README claims no result: code changed after the last recorded run has not been tested
until validation.md says so. The platform is not production-certified.

The distribution entry of 2026-10-27 records, for the built image: Compose first run
(migrate, `sync_roles`, `bootstrap_admin`, `create_api_key`, healthchecks, `/healthz`,
OpenAPI, `/mcp`, studio login, seeded public list, `tests/image_smoke.py`), backup and
restore into a fresh project with master-key checks, a retention dry run, the offline
webhook-receiver contract, `nginx -t` plus routing checks on both edge examples, and an
in-place upgrade from the previous release with image rollback. All of it ran on one
developer machine (linux/arm64 image, PostgreSQL 16.15); no internet-facing host, no
amd64 build and no registry publication have been exercised.

The release-candidate entry of 2026-10-27 (`0.2.0-pre`, revision `09e104a`) records the
merged tree's 1,945-test run under the PostgreSQL 16 harness, the end-to-end acceptance
workflow, the browser suites, the fresh-install kit verification, and a repeat of the Compose
first run on the image built from that exact tree. Remaining gates are listed in
[docs/release-checklist.md](docs/release-checklist.md).

## Development and testing

### In containers (PostgreSQL, authoritative)

```sh
docker compose up -d db
docker compose --profile test run --rm test                                            # full pytest suite
docker compose --profile test run --rm test tests/test_registration_api.py -k idempotency
```

The `test` image is the runtime image plus `requirements-dev.txt`; `tests/` and
`pyproject.toml` are bind-mounted read-only and never baked into images. The test
database (`test_<POSTGRES_DB>`) is created by pytest-django. The Dockerfile uses
`pip` inside its images.

### Local tooling (uv)

Local development uses [uv](https://docs.astral.sh/uv/). The SQLite fallback gives
fast feedback only — it is **not evidence of PostgreSQL behaviour**, and the
concurrency tests are skipped:

```sh
uv venv --python 3.12 .venv
. .venv/bin/activate
uv pip install -r requirements-dev.txt
DJANGO_TEST_SQLITE=1 pytest
ruff check .                     # rules E4, E7, E9, F from pyproject.toml only
```

`pyproject.toml` carries a self-contained Ruff configuration, so user-level Ruff
settings are not inherited.

### Browser SDK

```sh
(cd sdk && npm test)             # node --test; no dependencies
node tests/browser-sdk.cjs       # needs the `playwright` package and Chromium
node tests/browser-gtm.cjs       # site-wide GTM tag fixture + SDK in Chromium, all routes intercepted
```

## Documentation

| Document | Contents |
|---|---|
| [docs/implementation-guide.md](docs/implementation-guide.md) | **Start-to-finish implementation playbook** (deployment, pages, forms, proxy, lobby, ICS, webhooks, ops, launch checks) |
| [docs/remaining-gaps.md](docs/remaining-gaps.md) | Implemented vs external release gates vs not implemented |
| [docs/validation.md](docs/validation.md) | Release evidence |
| [docs/studio.md](docs/studio.md), [docs/waiting-room.md](docs/waiting-room.md), [docs/email.md](docs/email.md) | Marketer studio, hosted waiting room, operational SMTP |
| [docs/mcp.md](docs/mcp.md), [docs/native-integrations.md](docs/native-integrations.md) | Agent (MCP) access; native vendor integrations status |
| [docs/tickets.md](docs/tickets.md), [docs/hosting.md](docs/hosting.md), [docs/kits.md](docs/kits.md) | Capacity and paid tickets; hosting providers; website kits (all preview) |
| [docs/architecture.md](docs/architecture.md) | Components, module contracts, identity rules, outbox design, roles |
| [docs/api-reference.md](docs/api-reference.md) | Public, registration and proxy API, auth, errors, idempotency, CSRF |
| [docs/management-api.md](docs/management-api.md) | Event/occurrence/attendance write API |
| `GET /api/v1/openapi.json` | Canonical OpenAPI 3.1 description, generated from `webinars/openapi.py` (`docs/openapi.yaml` is superseded) |
| [docs/registration.schema.json](docs/registration.schema.json) | JSON Schema for the registration body |
| [docs/field-mapping.md](docs/field-mapping.md), [docs/field-dictionary.md](docs/field-dictionary.md) | Versioned schema, field mapping and field dictionary |
| [docs/browser-integration.md](docs/browser-integration.md), [docs/integration.md](docs/integration.md) | GTM script, consent, first-party proxy, error shapes, calendar flow |
| [docs/webhooks.md](docs/webhooks.md) | Payloads, signatures, retries, receiver obligations |
| [docs/security.md](docs/security.md), [docs/threat-model.md](docs/threat-model.md) | Security behaviour, threats, mitigations, residual risks |
| [docs/operations.md](docs/operations.md) | Bootstrap, edge, key rotation, backup/restore, upgrade/rollback, worker, retention, logging |
| `deploy/build-image.sh`, `deploy/backup.sh`, `deploy/restore.sh`, `deploy/upgrade.sh` | Versioned image build; Compose backup, restore and in-place upgrade (rehearsed, see validation.md) |
| [.github/workflows/release.yml](.github/workflows/release.yml) | Prepared GHCR release workflow (`v*` tags); not yet run, publishing needs owner approval |
| [docker/nginx.conf.example](docker/nginx.conf.example) | Example edge config: TLS, forwarding, rate limits (`nginx -t` and routing checked; not production-tuned) |
| [examples/verify_webhook.py](examples/verify_webhook.py) | Replay-safe webhook signature verification |

## Supply chain notes

* `requirements.txt` is a complete, hash-locked transitive lock generated from
  `requirements.in` (`uv pip compile requirements.in --generate-hashes -o requirements.txt`);
  the image build installs it with `pip --require-hashes`, so a tampered wheel fails the
  build. `requirements-dev.txt` adds three exact tool pins (pytest, pytest-django, ruff)
  that never enter the production image. `pip-audit` on the lock: no known
  vulnerabilities (2026-10-27, pip-audit 2.10.1); rerun it on every release lock.
* Base images are pinned by **digest**: `Dockerfile` `ARG PYTHON_IMAGE` and the `db`
  service in `compose.yaml` (tag kept in the comment). The pins are multi-platform
  index digests, so they resolve for linux/amd64 and linux/arm64. To refresh:

  ```sh
  docker buildx imagetools inspect python:3.12-slim-bookworm   # "Digest:" line = index digest
  docker buildx imagetools inspect postgres:16-alpine
  ```

  Paste the new `sha256:` into the two files, update the date comments, rebuild
  (`deploy/build-image.sh`), rerun the Compose first run, and record both digests in
  `docs/validation.md`.
* The image carries OCI labels (`org.opencontainers.image.version/revision/created/source`)
  and reports its version at `/healthz`, `manage.py show_version` and the MCP
  `serverInfo`; `deploy/build-image.sh` writes the image ID and tarball SHA-256 to
  `artifacts/image-<version>.sha256`.
* The image runs as uid 10001 with a read-only root filesystem, `cap_drop: ALL` and
  `no-new-privileges`. The build context is an allow-list (`.dockerignore`).

## License

Apache License 2.0 — see [LICENSE](LICENSE).
