# Architecture

Single-tenant Django 5.2 LTS + PostgreSQL 16 application. One container image runs
either the web process (`gunicorn config.wsgi`) or the durable worker
(`python manage.py run_worker`). No Redis: the transactional outbox and reminder
schedule live in PostgreSQL and are claimed with `SELECT … FOR UPDATE SKIP LOCKED`
plus time-bounded leases.

The platform sends no marketing or lifecycle email and no SMS, and hosts no media:
confirmations, reminders and lifecycle changes are emitted as webhook events for an
external email/marketing automation system, and join/recording URLs point at an
external webinar or video service. The only email it sends itself (when
`EMAIL_DELIVERY=smtp`, off by default) is operational: waiting-room verification
codes/links and team invitations ([email.md](email.md)).

## Layout

| Path | Purpose |
|---|---|
| `config/settings.py`, `config/urls.py`, `config/wsgi.py`, `manage.py` | Project, fail-closed production settings from env |
| `webinars/models.py` | **Shared data contract** (do not change field names without updating migrations) |
| `webinars/migrations/0001_initial.py` | Hand-written schema migration matching models.py |
| `webinars/migrations/0002_roles.py` | Data migration creating Administrator / Editor / Analyst groups |
| `webinars/migrations/0003_management_api_scopes.py` | No-op marker for the `content_write` / `attendance_write` scopes |
| `webinars/migrations/0004_idempotency_caller_outbox_person_cascade.py` | `IdempotencyRecord.caller_capability_id`; `OutboxMessage.person` → `CASCADE` |
| `webinars/roles.py` | Role → permission mapping (used by 0002 and `sync_roles` command) |
| `webinars/audit.py` | `audit(action, *, user=None, api_key=None, obj=None, **metadata)` – no PII |
| `webinars/validation.py` | `validate_thank_you_url(url)`, `validate_public_https_url(url)` |
| `webinars/timeutils.py` | IANA timezone + DST validation, local → UTC |
| `webinars/normalization.py` | Canonical fields, aliases, per-form mapping, email/phone normalisation |
| `webinars/identity.py` | Browser capability cookie issue/resolve/revoke, missing-field computation |
| `webinars/attribution.py` | Allowlisted parameters/identifiers, URL stripping, consent gating |
| `webinars/registration.py` | `register(...)` service: idempotency, dedupe, consent, attribution, outbox |
| `webinars/scheduling.py` | Reminders, cancellation, reschedule, recording publication, attendance |
| `webinars/ics.py` | ICS generation with stable UID / SEQUENCE |
| `webinars/api/auth.py` | Hashed scoped API keys |
| `webinars/api/views.py`, `webinars/urls.py` | Public API, proxy-target API, same-origin direct proxy, health, ICS |
| `webinars/api/management.py` | Management (write) API; reuses admin forms and `scheduling` (see `management-api.md`) |
| `webinars/crypto.py` | Fernet encryption (external master key), token hashing, webhook signing |
| `webinars/ssrf.py` | HTTPS allowlist + DNS/IP validation, pinned connection, no redirects |
| `webinars/outbox.py` | `enqueue_event(...)`, payload builder, destination field/purpose filtering |
| `webinars/delivery.py` | Lease-based claim, signed delivery, retries/backoff, dead-letter |
| `webinars/reporting.py` | Minimal aggregate reporting (no inferred attendance) |
| `webinars/admin.py` | Marketing admin UI, role-aware PII masking, outbox retry action |
| `webinars/management/commands/*` | `bootstrap_admin`, `create_api_key`, `run_worker`, `purge_personal_data`, `sync_roles`, `set_destination_secret` |
| `tests/` | pytest-django suite (PostgreSQL required for concurrency tests) |
| `Dockerfile`, `compose.yaml`, `.env.example`, `requirements.txt`, `pyproject.toml` | Deployment |
| `docs/*` | API reference/OpenAPI, management API, field mapping guide, webhooks, security, threat model, operations |
| `docker/nginx.conf.example` | Untested example edge: TLS, forwarding headers, rate limits |
| `examples/verify_webhook.py` | Replay-safe signature verification example |

## Module interfaces (contract between workstreams)

```python
# webinars/validation.py
validate_thank_you_url(url: str) -> None          # https only, host in settings.THANK_YOU_REDIRECT_HOSTS; raises django ValidationError
validate_public_https_url(url: str) -> None       # https only (media/hero/stream URLs)

# webinars/timeutils.py
validate_timezone(name: str) -> zoneinfo.ZoneInfo # raises ValidationError
local_to_utc(local_naive: datetime, tz_name: str, fold: int | None) -> datetime
    # raises ValidationError(code="nonexistent") for spring-forward gaps and
    # ValidationError(code="ambiguous") when fold is None for fall-back overlaps.

# webinars/normalization.py
CANONICAL_FIELDS = ["email","phone","firstName","lastName","name","company","jobTitle","companySize"]
CANONICAL_TO_MODEL = {"email":"email","phone":"phone","firstName":"first_name",...}
normalize_email(value: str) -> str               # raises FieldError
email_key(email: str) -> str                     # casefold for dedupe; no plus/dot stripping
normalize_phone(value: str, region: str | None) -> str   # E.164 only; raises FieldError
normalize_submission(profile: dict, custom: dict, form: FormVersion) -> Normalized
    # Normalized(fields: dict[canonical,str], custom: dict["custom.<k>",str], errors: dict[key,msg])
    # Blank strings are dropped (never erase). Conflicting alias values -> error.

# webinars/identity.py
COOKIE_NAME = settings.CAPABILITY_COOKIE_NAME   # default "webinar_session" (matches sdk/proxy PROXY_SESSION_COOKIE)
issue_capability(person) -> str                  # returns raw token; stores sha256
resolve_capability(request) -> BrowserCapability | None
revoke_capability(cap, reason: str) -> None
set_capability_cookie(response, token) / clear_capability_cookie(response)
missing_fields(person: Person | None, form: FormVersion) -> list[str]   # canonical keys only

# webinars/attribution.py
clean_attribution(raw: dict, consents: dict) -> dict  # {parameters, identifiers, pageUrl, referrer}

# webinars/registration.py
register(*, payload: dict, capability: BrowserCapability | None, api_key: ApiKey | None,
         trusted: bool = False) -> RegistrationResult(status_code:int, body:dict, set_token:str|None, clear_cookie: bool)

# webinars/scheduling.py
create_reminders(registration) -> None
process_due_reminders(now=None) -> int
cancel_occurrence(occurrence, user=None) -> None
reschedule_occurrence(occurrence, *, local_start, tz_name, fold=None, duration_minutes=None, user=None) -> None
publish_recording(occurrence, recording_url, user=None) -> None
record_attendance(registration, kind, user=None, source="admin") -> None

# webinars/ics.py
build_ics(occurrence) -> str

# webinars/crypto.py
encrypt_secret(plain: str) -> str ; decrypt_secret(token: str) -> str
hash_token(raw: str) -> str  (sha256 hex) ; generate_token(nbytes=32) -> str
sign_webhook(secret: str, delivery_id: str, timestamp: int, body: bytes) -> str  # "v1=<hex hmac-sha256>"

# webinars/ssrf.py
class UnsafeDestination(Exception)
validate_destination_url(url: str) -> None       # scheme/host allowlist, static checks
resolve_safe_addresses(host: str, port: int) -> list[str]   # all resolved IPs must be public
post_json_pinned(url, body: bytes, headers: dict, timeout: float) -> (status:int, snippet:str)  # connects to vetted IP, SNI/Host = hostname, no redirects
post_json_bounded(url, body, headers, timeout, total_timeout) -> (status, snippet)
    # URL policy in-process; DNS + pinned TLS POST in a spawned child, terminated/killed at
    # total_timeout (raises DeadlineExceeded, a TimeoutError). Used by delivery.deliver with
    # total_timeout = min(WEBHOOK_TOTAL_TIMEOUT_SECONDS=30, lease remaining - safety margin).

# webinars/outbox.py
enqueue_event(event_type: str, *, registration=None, occurrence=None) -> list[OutboxMessage]
    # MUST be called inside transaction.atomic(); fans out to enabled destinations
    # subscribed to event_type whose required_consents are satisfied.
build_payload(event_type, *, registration, occurrence, destination, delivery_id) -> dict

# webinars/delivery.py
claim_batch(worker_id: str, limit: int = 20) -> list[OutboxMessage]
deliver(message: OutboxMessage) -> None
run_once(worker_id: str) -> int
```

Event types: `registration.created`, `registration.updated`, `occurrence.cancelled`,
`occurrence.rescheduled`, `reminder.due`, `recording.published`, `attendance.recorded`,
`follow_up.requested`.

## HTTP contract (exact, shared with sdk/)

Public (no auth needed for read; registration requires an API key with `register`, `proxy` or `trusted` scope):

* `GET /api/v1/events?q=&topic=<slug>&host=<slug>&when=upcoming|recorded` → `{"count", "results":[EventSummary]}` (public visibility only)
* `GET /api/v1/events/<slug>` → EventDetail (public or unlisted; never join/waiting links)
* `GET /api/v1/occurrences/<uuid>/calendar.ics`
* `POST /api/v1/registrations` → body per SPEC (`schemaVersion:'1'`)

Proxy-target (API key with `proxy` scope; browser cookie forwarded by proxy):

* `GET /api/v1/profile?occurrenceId=&formVersion=` → `{"recognized": bool, "missingFields": [...], "formVersion": "..."}`
* `POST /api/v1/registrations`
* `POST /api/v1/reset` → `{"reset": true}` and an expiring `Set-Cookie`
* `GET /api/v1/my-registration?occurrenceId=` → `{"occurrenceId","recognized","registered","status"}` (+ `thankYou`, `calendar` when registered; no PII)

Same-origin direct proxy (served by Django itself when the CMS is on the same origin):
`GET /webinar-api/profile`, `POST /webinar-api/registrations`, `POST /webinar-api/reset`,
`GET /webinar-api/my-registration` (also needs the custom or double-submit header; rate limited) and
`GET /webinar-api/calendar.ics?occurrenceId=` (public ICS; no cookie, key or origin check).
CSRF defence: exact `Origin` match against `WEBINAR_ALLOWED_ORIGINS`, `Content-Type: application/json`
required on POST, `Sec-Fetch-Site` must be `same-origin` when present, and either the custom header
`X-Webinar-Request: 1` or the double-submit token below (both force a CORS preflight cross-site).

Management API, server-to-server with `content_write` / `attendance_write` / `publication` keys
(`config_read` for read-only `GET` on events, occurrences and waiting rooms):
`/api/v1/manage/{events,occurrences,hosts,topics,forms,registrations/<id>/attendance}` — see
`management-api.md`.

API key header: `Authorization: Bearer <key>` (also accepted: `X-Webinar-Proxy-Key`, `X-Api-Key`).
Key format `wbk_<prefix>_<secret>`; only sha256 of the full key is stored.

Registration success (body identical in every case, so existence is not disclosed; status codes below):

```json
{"status":"registered","occurrenceId":"…","thankYou":{"mode":"message","message":"…"} | {"mode":"redirect","redirectUrl":"https://…"},
 "calendar":{"title":"…","startsAt":"…Z","endsAt":"…Z","timezone":"Europe/London","icsUrl":"/api/v1/occurrences/…/calendar.ics",
             "googleUrl":"https://…"|null,"outlookUrl":"https://…"|null,"office365Url":"https://…"|null,"uid":"…","sequence":0}}
```

Errors: `{"error":{"code":"validation_error|idempotency_conflict|not_found|registration_closed|unauthorized|forbidden|origin_rejected","message":"…","fields":{"email":"…"}},"fieldErrors":{"email":"…"}}`
with 400/409/404/410/401/403 (`fieldErrors` mirrors `error.fields` for the browser SDK and is present only when there are field errors).

* `201` for a new registration and for **every** unverified submission (unknown browser, existing
  email), including duplicates, so the status never reveals that an email is already registered.
* `200` only when a capability holder repeats a registration for their own person.
* An idempotent replay returns the stored status and body.
* Cookie-less callers always receive a `Set-Cookie`: a real capability only when a new person was
  created, otherwise a random, never-stored decoy of the same shape.

Other error codes: `method_not_allowed` (405), `payload_too_large` (413), `unsupported_media_type`
(415), and `conflict` (409, management API).

Same-origin POSTs accept either `X-Webinar-Request: 1` or the SDK double-submit token `X-Webinar-CSRF` equal to the `__Host-webinar_csrf` cookie (issued by `GET /webinar-api/profile`).

## Identity rules

1. Person UUID is server-generated; registration UUID and browser capability are separate.
2. Capability = 32 random bytes, cookie `webinar_session`, HttpOnly, Secure, SameSite=Lax, Path=/, 180 days; sha256 stored; revocable (`/reset`, admin, purge).
3. No valid capability + email of an existing person → *unverified submission*. The registration
   is linked to that person for per-occurrence dedupe, but the person's profile and consent
   records are **not** updated, the submission is kept only in `Registration.submitted_profile`,
   `identity_verified=False`, and **no capability is issued** (the response carries a random decoy
   cookie that resolves to nothing). If that person is already registered for the occurrence, the
   existing registration is left untouched (no reactivation, consent, attribution, reminder or
   webhook) and only an audit entry is written. Email is never verified, so there is no account
   recovery and no cross-device linking.
4. No capability + new email → new Person, capability issued.
5. Valid capability + same/absent email → profile updated with non-blank values only.
6. Valid capability + different email → treated as registering someone else: handled as rule 3/4
   for that email, but the browser's cookie is never rebound.
7. Profile endpoint reveals only missing field keys; anonymous → full form.
8. Attribution and ad identifiers never authenticate.
9. Unverified submissions (rule 3) record **nothing** in `ConsentRecord` — neither grants nor
   withdrawals. The claim is stored only in `Registration.submitted_profile["consentClaims"]`.
   Browser org IDs from unverified submissions are likewise kept only on the registration, and
   their attribution touch belongs only to that new registration (never a first touch).
10. "Not you?" reset revokes the browser's capability and clears the cookie. It does **not** recover
    or re-link an identity: a later submission with an existing email is unverified (rule 3).
11. An idempotent replay never issues a real capability. A cookie-less replay gets a decoy cookie;
    if the original response carrying a new person's capability was lost, that browser stays
    unrecognised. A replay carrying a capability is allowed only for the same caller
    (`IdempotencyRecord.caller_capability_id`) or the capability's own registration; otherwise 409.
12. Webhook payloads for unverified registrations carry only the submitted profile and org IDs,
    report every consent as not granted, set `firstTouch` to null and mark `identity.verified=false`
    (see `webhooks.md`).

## Outbox / worker

Rows are written in the same transaction as the business change. Worker loop:
claim up to N rows (`status in (pending) and next_attempt_at<=now` or `in_flight` with expired lease)
using `select_for_update(skip_locked=True)`, set lease (default 120 s), deliver outside the
transaction, then update status. Exponential backoff with jitter capped at 1 h, `max_attempts`
per destination then `dead`. Admin action requeues dead messages. Delivery id = `OutboxMessage.id`
(stable across retries). Headers: `Webinar-Delivery-Id`, `Webinar-Timestamp`, `Webinar-Signature: v1=<hmac>`
over `"{delivery_id}.{timestamp}.{body}"`.

Each send runs under a total deadline (`post_json_bounded`, see `security.md`). Deleting a Person
deletes their outbox messages (`CASCADE`).

## Roles

Defined in `webinars/roles.py`; `sync_roles` replaces each group's permissions with exactly this mapping.

* **Administrator** – everything in the webinars app incl. people, destinations, API keys, outbox,
  audit log and reports. Users and groups are **view-only**.
* **Editor** – events, occurrences, series, hosts, topics, forms (add/change/view), attendance entries. No PII.
* **Analyst** – read-only events/occurrences/series/hosts/topics/forms, registrations with PII masked, reporting.
* **Superuser** – the only account type that can create or change users, groups and role membership.
  `bootstrap_admin` creates the first account as a superuser in the Administrator group.
