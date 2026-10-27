# Hosting providers

Link a session (occurrence) to the platform that runs it live. Attendees always use the
stable waiting-room link (`/w/<code>/`); the provider's attendee link stays private and is
only used as the redirect target when the waiting room admits a registrant. The host/start
link is stored encrypted and is never shown anywhere (only `hasHostUrl`).

Code: `webinars/hosting/` (adapters + `service.py`), REST `webinars/api/hosting.py`, studio
`webinars/studio/views_hosting.py`.

## Capability matrix

| Provider | Create | Update | Attach existing | Per-attendee links | Manual link | Contract |
|---|---|---|---|---|---|---|
| Zoom Webinars | yes | yes (PATCH) | yes (GET) | no | no | documented, **not sandbox-verified** |
| Wistia Webinars | yes | no | no | no | no | documented, **not sandbox-verified** |
| Sequel (Introvoke) | yes | no | no | no | no | documented, **not sandbox-verified** |
| Riverside | no (not documented) | no | yes | yes (registrant join links) | no | **unverified** paths/fields |
| StreamYard | no | no | no | no | yes | no public API; zero outbound calls |

Attendance import is not implemented for any provider. Teams and Google Meet are not
supported. "No" means the action is refused with a clear message, never simulated.

Every vendor contract here needs verification against a real sandbox account before
production use. Riverside endpoint paths (`platform.riverside.fm/api/v1/events/{id}` and
`.../registrants`) are our best reading and are constants in `webinars/hosting/riverside.py`.

## Setup (owner / Administrator)

1. The operator adds the provider's API hosts to `WEBHOOK_HOST_ALLOWLIST`
   (Zoom: `zoom.us`, `api.zoom.us`; Wistia: `api.wistia.com`; Sequel: `api.introvoke.com`;
   Riverside: `platform.riverside.fm`). Credentials are only ever sent to these pinned hosts.
2. Studio → Hosting providers → Connect a provider. Credentials are set-only, encrypted with
   `INTEGRATION_MASTER_KEY` and never shown again (blank keeps the stored value).
   - Zoom: Server-to-Server OAuth app (account ID, client ID, client secret) and the licensed
     host user ID or email (needs a Webinars licence).
   - Wistia: API token. Sessions must be at least 15 minutes.
   - Sequel: client ID/secret and company ID.
   - Riverside: API key; create the event in Riverside first, then attach its ID.
   - StreamYard: no credentials. Optional `HOSTING_MANUAL_LINK_HOSTS` (comma list) allows
     extra embed hosts besides `streamyard.com`.

API: `hosting_admin` scope for accounts; `hosting_provision` for preview/execute/reconcile/
manual links; `config_read` may GET providers and an occurrence's hosting state. All three
scopes are superuser-granted and never delegable. Analysts never see these pages.

## Preview → confirm (never automatic)

Publishing an event never creates provider sessions. On a session's Hosting page (or
`POST /api/v1/manage/occurrences/{id}/hosting/preview`) you see exactly what will be sent
(credentials redacted). `POST .../hosting/execute` first answers 428 with the preview and a
single-use `confirmToken`; repeat with header `Webinar-Confirm-Token`. In the studio only
Administrators/superusers can confirm; Editors may preview.

The studio Confirm button carries a signed token issued by the preview render. It is bound to:

- the user, the session, the account, the action and the external id;
- the session's state and schedule generation.

It expires after 10 minutes. A bare POST, or a confirm after the session changed, is refused with "Preview again".

## Durable state and reconciliation

`execute` commits a `ProvisioningOperation` (in flight) before the call, calls the provider
outside any database lock (10 s per operation, 20 s total, 64 KB response cap, redirects
never followed), then records the outcome:

- success → session `linked`; attendee link → `Occurrence.join_url` (private); host link encrypted;
- definitive failure (4xx, auth, DNS, blocked host) → `failed`, safe to retry;
- unknown (timeout, 429/5xx, connection drop, unreadable 2xx) → `needs_reconciliation`.

None of these providers accept an idempotency key for create, so an unknown create is never
re-sent automatically. Check the provider, then record "It was created (ID …)" or "Nothing
was created" (`POST .../hosting/reconcile`). Updates (Zoom PATCH of the full schedule) are
idempotent and may be retried.

Rescheduling marks a linked session `out_of_date`; send an update (Zoom) or change the time
in the provider yourself. Cancelling a session, or unlinking it, never deletes or cancels the
external session.

**Lost operations.** An operation can stay "in flight" if the web request timed out, the worker was killed or a deploy happened mid-call, or recording the outcome failed.

- Any operation still in flight after 5 minutes (`STALE_IN_FLIGHT_SECONDS`) is treated as unknown.
- A lost create or attach moves the session to `needs_reconciliation`, so a person can reconcile it.
- A lost update keeps the link and marks the session `out_of_date`.
- This runs from the worker and before every preview, reconcile or unlink.
- Stale pending registrants likewise go to `needs_reconciliation`.

**Late outcomes are fenced.** An operation records its outcome on the session only while it still owns it: the operation and the session are both in flight and no newer operation exists. A response that arrives after the operation was given up, reconciled or superseded is noted on the operation (`[late outcome: …]`, audit `hosting.operation_late_outcome`) and never copies an ID, join link or host link onto the session. A late *success* of a create/attach on a session reconciled as "Nothing was created" (now `failed`, nothing else in flight) puts the session back in `needs_reconciliation`, so a retry cannot quietly create a duplicate. Late registrant outcomes are fenced the same way (by state and attempt number).

**Unlinking keeps the join link.** "Unlink" keeps `Occurrence.join_url`, because it may have been pasted by hand. Clear or replace it in session details if the old provider link must stop working.

## Riverside registrants

After attach, the worker (`service.sweep_registrants`) creates one registrant per active
registration and stores its unique join link encrypted. The waiting room admits each
registrant to their own link (`service.attendee_join_url`). An unknown outcome marks that
registrant for reconciliation; it is not re-sent. Failures retry up to 5 times.

Riverside links are enforced per registrant. While an attached Riverside session is linked (or out of date), someone without their own linked registrant link is not admitted. The shared `Occurrence.join_url` is never used as a fallback, so links cannot be passed on. Lookup errors also deny admission.

Doors (`service.has_join_destination`): a linked Riverside session does not need a shared `join_url`. Its room opens on the usual clock and controls (countdown, Open, Hold, End, cancel) as long as the Riverside hosting account is enabled. If the account is disabled, the room shows "preparing" and admits nobody, even if a shared `join_url` is set. Admission never re-enables the account and never calls Riverside.

At admission (`POST /w/<code>/enter`), the room checks everything it already checks: the doors are open, the browser is recognised, the registration is active, and a paid ticket has not been refunded or flagged for refund. Then it needs the registrant's own record, which must be:

- linked to this occurrence's session,
- bound to the session's *current* Riverside account and event ID (see below),
- for this occurrence's active registration,
- decryptable,
- an https link on `riverside.fm` / `riverside.com` with no credentials in it.

Anything else denies admission. The link only appears in the 303 `Location`. The status JSON and the room page never include it, and polling never records a check-in.

**Event binding.** One session row is reused across detach and re-attach, so each registrant record also stores the Riverside account and event ID it was made for (`event_account`, `event_external_id`). The binding is captured under the session lock *before* the registrant call. A link counts only while that binding equals the session's current account and event ID. Matching the event ID alone is not enough: the same ID on a different account does not match.

- **Re-attach to another event or account.** Earlier links stop admitting at once. The next recovery pass (`service.recover_stale_operations`, run by the worker sweep and by every reconcile request) moves those records to `needs_reconciliation` and wipes the old link. They are never re-sent automatically, because a registrant may already exist in the new event. Re-attaching the *same* event on the same account keeps the links.
- **Call in flight during a re-attach.** A result that arrives after the event changed is not stored. The record goes to `needs_reconciliation` with no link. A result from an older attempt is fenced by the attempt number as before.
- **Legacy records.** Records created before migration `0010` have no binding. They are denied and flagged the same way, and they are never backfilled. A person confirms them against the current event.
- **Reconciling binds the record to the current event**, under the session and row locks. `created` stores the pasted link for the current event. `not_created` marks the record `failed` for the current event, so the sweep may retry it there. The attempt count is never reset, so the 5-attempt cap still applies and late results from earlier attempts stay fenced. Reconciling is refused while the session is detached.

### Reconciling stalled registrants

Registrants stuck in `needs_reconciliation` (or pending with a lost attempt) are not admitted until a person checks Riverside and records what they found. No Django shell is needed. All three routes call `service.reconcile_registrant`, so the existing fencing, row locks and 5-attempt cap still apply.

- **created**: paste that registrant's *own* https Riverside link (riverside.fm / riverside.com). It is stored encrypted and is set-only: it is never returned, rendered or echoed in a preview.
- **not_created**: the registrant goes back to `failed`, and the worker sweep may retry it within the 5-attempt cap.

| Interface | List | Reconcile | Who |
|---|---|---|---|
| Studio | Session → Hosting → "Riverside registrants to check" (`/studio/sessions/<id>/hosting/registrants`) | per-row form | Administrators / superusers only; Editors and Analysts get 403 |
| REST | `GET /api/v1/manage/occurrences/{id}/hosting/registrants` | `POST …/hosting/registrants/reconcile` `{registrationId, decision, joinUrl?, externalId?}` | list: `hosting_provision` or `config_read`; reconcile: `hosting_provision` |
| MCP | `list_session_registrants` | `reconcile_session_registrant` (gated) | same scopes |

Guarantees:
- **Lists are read-only.** API/MCP lists return registration IDs, state, attempt count, `boundToCurrentEvent` and update time. They also include records bound to another event (or to none) that recovery has not flagged yet. Reconciling such a record first flags it, so a token issued while it still showed `linked` is refused: reload and confirm again. They never include names, emails or links. The studio list also shows a masked email (`j***@example.com`) to Administrators. Listing makes no provider call and writes nothing.
- **Confirmation is required.**
  - REST: the first POST returns 428 with a link-free preview and a single-use `confirmToken`. Repeat the identical body with `Webinar-Confirm-Token`.
  - MCP: call with `dryRun` (the default), then `dryRun: false` plus the token.
  - Each token is bound to the key and the exact arguments (the link only as an HMAC). It is also bound to the registrant's state, attempt number, update time and event binding, and to the session's account, event ID, mode, state and update time. Any new attempt, outcome, reconciliation, detach or re-attach invalidates it. `reconcile_registrant(expected_attempts=…, expected_binding=…)` re-checks the attempt and the current event under the locks. The preview shows `providerEventId`, `providerAccountId` and `boundToCurrentEvent` so you can check which event you are confirming for.
  - Studio: forms carry CSRF and a signed state token bound to the user, session, registrant and that same state, valid for 10 minutes.
- **Refusals.**
  - A registration ID from another session gets 404, the same as an unknown ID.
  - A registrant that is not Riverside, or not in `needs_reconciliation`, gets 409.
  - An invalid link or ID gets 400, and the token is not consumed.
- **Nothing is sent to Riverside.** These routes only record your finding. They do not check the link against Riverside or import attendance. The Riverside contract itself is still unverified against a sandbox (see the provider table).
