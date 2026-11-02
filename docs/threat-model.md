# Threat model

Scope: the single-tenant Django/PostgreSQL application, its worker, the Compose
deployment, the GTM browser script and the example first-party proxy (`sdk/`). The
optional live video service in `live/` has [its own section](#your-own-live-video-live).
This document describes the implemented controls. Many are covered by the test
suite, but the final PostgreSQL run and the image build are still pending (see the
README's validation status), and no internet-facing deployment has been tested. The
platform is a preview and is **not** production-certified. Behavioural detail:
[`security.md`](security.md).

## Assets

| Asset | Where |
|---|---|
| Registrant PII (email, phone, names, company, job title, custom fields) | `Person`, `Registration.submitted_profile`, `OutboxMessage.payload` |
| Consent evidence (SMS disclosure, analytics/advertising choices) | `ConsentRecord` |
| Attribution data and ad/analytics cookie IDs | `AttributionTouch` |
| Organisation external IDs | `OrganizationExternalId` |
| Browser capability tokens | Cookie `webinar_session` (raw; name configurable) / `BrowserCapability.token_hash` (SHA-256) |
| API keys | Holders (raw) / `ApiKey.key_hash` (SHA-256) |
| Webhook signing secrets and destination credentials | `Destination.*_encrypted` (Fernet) |
| Fernet master key, Django secret key, DB password | Environment (`.env`) only |
| Private join / waiting-room links | `Occurrence.join_url`, `waiting_room_url` |
| Admin accounts and role assignments | Django auth tables |
| Integrity of event schedule, reminders and outbound webhooks | Occurrence, ReminderSchedule, OutboxMessage |

## Actors

* **Anonymous visitor** — browses public events, submits registrations via the GTM form.
* **Registrant with capability** — browser that created or legitimately owns a profile.
* **Malicious visitor** — knows or guesses a victim's email, replays requests, scripts submissions.
* **Cross-site attacker** — hosts a page that tries to drive the victim's browser.
* **CMS / proxy operator** — holds a `register`/`proxy` key.
* **Trusted integrator** — holds a `trusted` key.
* **Marketing users** — Administrator, Editor, Analyst roles in Django admin.
* **Webhook receiver** — external HTTPS endpoint; possibly compromised.
* **Network attacker** — between browser↔proxy, proxy↔app, worker↔receiver.
* **Supply-chain attacker** — via PyPI packages, base images, npm (none used by the proxy: stdlib only).

## Trust boundaries

1. Browser ↔ marketing site / first-party proxy (`/webinar-api/*`, cookies, Origin).
2. Proxy ↔ Django API (server-held `proxy` key over HTTPS).
3. Reverse proxy/TLS terminator ↔ gunicorn (`X-Forwarded-Proto` trust is opt-in).
4. Django ↔ PostgreSQL (internal Compose network, no published port).
5. Worker ↔ external webhook receivers (egress, SSRF controls).
6. Admin users ↔ Django admin (role permissions).
7. Operator ↔ `.env` / master key / backups.

## Threats and mitigations

| # | Threat (STRIDE) | Mitigations | Residual risk |
|---|---|---|---|
| T1 | **Profile hijack via email** (S/I): attacker submits a victim's email to view or overwrite their profile, change their consent, or obtain their capability. | Email match without a valid capability links the registration for per-occurrence dedupe only: profile and consent records not updated (not even withdrawals), submission kept in `submitted_profile`, `identity_verified=false`, **no capability issued** (decoy cookie only). An existing registration is never reactivated or changed by an unverified duplicate. Profile endpoint returns `recognized=false` and the full form. A browser with a capability submitting a different email is "registering someone else" and the cookie is never rebound. Idempotent replays never issue a real capability. | Attacker can register the victim's email for *another* open occurrence, which creates reminders and an unverified `registration.created` webhook (no double opt-in). Admins and receivers see `identity_verified=false`. No email verification, account recovery or cross-device linking exists. |
| T2 | **Capability theft** (S/I): XSS, logs, or network capture of the capability cookie (`webinar_session` by default). | 32 random bytes; cookie `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, 180-day expiry; only SHA-256 stored; revocable via reset/admin/purge; never in URLs, logs or responses. A stolen capability reveals only *which fields are missing*, never values. | XSS on the marketing site can still make requests with the cookie (the site's CSP is outside this project). Theft lets an attacker add non-blank values to that profile. |
| T3 | **CSRF on proxy / same-origin endpoints** (T). | Exact `Origin` allowlist, `Content-Type: application/json`, `Sec-Fetch-Site` same-origin when present, mandatory `X-Webinar-Request: 1` or double-submit `X-Webinar-CSRF` matching the `__Host-webinar_csrf` cookie (both force preflight; no CORS is granted). Example proxy also validates origin and a CSRF token. `SameSite=Lax` cookie. Admin uses Django CSRF with `CSRF_TRUSTED_ORIGINS`. | Relies on browsers sending `Origin` on POST (all current browsers do). |
| T4 | **SSRF via webhook destinations** (I/E). | HTTPS only; host must be on `WEBHOOK_HOST_ALLOWLIST`; all DNS answers must be public (private, loopback, link-local, metadata, CGNAT, multicast, reserved blocked); connection pinned to the vetted IP (no DNS rebinding window) with SNI/Host = hostname; redirects never followed; bounded timeout and response snippet. Only Administrators manage destinations. | Allowlisted hosts are trusted to be what they claim; a compromised allowlisted receiver gets the data it is configured for. |
| T5 | **Secret leakage** (I). | Secrets only in env; Fernet-encrypted destination secrets with external master key and rotation; API keys/capabilities stored hashed; `.env` excluded from build context (allow-list `.dockerignore`); build-time collectstatic uses throwaway values not persisted to image env; settings fail closed when secrets are missing in production. | Anyone with `.env` or host root has everything. Backups contain ciphertext only, but backups + master key = plaintext; store separately. |
| T6 | **PII in logs / audit** (I). | `AuditLog` stores ids, action names and non-sensitive metadata only. gunicorn access log omits query string, client IP, referrer and user agent. Error messages never echo submitted values. Admin masks PII for Analysts. | Unhandled-exception tracebacks could contain local variables if DEBUG were enabled — DEBUG must stay off. Upstream reverse-proxy logs are the operator's responsibility. |
| T7 | **Webhook replay / forgery** (S/T). | `Webinar-Signature: v1=HMAC-SHA256(secret, "{delivery_id}.{timestamp}.{body}")`, timestamp tolerance (300 s in the example verifier), stable `Webinar-Delivery-Id` for receiver dedupe; `examples/verify_webhook.py`. | Receivers must implement verification and dedupe; the platform cannot enforce that. |
| T8 | **Idempotency abuse** (T/I): reusing another client's key to read their response, steal a capability or block them. | Key bound to a payload hash: mismatches return `409` without data. Replays carrying a capability are allowed only for the original caller or the capability's own registration. Replayed responses contain no PII and never a real capability. Keys are client-random (UUIDv4). | Keys are global, so a guessed key could cause a 409 for a victim (requires guessing 122 random bits). A lost capability response is not recoverable (accepted, T22). |
| T9 | **Enumeration** (I): learning whether an email is registered/known. | Identical bodies; every unverified submission returns `201`; cookie-less callers always get a `Set-Cookie` (a decoy when no person was created); profile endpoint keyed on capability only; errors never reveal existence of people. | Response timing differs (a new email inserts a person and a capability). |
| T10 | **Consent spoofing** (R/T). | Client consent is recorded as `client_claim` with `server_verified=false`; the column is never set from client input. Unverified submissions record no consent at all. SMS consent stores disclosure version/text snapshot and phone snapshot; withdrawals append records. Destinations with `required_consents` receive messages only for verified registrations whose purpose is granted, re-checked at send time. No SMS is sent by this system. | Client claims are only as trustworthy as the page that collected them; downstream systems must treat them as claims. The consented phone snapshot is not included in webhooks (T21). |
| T11 | **Attribution used as authentication** (S). | Attribution and ad/analytics IDs are never consulted for identity. Allowlisted parameters/identifiers only, consent-gated, URLs stripped of query/hash, no fingerprinting, no whole-cookie harvesting. | Attribution data is attacker-controllable and may be false; treat as marketing signal only. |
| T12 | **Organisation ID spoofing** (S). | Browser-supplied external IDs stored `source=browser`, `verified=false`; DB check constraint forbids `verified=true` unless `source=trusted_api`; only `trusted` keys create verified IDs. | Downstream CRMs must not auto-merge on unverified IDs. |
| T13 | **Admin role abuse** (E/I). | Administrator / Editor / Analyst groups with least privilege (Editors see no PII; Analysts read-only with masking). Administrators can only *view* users and groups; only superusers manage users, groups and role membership, so an Administrator cannot grant itself superuser. `sync_roles` replaces group permissions exactly; audit log of admin actions; first admin created via `bootstrap_admin` (no default passwords). | A malicious Administrator can export all data. Any account given `auth.change_user` directly (outside the roles) can still escalate; there is no restricted user admin. Existing deployments must run `sync_roles` to drop old grants. No built-in MFA — **required** at the edge (SSO/VPN or an MFA-enforcing proxy) before internet exposure. |
| T14 | **Private link exposure** (I). | `join_url`/`waiting_room_url` never in public API, ICS, MCP or registration response, (with the managed room on, calendar files and add-to-calendar links carry the room URL `/w/<code>/` as location and first line instead: it asks for an email and a code at the door, and paid sessions check out there), and never rendered into Studio HTML for roles without `change_occurrence`. When the managed waiting room is enabled, webhooks **never** carry the provider `joinUrl`/`waitingRoomUrl`, even with `include_private_links`; they carry the stable managed room URL instead. Otherwise provider links are included only when the destination sets `include_private_links`. Queued messages are re-filtered at send time against the *current* destination settings (private links, allowed groups, field mapping), consent/erasure and managed-room state, regenerated from the canonical payload so mappings are applied once. | Receivers that email links forward them to registrants, including registrants whose identity was not verified (public registration does not verify email, so the room link is no more secret than registration). |
| T15 | **Open redirect** (S). | Thank-you redirect must be HTTPS and host in `THANK_YOU_REDIRECT_HOSTS`, validated on save and on response. | — |
| T16 | **Denial of service / spam** (D). | Body-size limits in the app and proxy, bounded key counts and lengths, bounded worker retries with leases and dead-letter, total per-send deadline. | **No built-in rate limiting.** An edge with per-client limits on registration, profile, reset and `/admin/login/` is **required** before internet exposure; see `docker/nginx.conf.example` (untested). Per-IP limits on `/api/v1/*` would throttle the first-party proxy as a whole, so limit visitors where they arrive. |
| T17 | **Worker races / duplicate sends / stalls** (T/D). | `SELECT … FOR UPDATE SKIP LOCKED` claims with time-bounded leases, per-message lease renewal before each send; each send runs in a child process killed at `min(30 s, lease remaining − 9 s)`; delivery id stable across retries; reminders keyed by `(registration, offset, generation)`; reschedule supersedes, cancellation suppresses, late reminders skipped. | At-least-once delivery: a send killed at the deadline may already have arrived, so receivers must dedupe by delivery id. |
| T18 | **Supply chain** (T/E). | Exact version pins (hash-locking required before release), multi-stage build with runtime installing only built wheels, base images pinned by tag (digest pinning documented), non-root uid 10001, read-only root fs, `cap_drop: ALL`, `no-new-privileges`, stdlib-only Node proxy. | The pins resolved and installed in validation, but requirements are not yet hash-locked and base images are not digest-pinned. No SBOM/signature verification yet. |
| T19 | **Data retention / erasure gaps** (I). | `purge_personal_data` clears Person PII, submitted profiles, consents, attribution touches, organisation IDs, capabilities and queued outbox payloads; supports `--person`, `--older-than-days`, `--dry-run`. Deleting a Person deletes their outbox messages (`CASCADE`); erasure and withdrawn consent are re-checked at send time. | Data already delivered to webhook receivers and data in existing backups is outside the command's reach; document backup expiry. A send already in progress when erasure commits still completes. |
| T20 | **Host header / scheme spoofing** (S). | `ALLOWED_HOSTS` required; `X-Forwarded-Proto` trusted only when `DJANGO_TRUST_X_FORWARDED_PROTO=1`; secure cookies and HSTS in production settings. | Misconfiguring the trust flag behind a proxy that does not overwrite the header would allow scheme spoofing. TLS termination is not built in and is required at the edge. |
| T21 | **Downstream misuse of unverified data** (S/T): a receiver merges an attacker-submitted profile into a real CRM record or texts an attacker-chosen number. | Unverified payloads carry only the submitted profile and org IDs (`verified:false`), report every consent as not granted, omit first touch and set `identity.verified=false`; consent-restricted destinations never receive them. | `identity.personId` and the submitted `identity.email` are still sent. Receivers **must not** merge `verified=false` payloads into verified records and **must not** send SMS unless the payload is verified, `consents.sms.granted` is true and the number is that payload's `profile.phone`. The consented phone snapshot is not in the payload, so a receiver cannot detect a phone change made after consent. |
| T23 | **Studio preview mailbox abuse** (E/I): an Administrator reads another user's invitation or a registrant's room code/magic link from the preview mailbox. | Preview mailbox is superuser-only and Administrators hold no `previewmessage` permissions (re-run `sync_roles`). Invite links are exchanged for a session-held token so the raw token does not persist in URLs or referrers. Account email change requires the current password. | Superusers can still read previewed mail; the raw token still appears once in the proxy access log for the first request, so redact `/studio/invite/` paths at the proxy. |
| T24 | **Waiting-room login CSRF / code lockout** (S/D). | A magic link opened in a browser without the matching pending cookie shows the email being verified and requires an explicit "This is me" confirmation ("Not me" discards it), so there is no silent login. Code issuance and guesses are limited per email across rooms as well as per room; with proxy trust enabled the client IP is taken from the rightmost untrusted `X-Forwarded-For` hop. Check-in and End serialize on the occurrence row lock. | An attacker can still use up a victim's issuance budget for a while. Per-IP limits depend on the proxy hop count being configured correctly. |
| T22 | **Lost recognition** (D, accepted). | Replays never issue capabilities (prevents T8 theft). | A browser whose capability response was lost, or that used "Not you?", stays unrecognised for an existing email; there is no recovery flow. |

## Your own live video (`live/`)

The optional service runs on its own server with LiveKit, Egress, Redis, Whisper, a slide converter and Traefik. Its own assets are: crew links and attendee links, `/admin` accounts, stream keys, the service secret shared with VisionCast, recordings, caption files, chat and questions. Trust boundaries: browser to service, browser to LiveKit, VisionCast to service (bearer secret over HTTPS), Egress to the service's program page, and the service to the bucket.

Each threat below came from adversarial review of this code. The fixes have service tests or browser tests unless the row says otherwise, and `E2E_SUITE=attack live/tests/e2e/run.sh` makes most of these attacks against a running stack. None of this has been tested on a public server.

| # | Threat (STRIDE) | Mitigations | Residual risk |
|---|---|---|---|
| L1 | **Forged or altered links** (S/E). | Links are signed tickets carrying room, role and identity, and are checked on every request. Attendee links are bound to one browser at a time: opening one elsewhere closes the first viewer. | A ticket is a bearer credential, and it appears in the first request's URL and in download links, so it can reach proxy logs and browser history. `/join/{link}` redirects to a clean address. |
| L2 | **Publishing without permission** (E). | LiveKit tokens carry the rights for the role, so LiveKit refuses the publish itself, whatever the page does. Producers can only publish in the green room. Attendees get no LiveKit token before going live. The stage is a separate room from the green room. | None known. Tested by having an attendee, a producer and an off-stage guest really try. |
| L3 | **Someone removed keeps watching** (E). | Crew are revoked by ID, not by role: removal disconnects their event stream and LiveKit connection, and a new link voids the old one. LiveKit tokens last 15 minutes. Webhooks from LiveKit are checked against who should be there, and anyone who shouldn't be is removed. | The old credential can still watch for up to 15 minutes after a new link is issued, if it was already connected to LiveKit. Removing someone from the Team page in the Studio doesn't remove their host link: use the session's crew panel. |
| L4 | **Crew link sharing** (S/R). | Each crew member has their own link, named and revocable. The activity log says who did what. | Anyone holding a link is that person: there's no second factor on crew links. |
| L5 | **Password guessing on `/admin`** (S). | Per-person accounts, Argon2. Failures are counted before the check: in 15 minutes, 8 tries at one email from one address, and 60 at one email from anywhere. Only four password checks run at once, so a flood can't use up the CPU. Traefik also rate limits `/login`. The client address comes from `X-Forwarded-For` only with `LIVE_TRUST_FORWARDED_FOR`. | No second factor. Put `/admin` behind an identity-aware proxy or VPN if you need one. |
| L6 | **An admin locks everyone out** (D/E). | The last owner can't be removed or demoted, under a lock. `reset-admin` on the server recovers an account, and makes an owner if none is left. | Anyone with shell access to the server can do the same. |
| L7 | **Attendees seeing crew material** (I). | Attendees get a projection of the room's state that leaves out drafts, the people list, crew chat, held messages, pending questions and unpublished results. Guests get no draft polls or unshared files. Anonymous questions drop the asker's identity for crew as well. | Poll tallies are sent even with `show_counts` off, so a viewer reading the event stream can see them. Guests and promoted attendees can see attendee names in crew chat. |
| L8 | **Path traversal and bad uploads** (T/D). | File names come from a lookup rather than from the request, and tickets are checked for safe characters. Logos are decoded with size limits, which stops image bombs. Deck conversion runs in a container with no network and with CPU, memory and time limits, one job per folder. | A deck is readable by other queued jobs inside the converter while it waits. |
| L9 | **Stream keys leaking** (I). | Keys are stored and sent only to Egress, never on pages, event streams or logs. They're masked in lists. The tests look for a known key in each. | Anyone with the server's database file can read them. |
| L10 | **SSRF through broadcast or graphics URLs** (T/I). | Broadcast destinations must be `rtmp` or `rtmps` to a public address, with IPv4-mapped addresses caught as well. VisionCast's calls to the service go through its existing allowlist, with a 64 KiB response limit, and the service's host must be on `WEBHOOK_HOST_ALLOWLIST`. | Egress itself connects to whatever a destination's name resolves to at the time. |
| L11 | **Load and abuse by a viewer** (D). | At most 20 actions a second per person, 5 event streams per attendee, capped chat and upload sizes. Local recording: 32 MB a chunk, 8 GB a file, 20 files per person, 100 GB per room, and only from people on stage while recording. Idle rooms are evicted after 10 minutes. | A person holding a breakout seat through `/act` without connecting keeps it until the breakouts close. The limits are per person, so a registration script can multiply them. |
| L12 | **Personal data left behind** (I). | Erasing a person in VisionCast also calls the service, which deletes their chat, questions, captions, votes, survey answers and attendance, and VisionCast won't accept their engagement again. Purge deletes inputs from LiveKit Ingress as well as files, and keeps the brand. | Recordings, caption files and local recordings can contain a person's voice and name, and are not edited on erasure. Local recordings stay on the server's disk until the room is purged. |
| L13 | **Share source trust** (S). | Screen and media sharing are limited to crew and people invited on stage. | The picture is whatever the sharer's browser sends: a person on stage can show anything until a producer removes them. |
| L14 | **TURN abuse** (E/D). | TURN credentials are issued by LiveKit for the session, on 443 over TLS and on a UDP port. | Relay through TLS has been proved up to the point of getting a relay address, not with media crossing it, because that needs a public server. |

### Live video is out of scope for

* Protecting against a compromised host, or against someone holding `.env`, the database file or the bucket's keys.
* End-to-end encryption. Media is encrypted in transit, and LiveKit, Egress and Whisper see it unencrypted on your server.
* Moderating what's said on stage or scanning shared files for malware.

## Explicitly unsupported / out of scope

* Email (or SMS) verification, and therefore account recovery and cross-device profile recognition.
* Email correction for a recognised browser (only "Not you?" reset, which does not recover identity).
* Built-in rate limiting, bot detection, CAPTCHA, TLS termination (all required at the edge).
* MFA for admin users (required via an upstream identity-aware proxy, SSO or VPN).
* Sending SMS. Hosting media or streaming in the core application: the optional `live/` service does that, on its own server.
* Multi-tenancy.
* Guarantees about availability of third-party ad/analytics cookies in the browser.
