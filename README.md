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
* multi-tenant, or a CRM. It does not verify email addresses or phone numbers.

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
  [docs/integration.md](docs/integration.md).

## Identity model (read before integrating)

There is **no email verification**. A browser is recognised only by the HttpOnly
capability cookie that was issued to the browser that created the person.

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

No scope implies another. Keys are created with `create_api_key` and stored hashed.
Privileged scopes are granted only by a superuser (studio) or `manage.py create_api_key`.

## Quick start (local build, Docker Compose)

There is **no published registry image**: you build the image locally from this
repository. Requirements: Docker with Compose v2, and Python 3 only for generating
secrets.

```sh
cp .env.example .env && chmod 600 .env
# Generate and paste secrets into .env:
python3 -c "import secrets; print(secrets.token_urlsafe(50))"                             # DJANGO_SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(32))"                             # POSTGRES_PASSWORD
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"  # INTEGRATION_MASTER_KEY
# Edit hosts/origins (DJANGO_ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, PUBLIC_BASE_URL,
# WEBINAR_ALLOWED_ORIGINS, THANK_YOU_REDIRECT_HOSTS, WEBHOOK_HOST_ALLOWLIST).

docker compose build
docker compose run --rm migrate
docker compose run --rm web manage bootstrap_admin --username admin --email admin@example.com
docker compose run --rm web manage create_api_key --name "site proxy" --scope proxy
docker compose up -d
```

* `bootstrap_admin` prints a generated password once (or reads
  `BOOTSTRAP_ADMIN_PASSWORD`; remove it from `.env` afterwards).
* The API key is printed once. Configure it in the first-party proxy
  (`sdk/proxy/`), never in GTM or page source.
* The web port is bound to `127.0.0.1:8000`. The admin is at `/admin/`, the health
  endpoint at `/healthz`.
* There are no default passwords; the stack refuses to start without the required
  secrets.

Container roles (`docker/entrypoint.sh`): `web` (gunicorn), `worker`
(`run_worker`), `migrate` (explicit, never automatic), `manage <args>` (pass-through).
Other management commands: `run_worker`, `purge_personal_data --person <uuid> |
--older-than-days N [--dry-run]`, `set_destination_secret --destination <id> --generate`,
`sync_roles`. Day-2 operations: [docs/operations.md](docs/operations.md).

## Production prerequisites

Before exposing the platform to the internet you must provide, at the edge:

1. **TLS termination** that always overwrites `X-Forwarded-Proto`, then set
   `DJANGO_TRUST_X_FORWARDED_PROTO=1`.
2. **Rate limiting** on registration, profile, reset, admin/studio login, `/w/` and
   `/mcp`. The application's limits are per-process only (`webinars/ratelimit.py`), and
   the example Node proxy has none.
3. **MFA (or SSO/VPN) for `/admin/` and `/studio/`** — passwords only.

[`docker/nginx.conf.example`](docker/nginx.conf.example) is a starting point for
TLS, trusted forwarding headers and per-client rate limits. It has not been tested;
tune and test it for your traffic. Also pin base images by digest and use the
hash-locked requirements once generated (see [Supply chain](#supply-chain-notes)).

## Validation status

Test runs, image builds and their dates are recorded only in
[docs/validation.md](docs/validation.md) (logs under `artifacts/`, not committed). This
README claims no result: code changed after the last recorded run has not been tested
until validation.md says so. The Compose stack and deployed image have no smoke test,
and the platform is not production-certified.

Compose ships `postgres:16-alpine`; PostgreSQL evidence so far was collected on
PostgreSQL 15.

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
```

## Documentation

| Document | Contents |
|---|---|
| [docs/implementation-guide.md](docs/implementation-guide.md) | **Start-to-finish implementation playbook** (deployment, pages, forms, proxy, lobby, ICS, webhooks, ops, launch checks) |
| [docs/remaining-gaps.md](docs/remaining-gaps.md) | Implemented vs external release gates vs not implemented |
| [docs/validation.md](docs/validation.md) | Release evidence |
| [docs/studio.md](docs/studio.md), [docs/waiting-room.md](docs/waiting-room.md), [docs/email.md](docs/email.md) | Marketer studio, hosted waiting room, operational SMTP |
| [docs/mcp.md](docs/mcp.md), [docs/native-integrations.md](docs/native-integrations.md) | Agent (MCP) access; native vendor integrations status |
| [docs/architecture.md](docs/architecture.md) | Components, module contracts, identity rules, outbox design, roles |
| [docs/api-reference.md](docs/api-reference.md) | Public, registration and proxy API, auth, errors, idempotency, CSRF |
| [docs/management-api.md](docs/management-api.md) | Event/occurrence/attendance write API |
| `GET /api/v1/openapi.json` | Canonical OpenAPI 3.1 description, generated from `webinars/openapi.py` (`docs/openapi.yaml` is superseded) |
| [docs/registration.schema.json](docs/registration.schema.json) | JSON Schema for the registration body |
| [docs/field-mapping.md](docs/field-mapping.md), [docs/field-dictionary.md](docs/field-dictionary.md) | Versioned schema, field mapping and field dictionary |
| [docs/browser-integration.md](docs/browser-integration.md), [docs/integration.md](docs/integration.md) | GTM script, consent, first-party proxy, error shapes, calendar flow |
| [docs/webhooks.md](docs/webhooks.md) | Payloads, signatures, retries, receiver obligations |
| [docs/security.md](docs/security.md), [docs/threat-model.md](docs/threat-model.md) | Security behaviour, threats, mitigations, residual risks |
| [docs/operations.md](docs/operations.md) | Bootstrap, edge, key rotation, backup/restore, rollback, worker, retention, logging |
| [docker/nginx.conf.example](docker/nginx.conf.example) | Untested example edge config: TLS, forwarding, rate limits |
| [examples/verify_webhook.py](examples/verify_webhook.py) | Replay-safe webhook signature verification |

## Supply chain notes

* `requirements.txt` and `requirements-dev.txt` pin exact versions but are **not yet
  hash-locked**; generating and checking a complete transitive lock remains a
  release prerequisite.
* Base images (`python:3.12-slim-bookworm`, `postgres:16-alpine`) are pinned by tag;
  pin them by digest for production.
* The image runs as uid 10001 with a read-only root filesystem, `cap_drop: ALL` and
  `no-new-privileges`. The build context is an allow-list (`.dockerignore`).

## License

Apache License 2.0 — see [LICENSE](LICENSE).
