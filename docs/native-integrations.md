# Native integrations

Status: **twelve adapters (PostHog, HubSpot, Customer.io, Attio, Segment, Amplitude, Google Analytics 4, Mixpanel, RudderStack, Brevo, ActiveCampaign, Mailchimp) are implemented in source on `ssrf.request_json_bounded`; their tests are mock-only, and none is production-ready or live-verified.** ActiveCampaign and Mailchimp live in self-contained modules under `webinars/native_providers/` (registered through `PROVIDER_MODULES`); their request shapes were compared with official reference pages fetched 2026-10-27 (see their sections). Mixpanel and RudderStack request shapes were compared with primary-doc facts fetched 2026-10-25; some contract points are still open (see below). That function is a bounded POST/PUT transport: response body up to 64 KB, `Retry-After` parsed, and every native request pinned to the destination's own configured host. If the function were ever missing, HubSpot and Attio fail validation, and their deliveries are dead-lettered with `native_transport_unavailable` without any network call.

The vendor API contracts below are **UNVERIFIED** assumptions. Testing so far is mocked contract tests only. Nothing has been checked against a real vendor or sandbox account: no accounts are connected and no external calls have been made. The API shapes below come from the implementer's knowledge of the vendors' public documentation and **must be checked against the current official docs** (list at the end) before anyone enables these integrations.

## How it fits

- **No new model field.** A destination's encrypted credentials (`Destination.credentials_encrypted`) hold flat `native_*` keys: `native_provider`, `native_region`, provider settings and provider secrets. With no `native_provider`, the destination is a generic signed webhook, exactly as before.
- **Shared module.** `webinars/native_integrations.py` is generic, so the MCP or admin config can reuse it:
  - `FIELDS` (per-provider field specs)
  - `merge_credentials` (blank secret = keep)
  - `validate` (no network)
  - `preview` (redacted, synthetic sample)
  - `execute` (one delivery)
- **Delivery.** `webinars/delivery.py` keeps the claim/lease, erasure and consent re-checks and the retry, dead and cancel bookkeeping, then hands native destinations to `execute` with a `NativeContext`:
  - The lease is renewed before every vendor call after the first. If renewal fails, the run stops and nothing is recorded.
  - There are at most 6 calls per message.
  - Each call goes through `ssrf.request_json_bounded`:
    - Methods are POST or PUT only. Method and headers are adapter constants, never user input.
    - The same URL and port policy as webhooks, with DNS vetting and a pinned IP for TLS.
    - Redirects are returned but never followed.
    - A spawned child has a hard deadline inside the lease.
    - Responses are capped at 64 KB. Larger bodies are dropped and treated as retryable `response_too_large`.
    - `Retry-After` (seconds or HTTP-date) is capped at 3600 s and used as the minimum delay before the retry.
    - Every native request is refused unless its host is the destination's configured host: `_deliver_native` passes `native_integrations.allowed_hosts(provider, credentials)` (one host: the chosen provider and region, or the RudderStack data plane) as `allowed_hosts`. A key for one provider or region is never sent to another vendor's host, even one in `VENDOR_HOSTS` and on the operator allowlist; such a call is dead-lettered as `<step>: unsafe_destination` before any connection. If the configuration no longer resolves to a host, the set is empty and every call is refused.
    - Only scalar record IDs are parsed out of response bodies. Response bodies never reach `last_error` or the logs.
- **Outcomes.**
  - 2xx: delivered.
  - 408, 425, 429, 5xx, 3xx, DNS failures and transport errors: retried with backoff.
  - Other 4xx (auth, schema, 409 conflict): dead-lettered.
  - Unverified identity, no person, or unknown attendance: cancelled, and the payload is purged.
  - `last_error` holds only a step name and a code, never response bodies or secrets.
- **Hosts are a strict per-vendor allowlist.** Credentials are only sent to these hosts:
  - PostHog: `us.i.posthog.com`, `eu.i.posthog.com`
  - Customer.io: `track.customer.io`, `track-eu.customer.io`
  - HubSpot: `api.hubapi.com`
  - Attio: `api.attio.com`
  - Segment: `api.segment.io`, `events.eu1.segmentapis.com`
  - Amplitude: `api2.amplitude.com`, `api.eu.amplitude.com`
  - Google Analytics 4: `www.google-analytics.com` (no regional GA host is assumed)
  - Mixpanel: `api.mixpanel.com`, `api-eu.mixpanel.com`, `api-in.mixpanel.com` (unverified)
  - RudderStack: the destination's own data plane host, which must be an exact operator allowlist entry (see below)
  - Brevo: `api.brevo.com` only (no other region or host)
  - ActiveCampaign: the account's own API host (`<account>.api-us1.com`), typed by the marketer and required to be an exact operator allowlist entry, exactly like the RudderStack data plane
  - Mailchimp: `<data center>.api.mailchimp.com`, derived from the configured data center, which must equal the API key's suffix and be an exact operator allowlist entry

  The operator must also add the host to `WEBHOOK_HOST_ALLOWLIST`. The destination URL is set from provider and region and is read-only in the studio. Self-hosted PostHog and HubSpot EU-specific hosts are **not supported**.
- **Studio.**
  - A provider dropdown, with fields for the provider's settings.
  - A separate credential form for secrets. Blank keeps the stored value, and values are never rendered.
  - A "Check before enabling" panel and a redacted request preview.
  - When the sample plan is refused, `native_integrations.preview` still returns `[]` (public shape unchanged). The panel adds "Preview unavailable (`code`): guidance", built by `preview_refusal`. The codes are a fixed set (`PREVIEW_REFUSALS`): `native_match_strategy_unconfirmed`, `native_config_invalid`, `native_config_incomplete`, `native_delivery_id_missing`, `preview_event_skipped`, `preview_payload_unsupported`, and `preview_refused` as the fallback for anything else. Payload values, provider text, exception messages and secrets are never shown. A provider with no usable host gets no preview refusal, because `validate` already reports it.
  - Enabling is refused until `validate` passes.
  - The generic credential form keeps provider keys, and `native_*` names are reserved.
  - Switching provider drops the previous provider's keys, with a visible notice.
- **Shared rules.**
  - Supported events: `registration.created`, `registration.updated`, `attendance.checked_in` and `attendance.recorded`. `attendance.recorded` is sent only for attended or no-show.
  - Required data groups: identity, registration and event.
  - Only registrations with `identity.verified` are sent.
  - Organizations come only from a single **verified, trusted_api** external ID in the configured namespace. If there are several, no association is made. Company names are a property only, never a match key.
  - No recording-view event is sent, because the platform has no access evidence.
  - Contacts are never subscribed to anything.
- **Operator data-processing confirmation.** Every provider requires **Data processing confirmation** before it can be enabled. Its value is `confirmed`, and nothing preselects it. With it, the operator states that they have a lawful basis to send the data and that the destination's **Required consent** settings reflect their policy.
  - The platform does not infer legal policy for CRM or email tools. Which consent purposes (if any) gate HubSpot, Attio or Customer.io is the operator's choice.
  - Analytics tools keep a built-in rule: PostHog, Amplitude, Google Analytics 4, Mixpanel and RudderStack require Analytics consent on the destination, because the platform's consent model has that purpose. Segment is a routing CDP; its required consents are the operator's choice.
  - Google Analytics 4 also refuses the **profile** data group (no email, phone, name, company or custom fields can enter its snapshot), and needs the **consents** and **attribution** groups.
- **Matching existing people (HubSpot and Attio).** **Match existing people by** is required, with two options:
  - **Email**: merges into an existing contact or person that has the same email. It needs **Email merge confirmation** (`confirmed`), and the registrant must have an email.
    - HubSpot: contacts batch upsert with `idProperty: "email"`.
    - Attio: `PUT /v2/objects/people/records?matching_attribute=email_addresses`.
    - Only registrations with `identity.verified` are ever sent. That flag means the platform bound the registration to the person, either through the browser capability or by creating the person. It is **not proof of mailbox ownership**, because the Person model has no email-verified flag. The operator's confirmation covers this.
  - **Custom ID**: matches on our person ID in a configured unique property or attribute, which is then required.
    - If an existing contact or person already has the same email, the vendor is expected to reject the write. The delivery is then dead-lettered (`contact: http_status: 409` or the vendor's 4xx). Nothing is merged or claimed.
  - If no strategy is set, or email is chosen without the confirmation, nothing is written: the message is dead-lettered with `native_match_strategy_unconfirmed`.
- **Send-time re-checks on every step.** Before each vendor call after the first, delivery:
  - renews the lease;
  - re-reads the destination;
  - re-runs the erasure and consent check (`send_time_block_reason`) against the **current** `required_consents`;
  - re-validates the native configuration.

  Outcomes:
  - A withdrawal or newly required consent cancels the message with `<reason>_mid_sequence` and purges the payload.
  - A disabled destination parks the message as pending (`destination_disabled_mid_sequence`).
  - A changed or invalid configuration dead-letters it. Every step is planned from the configuration read before the first call. The check therefore compares the current `url`, `kind`, `event_types`, `allowed_groups`, `include_private_links`, `field_mapping` and stored `credentials_encrypted` with that snapshot. Any difference stops the run with `native_config_changed_mid_sequence`, so the old plan is never sent. Credentials are compared as stored ciphertext, so saving the same key again also counts as a change. The consent and disabled checks run first and keep the outcomes above.
  - **Writes already completed by earlier steps are not recalled or deleted in the vendor.** Requeueing a dead message reruns the sequence; Customer.io events are not idempotent.
- **Test sends.** The synthetic `webhook.test` fixture (MCP `send_test_delivery`) has no person, so no vendor contract applies. For native destinations it is cancelled with `native_test_send_unsupported`, never reported as delivered. Destinations with required consents cancel it earlier, with `consent_not_granted`.

## Provider contracts implemented

| Provider | Calls | Identity / dedupe |
| --- | --- | --- |
| PostHog | `POST /batch/` with `api_key` in the body, containing `$identify` (with `$set`, only when the profile group is allowed), `$groupidentify` (`$group_type`, `$group_key`, `$group_set`) and the event, with `$groups` | `distinct_id` = platform person ID; group key = verified org ID. Each event has a deterministic `uuid` (uuid5 of delivery ID and step) and a fixed timestamp. Whether PostHog dedupes on it is **not assumed**. Requires Analytics consent on the destination. |
| Customer.io | Track API v2 `POST /api/v2/entity`, up to three calls: identify the org object (`object_type_id`, `object_id`); identify the person with `cio_relationships`; send the person event. Basic auth with site ID and API key. The event `timestamp` is **integer Unix seconds**, per the primary docs at https://docs.customer.io/integrations/api/track/tag/track_v2/entity/ (searched 2026-10-23). It is omitted if `occurredAt` can't be parsed. | `identifiers.id` = person ID. Identify calls are keyed by that ID and assumed idempotent (unverified). A retry after a partial failure **can repeat the event** because no event dedupe ID is sent (format unverified). |
| HubSpot | `POST /crm/v3/objects/contacts/batch/upsert` (`idProperty` = `email` in email mode, per the primary docs at https://developers.hubspot.com/docs/api-reference/crm-contacts-v3/batch/post-crm-v3-objects-contacts-batch-upsert; or the configured unique contact property in custom-ID mode); `POST /crm/v3/objects/companies/batch/upsert`; `POST /crm/v3/objects/{participation object}/batch/upsert` (`idProperty` = registration ID property); `POST /crm/v4/associations/contacts/companies/batch/associate/default`; `POST /crm/v4/associations/contacts/{object}/batch/associate/default` | Upserts are keyed by person, org and registration IDs, so a retry targets the same records (no plain create). A single record per key relies on unique-value properties, which is unverified. Participation is a **custom object** with properties `webinar_event_id`, `webinar_event_title`, `webinar_occurrence_id`, `webinar_starts_at`, `webinar_registration_status`, `webinar_attendance` and `webinar_last_event_type`, plus a default association to contacts that the HubSpot admin must create. |
| Segment | HTTP API `POST /v1/identify` (only when the profile group is allowed; traits `email`, `firstName`, `lastName`, `title`), then `POST /v1/track` (`userId`, `event`, `properties`, `messageId`, `timestamp`). Basic auth with `writeKey:` (empty password). | `userId` = platform person ID. `messageId` is deterministic (uuid5 of delivery ID and step), so retries resend the same ID; Segment-side dedupe is **not assumed**. Segment answers 200 for most malformed requests, so **2xx is not proof of acceptance**. No organization/group call. |
| Amplitude | HTTP V2 `POST /2/httpapi` with `api_key` in the body and one event: `user_id`, `event_type`, `time` (ms), `insert_id`, `event_properties`. No profile data is sent. | `user_id` = platform person ID. `insert_id` is deterministic per delivery. Amplitude documents dedupe on `device_id` + `insert_id`; we send **no `device_id`** (none is captured and none is invented), so dedupe of a retried event is **not assumed**. |
| Google Analytics 4 | Measurement Protocol `POST /mp/collect?measurement_id=G-…&api_secret=…` with `client_id`, `consent` (`ad_user_data`, `ad_personalization`), `timestamp_micros` and one event (`webinar_registered`, `webinar_registration_updated`, `webinar_checked_in`, `webinar_attended`, `webinar_no_show`) whose params are only `webinar_event_type`, `webinar_event_id`, `webinar_occurrence_id`, `webinar_registration_status`, `webinar_attendance` (≤100 chars). | `client_id` is **only** the captured `gaClientId` from the snapshot's attribution (stored only with analytics consent by `attribution.clean_attribution`); the `_ga` cookie form `GA1.n.X.Y` is reduced to `X.Y`. No client ID, or no granted analytics consent in the snapshot: cancelled (`native_ga_client_id_missing` / `native_analytics_consent_missing`), nothing sent. Person ID, registration ID, email, names and custom fields are never sent. Advertising signals are `DENIED` unless the snapshot carries an explicit advertising grant. The URL carries the API secret: previews show `api_secret=[redacted]`, and errors are step + code only. GA returns 2xx for invalid payloads, so **2xx is not proof of acceptance**; events are not deduplicated. |
| Attio | `PUT /v2/objects/companies/records?matching_attribute=…`; `PUT /v2/objects/people/records?matching_attribute=email_addresses` (email mode) or `…?matching_attribute=<custom attribute>` (custom-ID mode), with `email_addresses` and a `company` record reference; `PUT /v2/lists/{list}/entries` (parent person, `entry_values` with the `webinar_*` attributes and `webinar_registration_id`) | Assert by configured unique attributes. Results in **one list entry per person per list**, not one per registration. The workspace must define these attributes. |

Sources for Segment, Amplitude and GA4 (researched 2026-10-25, as supplied to this work; not re-fetched by the implementer, who had no network access):

- Segment HTTP API: https://segment.com/docs/connections/sources/catalog/libraries/server/http-api/ (direct fetch returned 403; the contract comes from official mirror and search evidence).
- Amplitude HTTP V2: https://amplitude.com/docs/apis/analytics/http-v2
- GA4 Measurement Protocol: https://developers.google.com/analytics/devguides/collection/protocol/ga4/reference

## Mixpanel and RudderStack (contracts partly checked against primary docs)

Status: **source implemented; tests mock-only (fake transports, no network); live behaviour unverified.** No sandbox or live account has been used.

Pi fetched the primary docs on 2026-10-25 and passed the extracted facts to this work. The implementer had no network access and did not re-read the pages. The code was compared against those facts. Anything the facts don't cover is still the implementer's assumption.

- Mixpanel Import Events: https://developer.mixpanel.com/reference/import-events
- RudderStack HTTP API: https://www.rudderstack.com/docs/api/http-api/

| Provider | Calls | Identity / dedupe |
| --- | --- | --- |
| Mixpanel | Ingestion `POST /import?strict=1&project_id=<id>` with a JSON array of one event: `{"event", "properties": {"time" (ms), "distinct_id", "$insert_id", "ip": 0, webinar_* }}`. Basic auth with service-account `username:secret`. Hosts: `api.mixpanel.com` (US), `api-eu.mixpanel.com` (EU), `api-in.mixpanel.com` (India). No profile (`/engage`) call and no profile data. | `distinct_id` = platform person ID. `$insert_id` = uuid5 of delivery ID and step (36 chars, hex and hyphens), and `time` comes from `occurredAt`, so a retry resends an identical (event, time, distinct_id, `$insert_id`), which is Mixpanel's documented dedupe tuple. This is **best effort, not exactly-once**. No parseable `occurredAt`: cancelled `native_no_timestamp` (no time is invented). Missing delivery ID: dead `native_delivery_id_missing`. The response body is not read. With `strict=1`, a 400 means at least one record was invalid and goes dead. 200 means all records were ingested; other 2xx also count as delivered. |
| RudderStack | HTTP API on the destination's own data plane: `POST /v1/identify` (only when the profile group is allowed; top-level `traits`), then `POST /v1/track`. Basic auth with the write key as the username and an empty password, as in the documented `curl -u <write_key>:`. `timestamp` is sent only if `occurredAt` parses as ISO-8601; otherwise it is left out. | `userId` = person ID (no `anonymousId`). A deterministic `messageId` is sent per delivery and step, distinct from Segment's. The RudderStack page does not mention `messageId`, so RudderStack-side dedupe is **not assumed** and nothing here is exactly-once. |

**Payload limits (checked before any call).** Each limit is checked against the exact compact UTF-8 body that `NativeContext.call` sends (`_encode`), so multibyte text counts in bytes. "KB" and "MB" are read as decimal, the stricter reading. A breach is dead-lettered with a step and code only, and no request is made. For RudderStack, every step is checked before the first call, so an identify is never sent ahead of a track that would be refused.

- Mixpanel: the one-event body must be under 1,000,000 bytes, otherwise `import: payload_too_large`. At most 254 property keys (under 255), and every property value must be a scalar, otherwise `import: payload_unsupported`. Mixpanel allows nesting to depth 3 and arrays under 255 elements, but this adapter sends none of either, so it doesn't need to enforce them.
- RudderStack: each call body must be at most 32,000 bytes, otherwise `<step>: payload_too_large`. `traits` and `properties` values must be scalars, otherwise `<step>: payload_unsupported`, because the page's JSON nesting limit was not extracted.

**Geolocation privacy.** The official Import Events page describes an `ip` event property and says setting it to `0` skips geolocation. The adapter sends numeric `"ip": 0` to prevent server-IP enrichment. This follows the documented contract; live ingestion remains unverified.

**Verified against the 2026-10-25 facts (request shape only, mock tests):**

- Mixpanel: `/import`; service-account Basic auth plus `project_id`; `strict=1` (400 when any record is invalid, 200 when all are ingested); the required `time` (milliseconds), `distinct_id` and `$insert_id`; the `$insert_id` format (≤36 bytes, alphanumeric or hyphen); the dedupe tuple; the size, key, nesting and array limits above; and `ip=0` to stop request-IP geolocation.
- RudderStack: `/v1/identify` and `/v1/track`; `Content-Type: application/json` (we send `application/json; charset=utf-8`); an ISO-8601 `timestamp`; `userId` (one of `userId`/`anonymousId` is required); top-level identify `traits`; 32 KB per call. Status codes: 200 is success, 401 (bad auth) and 404 (source disabled) go dead, and 429 is retried.

**Unresolved:**

- RudderStack auth. The auth paragraph on the page says the username is blank and the write key is the password, but every curl example uses the key as the username with a blank password. The adapter follows the curl examples (`writeKey:`), matching Segment. Which one is right has not been tested against a real data plane.
- RudderStack's JSON nesting limit (not extracted), whether it handles or dedupes `messageId` (not on the page), its Cloud data plane host formats, and the decimal vs binary reading of "32 KB".
- Mixpanel: where `ip` goes and its type (see above), the residency hosts, the decimal vs binary reading of "1 MB", any per-request (batch) limit, and the status codes other than 400 and 200 (401, 413, 429 are handled generically).
- No sandbox or live verification for either provider.

**Consent.** Both are analytics providers. Enabling requires **Analytics** in Required consent (`validate`/`check_send_policy` → `native_consent_policy_invalid`) and the data-processing confirmation. As defence in depth, a snapshot that carries a `consents` group without an explicit analytics grant is cancelled with `native_analytics_consent_missing`. A missing record never counts as a grant.

**RudderStack host policy (fail closed).** The data plane host is a non-secret setting that the marketer types in, so it is checked at every trust boundary: `validate`, `check_send_policy` (also re-run between steps) and `base_url` at send time.

- It must be a bare lowercase DNS name: no scheme, port, path, userinfo, wildcard, IP literal or trailing dot.
- It must not be one of the static vendor hosts.
- It must be an **exact, non-wildcard entry** in the operator's `WEBHOOK_HOST_ALLOWLIST`. A parent domain (`rudderstack.com`) or wildcard entry (`*.example.com`) does **not** admit sub-domains here. This blocks suffix tricks such as `rudderstack.com.evil.net` and keeps a typed host from receiving the write key unless the operator named that host.
- It must also pass `ssrf.validate_destination_url` (https, no internal names).

**Per-destination host pinning (implemented in source; tests mock-only; live unverified).** `webinars/delivery.py` sets `NATIVE_DESTINATION_HOSTS = True` and `_deliver_native` computes `allowed_hosts(provider, credentials)` once, from the credentials that passed `check_send_policy`, and passes it to both `_native_post` (including its `post_json_pinned` test seam) and `_native_request`. This narrows every provider, static or not, to its single configured host. `_native_post` and `_native_request` still default to `VENDOR_HOSTS` when called without `allowed_hosts` (compatibility for existing test seams); delivery never relies on that default. A delivery module without the constant keeps RudderStack fail-closed: `validate` reports the gap, `check_send_policy` returns `native_transport_unavailable` and `execute` refuses.

**No exactly-once.** The outbox is at-least-once. A timeout after the vendor accepted a request, or a lost lease, leads to a resend. Deterministic `$insert_id`/`messageId` values only give the vendor a chance to dedupe. Nothing here guarantees a single event.

## Brevo (contact + event; source checked against fetched docs, sandbox unverified)

Status: **source implemented; tests mock-only (`tests/test_native_brevo.py`, fake transports, no network); not checked against a Brevo account or sandbox.** Pi fetched the create-contact and create-event references on 2026-10-25 and passed the extracted facts to this work. The implementer did not re-read the pages. The code was compared with those facts; anything they don't cover is still an assumption.

- Create contact: https://developers.brevo.com/reference/create-contact
- Create event: https://developers.brevo.com/reference/create-event

**Settings** (schema-driven: the studio form, preview and validation pick these up from `FIELDS["brevo"]`):

- **API host**: `api.brevo.com` only. The operator must add it to `WEBHOOK_HOST_ALLOWLIST`. Delivery pins every call to that one host (`allowed_hosts`), so the key is never sent anywhere else.
- **API key**: secret, set-only. It is sent only in the `api-key` header (the SDK samples use only this; the docs' generic OAuth alternative is not implemented). It is never rendered, previewed (`api-key: [redacted]`), logged or put in `last_error`.
- **Contacts**: `events` (send events only) or `upsert` (create or update the contact by email, then send the event).
- **Email match confirmation**: required in both modes, because every Brevo call identifies the contact by email. Events attach to whatever contact has that email, and in `upsert` mode the mapped attributes overwrite it. Only `identity.verified` registrations are sent. That flag means the platform bound the registration to the person (browser capability or new person); **it is not proof of mailbox ownership**. Without the confirmation the destination can't be enabled, and a send is dead-lettered `native_match_strategy_unconfirmed` with no call.
- **Contact attribute mapping**: optional explicit pairs such as `firstName=FIRSTNAME,lastName=LASTNAME`. Sources are only `firstName`, `lastName`, `company` and `jobTitle`; no custom fields, phone or other PII can be mapped. Targets must be existing uppercase Brevo attributes. Each source and target may appear once. Identifier, channel and blocklist names (`EMAIL`, `SMS`, `WHATSAPP`, `LANDLINE_NUMBER`, `EXT_ID`, `CONTACT_ID`, `EMAIL_ID`, `EMAILBLACKLISTED`, `SMSBLACKLISTED`) are refused.
- **Data processing confirmation**: required, as for every provider.

**Consent.** Brevo is not treated as analytics. Which consent purposes gate it is the operator's **Required consent** choice. The existing machinery checks it at enqueue, before the first call and again before the event call (`send_time_block_reason` against the current destination). A withdrawal between the two calls stops the event; a contact write that already happened is not recalled.

**Contact marketability.** The platform has no marketing-consent purpose (its purposes are `sms`, `analytics` and `advertising`) and sends no blocklist fields. A contact that Brevo creates, through `upsert` or possibly through the event call (unresolved below), gets **whatever blocklist state Brevo applies by default**. That default may leave the contact reachable by email campaigns. Not adding the contact to a list does **not** guarantee it is out of reach of campaigns, segments or automations in the Brevo account. Whether those contacts may be marketed to is the operator's decision under the data processing confirmation; the platform does not treat it as consented or confirmed.

| Call | Body | Notes |
| --- | --- | --- |
| `POST /v3/contacts` (only in `upsert` mode, when the snapshot has the **profile** group with an email) | `email`, `attributes` (mapped fields only, omitted when empty), `updateEnabled: true` | Never sent: `ext_id` (avoids cross-merging on our person ID), `listIds`/`unlinkListIds` (no subscription or list additions), `emailBlacklisted`/`smsBlacklisted`/`smtpBlacklistSender` (no blocklist resets), `SMS`/`WHATSAPP`, and `forceMerge` (the docs default it to false; we never send it). 201 means contact created. For an updated existing contact, a 204 is expected but the fetched source did not say so explicitly (**unresolved**); any 2xx counts as delivered. |
| `POST /v3/events` | `event_name` = `webinar_` + the lifecycle type with dots replaced by underscores (`webinar_registration_created`, `webinar_registration_updated`, `webinar_attendance_checked_in`, `webinar_attendance_recorded`); `identifiers: {"email_id": …}` only; `event_date` = `occurredAt` (ISO-8601; left out if it doesn't parse, never invented); `event_properties` | 204 means event posted. The email comes from the profile, or else from `identity.email`; with neither, the message is cancelled `native_no_email` before any call. `attendance.recorded` is sent only for attended or no-show; the value is in `registration_attendance`. |

**Event properties** are a self-contained, flat snapshot from an explicit path list (`BREVO_EVENT_PATHS`): `webinar_event_type`, `occurred_at`, `schema_version`, `person_id`, `registration_id`/`_status`/`_attendance`/`_form_version`/`_created_at`/`_updated_at`, `event_id`/`_slug`/`_title`/`_revision`, `occurrence_id`/`_starts_at`/`_ends_at`/`_timezone`/`_status`/`_cancelled_at`/`_recording_published_at`, `delivery_id` and `delivery_created_at`. Only scalar values are sent. Keys match `^[A-Za-z0-9_-]{1,255}$`. The properties are built from the body after `outbox.refilter_for_send`, so a group the destination may not receive is simply missing. `delivery_metadata` is in `outbox.ALWAYS_GROUPS`, and `delivery.serialize_body` re-sets `deliveryId`, so `delivery_id` is present even when `allowed_groups` does not list that group. No enable-time rule is needed for adapters that require it (`_require_delivery_id`). Profile, consent and attribution data are never in event properties. **No URL is ever sent**: no `joinUrl`/`waitingRoomUrl` (even with `include_private_links`), no `managedRoomUrl`, recording, image or redirect URL.

**Limits (checked before the first call).** `event_properties` must be at most 50,000 bytes as compact UTF-8 ("50KB" read as decimal, the stricter reading), otherwise `event: payload_too_large`. A nested contact attribute gives `contact: payload_unsupported`. Both checks run before the contact call, so a contact is never written ahead of an event that would be refused.

**Responses.** 2xx: delivered. 429, 408, 425 and 5xx: retried, using `Retry-After` when present. 3xx: retried, never followed. Other 4xx (400, 401, 403…): dead-lettered. Errors hold only a step name and a code.

**No exactly-once.** Brevo documents no idempotency key for events. Each event carries `delivery_id`, which stays the same across retries, so consumers can dedupe on it. A retry after a timeout, a lost lease or a failed event call **can create a duplicate event**. The contact upsert is keyed by email and is expected to converge (unverified).

**Unresolved:**

- The status code for updating an existing contact (204 expected, not explicit in the fetched source).
- Whether `POST /v3/events` creates a contact for an unknown `email_id`, and how it behaves with blocklisted contacts.
- Whether `event_properties` limits apply to the serialized JSON or something else, and the decimal vs binary reading of "50KB".
- Rate limits and their `Retry-After` behaviour.
- Transport enforcement also requires explicit `allowed_hosts` for `api-key` headers, case-insensitively, alongside `Authorization`. Regression tests reject missing host restrictions before any request is spawned.
- No sandbox or live verification.

## ActiveCampaign (contact sync + lifecycle tags; source checked against official references, account unverified)

Status: **source implemented in `webinars/native_providers/activecampaign.py`; tests mock-only (`tests/test_native_activecampaign.py`, fake transports, no network); not checked against an ActiveCampaign account.** The implementer fetched these official pages with WebFetch on 2026-10-27 and compared the code with them:

- Sync a contact's data (`POST /contact/sync`): https://developers.activecampaign.com/reference/sync-a-contacts-data
- Add a tag to a contact (`POST /contactTags`): https://developers.activecampaign.com/reference/create-contact-tag
- Authentication (`Api-Token` header, `https://<account>.api-us1.com/api/3/`): https://developers.activecampaign.com/reference/authentication
- Rate limits (5 requests/second per account, 429 with `Retry-After`): https://developers.activecampaign.com/reference/rate-limits

**Settings** (schema-driven from the module's `FIELDS`; the studio form, preview and validation need no provider-specific code):

- **API host**: the account's own host, e.g. `acme.api-us1.com`, typed by the marketer. Checked by `native_integrations.destination_host_problem` at every trust boundary (`validate`, `check_send_policy` between steps, `host_of` at send time): a bare lowercase DNS name, not a static vendor host, an **exact, non-wildcard** `WEBHOOK_HOST_ALLOWLIST` entry (no parent domain or wildcard admits it), and `ssrf.validate_destination_url`. Delivery pins every call to that one host (`allowed_hosts`), so the key is never sent anywhere else, even to another allowlisted host. VERIFIED (host pattern): the authentication page's example host is `123456demo.api-us1.com`; the page does not state the pattern as a rule, so any other regional suffix is accepted as long as the operator allowlists it.
- **API key**: secret, set-only, sent only in the `Api-Token` header (VERIFIED: authentication page). `api-token` is in `SECRET_HEADERS` (previews show `Api-Token: [redacted]`), and `ssrf.CREDENTIAL_HEADERS` now includes `api-token` and `x-api-token`, so a request carrying either is refused without an explicit `allowed_hosts` (regression-tested).
- **Email match confirmation**: required. Every call creates or updates the contact that has the registrant's email; names and mapped fields overwrite it. Only `identity.verified` registrations are sent; that is not proof of mailbox ownership. Without it: `native_match_strategy_unconfirmed`, no call.
- **Custom field mapping**: optional `source=<numeric field id>` pairs. Sources are only `firstName`, `lastName`, `company`, `jobTitle`, `webinar_event_title`, `webinar_starts_at`, `webinar_registration_status`, `webinar_attendance` and `webinar_event_type`. Never `phone` or anything else. Each source and each field id may appear once.
- **Tag IDs** (`tag_registered`, `tag_attended`, `tag_no_show`): optional numeric ids of **existing** tags (VERIFIED: `contactTag.tag` is the tag id "not a tag name"; a name returns 400 "No tag id provided"). `registration.created` → registered; `attendance.checked_in` and `attendance.recorded`=attended → attended; `attendance.recorded`=no_show → no-show; `registration.updated` applies no tag.
- **Data processing confirmation**: required, as for every provider.

| Call | Body | Notes |
| --- | --- | --- |
| `POST /api/3/contact/sync` | `{"contact": {"email", "firstName", "lastName", "fieldValues": [{"field": "<id>", "value": …}]}}` (names only when the profile group is present; `fieldValues` only for mapped, non-empty scalar values) | VERIFIED: creates (201) or updates (200) one contact by email; "duplicate-email errors do not occur"; omitted fields are not changed; `fieldValues[].field` is the custom field id. Never sent: `phone`, `lists`, `tags`, `status`, `form`, `account`/`orgid`/`organization`, `ip4`, `whatsapp_id`, `source`, `useDefaults`, `allowNullEmail`, `open_tracking_optout` (VERIFIED as accepted keys we deliberately omit). The response `contact.id` (a string of digits) is required for the tag call; without it the message is retried (`contact: response_missing_id`). |
| `POST /api/3/contactTags` (only when the lifecycle has a tag id) | `{"contactTag": {"contact": <id>, "tag": <id>}}` as integers | VERIFIED: 201 applied; **200 when the tag is already applied** ("repeating the request is safe"), so a retry after a lost lease or timeout never duplicates the tag. The documented 400 "contactTag object already exists" only occurs for two simultaneous requests and is dead-lettered like any other 4xx (no special case). 404 "Tag not found" (deleted or template tag) is dead. |

**Identity / dedupe.** Both calls key on the email (sync) or the contact id the sync returned (tag); no platform id is written to ActiveCampaign. A retry re-sends the identical sync and tag requests, which converge. No `_require_delivery_id`: nothing here derives from the delivery id.

**Limits.** One contact per sync request (VERIFIED). Rate limit 5 requests/second per account, 429 with `Retry-After` (VERIFIED), which the generic status mapping honours. No payload byte limit is documented; values must be scalars (`contact: payload_unsupported` before any call).

**Consent / marketability.** Not an analytics provider: which consent purposes gate it is the operator's **Required consent** choice, re-checked before the tag call. The contact is **never added to a list or automation and its status is never changed**: a synced contact has no list subscription from this adapter. UNVERIFIED and the operator's responsibility: whether existing automations in the account trigger on contact creation, field changes or tag application (tags are a common automation trigger), and whether an account's default settings make a list-less contact reachable by campaigns. The data processing confirmation covers this; the platform does not treat the contact as consented to marketing.

**Not implemented.** Site/event tracking through `trackcmp.net` (a different host, form-encoded, actid/key credentials), `contactLists` (subscriptions), `contactAutomations`, account/organization association and `POST /contacts` (plain create).

**Responses.** 2xx: delivered. 408, 425, 429 (with `Retry-After`), 5xx and 3xx: retried. Other 4xx (400, 401, 403, 404, 422): dead-lettered. Errors hold only a step name and a code.

**Unresolved:**

- Whether the `fieldValues[].value` type for date custom fields accepts the ISO-8601 `webinar_starts_at` string (the reference lists string/number/boolean values but no date format).
- The exact type of `contact.id` in the sync response (string in the reference; digits are required here) and of `contactTag.contact`/`tag` in responses (the page disagrees with itself; the adapter does not read them).
- Regional API host suffixes other than `api-us1.com`.
- No sandbox or live verification.

## Mailchimp (audience member + tags + member event; source checked against official references, account unverified)

Status: **source implemented in `webinars/native_providers/mailchimp.py`; tests mock-only (`tests/test_native_mailchimp.py`, fake transports, no network); not checked against a Mailchimp account.** The implementer fetched these official pages with WebFetch on 2026-10-27 and compared the code with them:

- Add or update list member (`PUT /lists/{list_id}/members/{subscriber_hash}`): https://mailchimp.com/developer/marketing/api/list-members/add-or-update-list-member/
- Add event (`POST …/members/{subscriber_hash}/events`): https://mailchimp.com/developer/marketing/api/list-member-events/add-event/
- Add or remove member tags (`POST …/members/{subscriber_hash}/tags`): https://mailchimp.com/developer/marketing/api/list-member-tags/add-or-remove-member-tags/
- Quick start (`curl --user "anystring:${apikey}"`): https://mailchimp.com/developer/marketing/guides/quick-start/
- Errors (status codes, 429 = 10 concurrent requests per user): https://mailchimp.com/developer/marketing/docs/errors/
- Contact statuses (non-subscribed/transactional "can't receive marketing emails"; pending contacts "must confirm their opt-in before being added"): https://mailchimp.com/help/about-your-contacts/
- Official Python client README (`server` prefix such as `us19`; "All URIs are relative to https://server.api.mailchimp.com/3.0"): https://github.com/mailchimp/mailchimp-marketing-python

**Settings** (schema-driven from the module's `FIELDS`):

- **Data center**: `^[a-z]{2,3}[0-9]{1,3}$`, e.g. `us19`. The host is `<data center>.api.mailchimp.com` (VERIFIED against the official client README; the current API docs show only the root `https://api.mailchimp.com/3.0/`, so whether the un-prefixed host also accepts the key is UNVERIFIED and not offered). It must be an **exact, non-wildcard** `WEBHOOK_HOST_ALLOWLIST` entry and pass `ssrf.validate_destination_url` (`destination_host_problem`); otherwise there is no host, nothing validates and nothing is sent. `validate_extra` also requires the data center to equal the stored API key's suffix after its last `-`, compared without rendering the key (UNVERIFIED: the fetched pages no longer state that the key carries the data center suffix; the check is conservative and fails closed). Delivery pins every call to that one host.
- **API key**: secret, set-only, sent only as HTTP Basic `anystring:<key>` (VERIFIED: quick-start curl `--user "anystring:${apikey}"`; the reference pages describe a Bearer alternative that is not implemented). Previews show `Authorization: [redacted]`.
- **Audience ID**: the list the member is written to.
- **Email match confirmation**: required. Every call is keyed by the MD5 of the lower-cased email (VERIFIED: `subscriber_hash` "is the MD5 hash of the lowercase version of the list member's email address"), so merge fields overwrite any existing member with that email. Only `identity.verified` registrations are sent; not proof of mailbox ownership.
- **Status for new members**: `transactional` (default) or `pending`. **Never `subscribed`** (not a choice; refused by `validate` and `run`). Pending requires the **Opt-in email confirmation** acknowledgement, because Mailchimp then sends the registrant its opt-in confirmation (UNVERIFIED for API-created members: the double opt-in help page says double opt-in "can only be enabled for Mailchimp signup forms"; a pending member who never confirms is deleted after 60 days per the statuses page).
- **Merge field mapping**: optional `firstName=FNAME,lastName=LNAME,…` pairs. Sources only `firstName`, `lastName`, `company`, `jobTitle`; targets are uppercase merge tags; `EMAIL` is refused; each source and target once. Merge tags must already exist in the audience (UNVERIFIED whether unknown tags are ignored or rejected).
- **Tags** (`tag_registered`, `tag_attended`, `tag_no_show`): optional tag names (VERIFIED: a tag "that does not exist … set as `active`" is created). Lifecycle mapping as for ActiveCampaign.
- **Data processing confirmation**: required.

| Call | Body | Notes |
| --- | --- | --- |
| `PUT /3.0/lists/{audience}/members/{md5}` | `email_address`, `status_if_new`, `merge_fields` (mapped, non-empty, only when the profile group is present) | VERIFIED: "adds a list member or updates an existing one"; `status_if_new` is required only when the address is not yet on the list, with `transactional` and `pending` among its values; `merge_fields` keys are merge tags such as `FNAME`. **`status` is never sent**, so an existing member's status is never changed (VERIFIED that `status` is the field that would change it). Never sent: `interests`, `tags`, `marketing_permissions`, `ip_signup`, `ip_opt`, `timestamp_signup`, `timestamp_opt`, `vip`, `language`, `email_type`, `location`. 200 (the only documented status) returns `id` = the hash; a missing `id` is retried (`member: response_missing_id`) before any tag or event call. |
| `POST …/members/{md5}/tags` (only when the lifecycle has a tag name) | `{"tags": [{"name": …, "status": "active"}]}` | VERIFIED: `active` adds, 204 empty response. Never `inactive` (no tag removal), never `is_syncing`. |
| `POST …/members/{md5}/events` | `name` (`webinar_registered`, `webinar_registration_updated`, `webinar_checked_in`, `webinar_attended`, `webinar_no_show`: 2–30 chars, VERIFIED), `properties` (string values only, VERIFIED type "map from string to string"), `occurred_at` (ISO-8601, VERIFIED; omitted when `occurredAt` doesn't parse) | 204. Properties are the lifecycle facts only: `webinar_event_type`, `_event_id`, `_event_title`, `_occurrence_id`, `_starts_at`, `_registration_id`, `_registration_status`, `_attendance`, `_schema_version`, `_delivery_id`. Never profile, consent, attribution data or any URL. Sent last so that a failure earlier in the sequence never leaves a duplicate event behind. |

**Identity / dedupe.** Every call keys on the email hash; no platform id is written. The member upsert and the tag call converge on retry. The event is **not idempotent** (the reference documents no idempotency key): a retry after a timeout, a lost lease or a failed event call can create a duplicate event. `webinar_delivery_id` stays the same across retries so consumers can dedupe (`_require_delivery_id`: missing delivery id is dead `native_delivery_id_missing`).

**Limits.** 429 means more than 10 simultaneously processing requests per user (VERIFIED) and is retried with `Retry-After` when present. Event `name` length is enforced (VERIFIED 2–30). No documented size limits for `properties` or `merge_fields`; nested merge values are refused before any call (`member: payload_unsupported`).

**Consent / marketability.** Not an analytics provider; consent purposes are the operator's **Required consent** choice, re-checked before the tag and event calls. A `transactional` member is a non-subscribed contact who "can't receive marketing emails, marketing text messages" (VERIFIED, statuses page), so the default never makes anyone marketable. A `pending` member becomes `subscribed` only if they click Mailchimp's confirmation; the operator's opt-in acknowledgement records that they accept Mailchimp sending that email. An existing `subscribed` member stays subscribed (we never send `status`); this adapter neither grants nor withdraws marketing permission. UNVERIFIED and the operator's responsibility: whether audience automations trigger on the tags or events this adapter writes.

**Responses.** 2xx: delivered. 408, 425, 429, 5xx and 3xx: retried. Other 4xx (400 "Invalid Resource", 401 "API Key Invalid", 403, 404 "Resource Not Found", 405, 414, 422): dead-lettered. Errors hold only a step name and a code; the `application/problem+json` body is never read.

**Unresolved:**

- Whether the data-center host form (`<dc>.api.mailchimp.com`) is still required, and whether the API key suffix is the authoritative data center (the current docs show only the root URL; the check follows the official client README and fails closed).
- Whether an API-created `pending` member receives the opt-in confirmation email.
- Whether unknown merge tags are ignored or rejected, and whether `skip_merge_validation` would be needed for audiences with required merge fields (not sent).
- Idempotency of re-adding an already active tag (not stated; assumed a no-op).
- Any `properties` size or key-format limits for events.
- No sandbox or live verification.

## Deferred providers

- Salesforce, Pipedrive, further marketing-automation and transactional tools are a later wave.

## Marketer setup

1. Ask the operator to allowlist the vendor host.
2. Create the integration, choose the provider and region, and save.
3. Fill in the settings: the match strategy and its confirmation (HubSpot and Attio), the stable ID properties or attributes, and the organization namespace and target (or leave both empty).
4. Enter secrets in the provider credentials form.
5. Tick the supported events and the identity, registration and event groups. Choose **Required consent** according to your policy; PostHog, Amplitude, GA4, Mixpanel and RudderStack require Analytics. Then set the data processing confirmation.
6. Review the redacted preview, then enable. Enabling re-validates.

## Gaps and assumptions to verify (official docs needed)

Evidence so far:

- Pi read the primary docs on 2026-10-23 for two facts: the Customer.io Track v2 entity `timestamp` is integer seconds, and HubSpot contacts batch upsert supports `idProperty: "email"`.
- Pi fetched the Mixpanel Import Events and RudderStack HTTP API pages on 2026-10-25. The extracted facts and what they leave open are listed in the Mixpanel and RudderStack section.
- Attio `matching_attribute=email_addresses` comes from the review instructions. This worker had no web access, so it is not independently re-read here.
- Everything else below is still unverified.

These need checking in the current official docs:

- **PostHog:** the capture/batch API reference (endpoint, `api_key` in the body, `uuid` and `timestamp` fields, response codes and rate limits); the group analytics docs (`$groupidentify`, group-type limits and plan availability); regional hosts.
- **HubSpot:**
  - CRM objects batch upsert (`idProperty` must be a unique-value property) and its response shape.
  - The v4 associations batch default-associate endpoint.
  - Custom object schemas and their associations.
  - Private app scopes, rate limits, and 409 behaviour on email conflicts.
  - EU data-hosting API host.
- **Customer.io:**
  - Track API v2 `/api/v2/entity` (actions `identify` and `event`, `object` type, `cio_relationships`).
  - Regional hosts and Basic auth.
  - Event dedupe `id` (ULID?) support.
  - Whether objects require a plan.
  - Rate limits.
- **Attio:**
  - Assert-record `PUT` with `matching_attribute`.
  - Record-reference value format.
  - The list-entry assert endpoint and its matching semantics.
  - Token scopes and rate limits.
- **Mixpanel** (request shape checked against the 2026-10-25 facts; see the Mixpanel and RudderStack section):
  - Residency hosts.
  - Confirm numeric event-property `ip=0` behavior in the sandbox.
  - Status codes other than 400 and 200 (401, 413, 429), and any per-request batch limit.
- **RudderStack** (request shape checked against the 2026-10-25 facts):
  - Which auth form is right (the page contradicts itself; we follow the curl examples).
  - The JSON nesting limit.
  - `messageId` handling and dedupe.
  - The Cloud data plane host formats.

Other known gaps:

- **Idempotency is not guaranteed for every call.**
  - HubSpot and Attio record writes are upserts or asserts keyed by our IDs. Tests prove a retry resends the same key and never a plain create. That this means no duplicate *in the vendor* depends on the configured property or attribute being unique, which must be verified in the workspace.
  - PostHog events carry stable uuids, but vendor dedupe on them is unverified.
  - Customer.io events have no dedupe ID, and a retry after a partial failure resends the event (covered by a test).
  - The HubSpot association calls and the Attio list-entry assert are assumed idempotent but unverified.
- There's no "send test to a chosen record" button. That would need a new URL route, and this work doesn't own that file.
- Deletion and erasure propagation to vendors is not implemented.
- GA4 snapshot minimisation is partial. The required **identity** group still carries `identity.email` in the stored outbox snapshot; the adapter never sends it. Removing it from the snapshot needs an outbox change: an identity-without-email group, or provider-aware filtering in `outbox.refilter_for_send`.
- There are no owner-approved sandbox checks yet.
