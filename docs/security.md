# Security notes

These notes cover the identity and delivery hardening. See `docs/architecture.md` for the module contracts and `docs/threat-model.md` for threats and residual risks. The platform is a preview and is not production-certified.

## Required at the edge before internet exposure
The application has no built-in TLS termination, rate limiting or admin MFA. Before exposing it to the internet:
- terminate TLS at a reverse proxy that always overwrites `X-Forwarded-Proto`, then set `DJANGO_TRUST_X_FORWARDED_PROTO=1`;
- rate-limit registration, profile, reset and `/admin/login/` per client;
- put `/admin/` behind MFA (an identity-aware proxy, SSO or VPN).

`docker/nginx.conf.example` is an untested starting point.

## Identity without email verification
- Email addresses are never verified. A browser is recognised only by the capability cookie issued to the browser that created the person. There is **no account recovery and no cross-device recognition**.
- An existing email submitted from a browser without that person's capability is an *unverified submission*. It never updates the person's profile or consent records (neither grants nor withdrawals). It never reactivates or changes an existing registration, and never yields a capability. The submission is kept on the registration only.
- A recognised browser cannot correct its email. Submitting a different email registers someone else, and the cookie is not rebound.
- **"Not you?" reset does not recover an identity.** It revokes the capability. Afterwards the browser is anonymous, and submitting an existing email is unverified.

## Admin users
Only superusers can create or change users, groups and role membership. The Administrator role can view them but not change them. After upgrading a deployment created before this change, run `manage.py sync_roles` (migration `0002` does not re-run).

## Idempotent retries and capabilities
- A retry with the same `idempotencyKey` and the same payload replays the stored response body. The body is uniform: it holds no profile, identity, join links or waiting-room links.
- **A replay never issues a real capability.** Knowing the key and the payload does not prove that the caller owns the person. An attacker can submit the victim's email (even onto the victim's own registration) and then replay their own key, so no timing or key-length rule can tell the two cases apart.
  - A cookie-less replay gets an unbound decoy `Set-Cookie`, the same as any cookie-less response.
  - **Accepted consequence:** if the response that carried a new person's capability is lost, that browser stays unrecognised. It recovers by registering again or using "Not you?". Later submissions with that email are treated as unverified until then.
- A retry that carries a capability replays when either:
  - `IdempotencyRecord.caller_capability_id` equals that capability's id. This covers the same browser retrying after registering someone else (rule 6). The field stores an id, never a token.
  - Or the stored registration belongs to the capability's person (an anonymous request succeeded and the retry carries the issued cookie).

  Any other capability gets `409 idempotency_conflict`.

## Webhook delivery
- **SSRF:** the URL policy, DNS vetting of every address, the pinned connection with SNI and certificate verification, and the ban on redirects are unchanged (`webinars/ssrf.py`).
- **Total deadline:**
  - Each send runs DNS and the HTTP exchange in a spawned child process (no threads), so a slow resolver or a receiver that drips its response cannot outlive the lease.
  - At `min(WEBHOOK_TOTAL_TIMEOUT_SECONDS (default 30), remaining lease - 9s)` the child is terminated, then killed, then joined. This is recorded as a `timeout` and retried with backoff. `WEBHOOK_TOTAL_TIMEOUT_SECONDS` is configurable through the environment (5–600 seconds); the lease cap still applies. See [operational settings](operational-settings.md).
  - The receiver may still have received the request, so delivery stays at-least-once: dedupe on `Webinar-Delivery-Id`.
  - `OUTBOX_LEASE_SECONDS` must comfortably exceed about 10s.
- **Send-time consent gate:** when a destination has `required_consents`, delivery cancels and purges the message if any of these hold:
  - the payload marks the identity as unverified;
  - the source registration cannot be found;
  - the registration is unverified;
  - the required consent is no longer granted.

  This catches messages queued before enqueue applied the unverified-registration rule.
- **Unverified payloads:** an unverified registration's webhook carries only the submitted profile and org IDs, reports every consent as not granted and sets `firstTouch` to null. `identity.personId` and the submitted email are still present, and `identity.verified=false` is the signal.
- **Receiver obligations:**
  - Never merge `verified=false` payloads into verified CRM records.
  - Never send SMS unless the payload is verified, `consents.sms.granted` is true and the number is that payload's `profile.phone`. The consented phone snapshot is not in the payload.
  - Dedupe on `Webinar-Delivery-Id`.
- **Deletion:** `OutboxMessage.person` is `CASCADE` (migration 0004). Deleting a Person deletes their queued and historical messages, so no personal-data payload outlives the Person row. Erasure (`erase_person`) still marks rather than deletes.
