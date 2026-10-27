# Native integrations

Status: **fifteen adapters (PostHog, HubSpot, Customer.io, Attio, Segment, Amplitude, Google Analytics 4, Mixpanel, RudderStack, Brevo, ActiveCampaign, Mailchimp, Salesforce, Pipedrive, beehiiv) are implemented in source on `ssrf.request_json_bounded`; their tests are mock-only, and none is production-ready or live-verified.** ActiveCampaign, Mailchimp, Salesforce, Pipedrive and beehiiv live in self-contained modules under `webinars/native_providers/` (registered through `PROVIDER_MODULES`); their request shapes were compared with official reference pages fetched 2026-10-27 (see their sections). Mixpanel and RudderStack request shapes were compared with primary-doc facts fetched 2026-10-25; some contract points are still open (see below). That function is a bounded POST/PUT transport: response body up to 64 KB, `Retry-After` parsed, and every native request pinned to the destination's own configured host. If the function were ever missing, HubSpot and Attio fail validation, and their deliveries are dead-lettered with `native_transport_unavailable` without any network call.

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
    - Methods are GET (bodiless lookups: Pipedrive search and activity dedupe), POST, PUT or PATCH (Pipedrive v2 updates) only. Method and headers are adapter constants, never user input. Requests carrying a credential header (`Authorization`, `api-key`, `x-api-token`: `ssrf.CREDENTIAL_HEADERS`) are refused without an explicit `allowed_hosts`.
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
  - Salesforce: the destination's own My Domain host (`acme.my.salesforce.com`, or a sandbox host such as `acme--dev.sandbox.my.salesforce.com`), which must be an exact operator allowlist entry, like the RudderStack data plane. The token request goes there too.
  - Pipedrive: `api.pipedrive.com` only (the `{company}.pipedrive.com` form shown in the authentication guide is not used)
  - beehiiv: `api.beehiiv.com` only

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
  - Contacts are never subscribed to anything, with one deliberate exception: **beehiiv**, whose only model for a person is the newsletter subscription itself. That adapter subscribes registrants, says so in its settings, and needs an extra **Subscription confirmation** before it can be enabled (see its section).
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
- Transport enforcement also requires explicit `allowed_hosts` for `api-key` and `x-api-token` headers, case-insensitively, alongside `Authorization` (`ssrf.CREDENTIAL_HEADERS`). Regression tests reject missing host restrictions before any request is spawned.
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

## Salesforce (Lead or Contact, Account and participation in one Composite request; checked against the REST API Developer Guide, no org used)

Status: **source implemented (`webinars/native_providers/salesforce.py`); tests mock-only (`tests/test_native_salesforce.py`, fake transports, no network); not checked against a Salesforce org or sandbox.** The implementer read these pages on 2026-10-27 in a headless browser (the legacy `developer.salesforce.com/docs/atlas.en-us.api_rest.meta/...` URLs and the site's content API answer 403 to plain fetches):

- Composite resource: https://developer.salesforce.com/docs/platform/api-rest/guide/resources-composite-composite-post.html
- Composite request body: https://developer.salesforce.com/docs/platform/api-rest/guide/requests-composite.html
- Composite subrequest result: https://developer.salesforce.com/docs/platform/api-rest/guide/resources-composite-subrequest-result.html
- Upsert by external ID: https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-upsert-patch.html and https://developer.salesforce.com/docs/platform/api-rest/guide/dome-upsert.html
- OAuth overview: https://developer.salesforce.com/docs/platform/api-rest/guide/intro-oauth-and-connected-apps.html (creating connected apps is restricted from Spring '26; External Client Apps are recommended)
- **Not readable:** the client credentials flow article https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_client_credentials_flow.htm did not render headless ("CSS Error"), so the token request and response shape below are **UNVERIFIED**.

**Settings** (schema-driven from `FIELDS["salesforce"]`):

- **Host**: fixed choice "Operator-allowlisted My Domain host" plus **My Domain host** (`instance_host`), a bare host name typed by the marketer. It is checked exactly like the RudderStack data plane (`destination_host_problem`): bare lowercase DNS name, not a static vendor host, an **exact, non-wildcard** `WEBHOOK_HOST_ALLOWLIST` entry, and `validate_destination_url` must pass. `host_of` returns it, so `allowed_hosts` pins the token request and the write to that one host; otherwise the host set is empty and every call is refused. A sandbox is just another host value.
- **Consumer key** and **Consumer secret**: secrets, set-only, never rendered, previewed, logged or put in `last_error`. Sent only in the token request body to the configured host.
- **Match existing people by**: only **custom ID**. Salesforce upserts only on External ID fields and the standard `Email` field cannot be one, so there is no email merge. An existing record with the same email but no person ID is not merged; a duplicate rule may reject the write (dead-lettered, nothing merged).
- **Person object**: `Lead` or `Contact`. **Person External ID field** (`Webinar_Person_Id__c`): custom field (`__c`) holding our person ID.
- **Account External ID field** (optional, Contact mode only; a Lead configuration with it fails validation) with the **Organization ID namespace**, both or neither.
- **Participation object** (`Webinar_Participation__c`), **Participation External ID field** (our registration ID), **Person lookup relationship** (`Contact__r` or `Lead__r`), and **Participation field mapping**: comma pairs `source=Field__c` whose sources are only the keys of `facts["properties"]` (`webinar_event_type`, `webinar_event_id`, `webinar_event_title`, `webinar_occurrence_id`, `webinar_starts_at`, `webinar_registration_id`, `webinar_registration_status`, `webinar_attendance`, `webinar_schema_version`, `webinar_delivery_id`); targets are custom fields, each source and target once, never the External ID field or the lookup. Validated strictly like Brevo's mapping; an invalid mapping is `native_config_invalid` with no call.
- **Data processing confirmation**: required. The **profile** data group is also required, because Lead and Contact need a last name (and Lead a company) on create.

| Call | Body | Notes |
| --- | --- | --- |
| `POST /services/oauth2/token` (`Content-Type: application/x-www-form-urlencoded`) | `grant_type=client_credentials&client_id=…&client_secret=…` | **UNVERIFIED** (article not readable). Skipped when a token is cached: tokens live only in process memory (`hosting.base.cached_token`/`store_token`, keyed by host and credentials, 15 min minus safety margin, never the database). The response's `access_token` is required (else retry `token: response_missing_token`) and its `instance_url` must be `https://<configured host>` (else dead `token: instance_mismatch`, nothing cached): credentials never follow a vendor-supplied URL. A composite 401 on a cached token forgets it and retries (`composite: token_rejected`); a 401 on a fresh token is dead. |
| `POST /services/data/v62.0/composite` (`Authorization: Bearer …`) | `{"allOrNone": true, "collateSubrequests": false, "compositeRequest": [account?, person, participation]}`; each subrequest is `{"method": "PATCH", "url": "/services/data/v62.0/sobjects/<Object>/<ExtIdField>/<value>", "referenceId": …, "body": …}` | Verified on the pages above: URI and method; `allOrNone` rolls the whole request back and the top-level status stays 200; up to 25 subrequests; subrequest fields and allowed methods (`PATCH` included); `referenceId` letters, digits and underscores; the result shape (`compositeResponse[].httpStatusCode/referenceId/body`); the upsert URL pattern; 201 on create, 200 on update (API 46+), 300 when several records match; `Relationship__r: {ExtId__c: value}` relates a record by external ID. |

Subrequest bodies: `account` (Contact mode with a verified org ID): `{<AccountExtId>: org, "Name": company name or the org ID}` (Account Name is required on create; the verified ID stands in when no company name is known). `person`: `{<PersonExtId>: person id, "Email", "FirstName", "LastName", "Title"}` plus `"Company"` for Lead and `"Account": {<AccountExtId>: org}` for a Contact with an account (the standard `Account` relationship by external ID is **assumed** from the custom `__r` example). `participation`: `{<RegExtId>: registration id, <mapped fields…>, <Relationship__r>: {<PersonExtId>: person id}}`. Path values are URL-quoted and must be printable ASCII without spaces, otherwise `<ref>: payload_unsupported`.

**Identity / dedupe.** Every write is an upsert by our stable IDs, so a retried sequence targets the same records and no plain create (`POST /sobjects/...`) is ever sent (tested). That this gives a single record per key depends on the External ID fields being **Unique** in the org (unverified; a non-unique External ID also needs "View All Data", per the upsert page). Registrations without a last name, or Leads without a company, are cancelled (`native_contact_fields_missing` / `native_lead_fields_missing`) rather than written with placeholders. Company names are only ever `Name`/`Company` values, never match keys.

**Responses.** Top-level 2xx with every subrequest 2xx: delivered. A subrequest outside 2xx: the step is its `referenceId`; among several failures the one whose body is not `PROCESSING_HALTED` (the fixed error code of siblings rolled back by `allOrNone`) is reported. 408/425/429/5xx: retry; 300 (several records match) and other 4xx: dead. Top-level 408/425/429/5xx/3xx: retry; other 4xx: dead. Only the error code enum is read from bodies; messages never reach `last_error`. A response without a parseable `compositeResponse` of the expected length is retried (`composite: response_unparsable`).

**Limits.** At most 3 subrequests are built (25 allowed). The composite body (compact UTF-8) must be at most 200,000 bytes (`composite: payload_too_large`, our own bound, unverified against any Salesforce limit) and mapped values must be scalars (`participation: payload_unsupported`); both are checked before the token call. Two calls per message (token, composite), within `MAX_CALLS`.

**Consent.** Salesforce is not analytics: which purposes gate it is the operator's **Required consent** choice, re-checked before the composite call (a withdrawal between the token and the write stops it).

**Unresolved:**

- The client credentials token request (path, parameters, `instance_url`, whether `expires_in` is present) and session lifetime; the 15-minute cache TTL is an assumption.
- API version `v62.0` (the docs' examples use `v68.0`; any version from 46.0 returns 200 with a body on update).
- The standard `Account` relationship-by-external-ID syntax on Contact; any composite byte limit; duplicate-rule behaviour; required permissions and field-level security for the External ID fields.
- No sandbox or live verification.

## Pipedrive (person, organization and activity; checked against the API v2 OpenAPI file, no account used)

Status: **source implemented (`webinars/native_providers/pipedrive.py`); tests mock-only (`tests/test_native_pipedrive.py`, fake transports, no network); not checked against a Pipedrive account.** On 2026-10-27 the implementer downloaded the official OpenAPI files https://developers.pipedrive.com/docs/api/v1/openapi-v2.yaml (servers: `https://api.pipedrive.com/api/v2`) and https://developers.pipedrive.com/docs/api/v1/openapi.yaml, and read https://pipedrive.readme.io/docs/core-api-concepts-authentication and https://pipedrive.readme.io/docs/core-api-concepts-custom-fields. The v1 file no longer lists persons, organizations or activities, so the adapter uses **v2** (`PATCH` for updates, which is why the native transport now admits PATCH).

**Settings** (schema-driven from `FIELDS["pipedrive"]`): region (`api.pipedrive.com` only); **API token** (secret, `x-api-token` header only, verified in the authentication guide and the OpenAPI security scheme; previews show `x-api-token: [redacted]`); **Match existing people by** (`email` with the email merge confirmation, or `custom_id`); **Person ID custom field key** and **Organization ID custom field key** (40-character hex keys, verified format; required for custom-ID matching and organizations respectively); **Organization ID namespace** (both or neither with the org field); **Activity type** (key, default `meeting`; must exist in the account, unverified); **Data processing confirmation**.

| Call | Request | Notes |
| --- | --- | --- |
| `GET /api/v2/organizations/search?term=<org id>&fields=custom_fields&exact_match=true&limit=1` (only with an org field and a single verified org ID) | — | Verified parameters and `data.items[].item.id`. Found: the id is reused and the organization is **not** renamed. Not found: `POST /api/v2/organizations` `{"name": company name or org id, "custom_fields": {<key>: org id}}` (`name` required, verified). |
| `GET /api/v2/persons/search?term=<person id or email>&fields=custom_fields|email&exact_match=true&limit=1` | — | Verified parameters (`exact_match` is not case sensitive; only `varchar`/`text`/`varchar_auto`/… custom fields are searchable). Terms must be a plain id (`[A-Za-z0-9_-]`) or a plain email, otherwise `person_search: payload_unsupported` before any call. The first hit is taken; that a `custom_fields` match is on the configured field is **assumed**. |
| `PATCH /api/v2/persons/{id}` (found) or `POST /api/v2/persons` (not found) | `name` (first and last name, else the email; a registration with neither is cancelled `native_person_name_missing`), `custom_fields: {<person key>: person id}` when configured, `org_id`; `emails: [{"value", "primary": true, "label": "work"}]` **only on create** (PATCH replaces the whole list and would drop other addresses) | Verified fields and that `name` is required on create. Never sent: `marketing_status`, `job_title`/`notes`/`postal_address`/`im`/`birthday` (contact-sync fields that answer 403 otherwise), phones, labels, deals, leads, URLs. A 2xx create without `data.id`: retry `person: response_missing_id`. |
| `GET /api/v2/activities?person_id=<id>&limit=500&sort_by=add_time&sort_direction=desc` | — | Verified parameters (`person_id` filters by primary participant; 500 max). Items whose `note` contains `webinar-registration:<registration id>:<event type>` mean the activity exists: nothing more is sent. **Only this first page is scanned**; older activities are not. |
| `POST /api/v2/activities` | `subject` ("Webinar registered: <title>" etc.), `type`, `due_date` `YYYY-MM-DD` and `due_time` `HH:MM:SS` in UTC from the occurrence start (omitted when unparseable), `done` (true for check-in and attendance outcomes), `note` = the marker, `org_id`, `participants: [{"person_id", "primary": true}]` | Verified fields (the spec example uses `'15:00:00'`; the time zone Pipedrive applies to `due_time` is unverified). |

**Identity / dedupe.** Search-then-write makes retries converge: a person or organization created by a failed earlier attempt is found and updated, never re-created, and an activity already carrying the marker is not duplicated (tested for a failure after each step). Email mode needs the confirmation and a profile email (`native_no_email` otherwise); only `identity.verified` registrations are sent. Organizations come only from a single verified `trusted_api` external ID; company names are only `name` values.

**Limits.** Each write body (compact UTF-8) is at most 100,000 bytes (`<step>: payload_too_large`, our own bound) and profile values must be scalars (`person: payload_unsupported`), checked before the first call. At most six calls per message (organization search and create, person search and write, activity list and create), exactly `MAX_CALLS`.

**Consent.** Pipedrive is not analytics: the operator's **Required consent** choice gates it, re-checked before every call after the first.

**Responses.** 2xx: delivered (a 2xx update needs no body). 408/425/429/5xx/3xx: retried; other 4xx: dead. Errors are `<step>: <code>` only (`organization_search`, `organization`, `person_search`, `person`, `activities`, `activity`).

**Unresolved:**

- Rate limits (token budget per request) and `Retry-After` behaviour.
- The time zone applied to `due_time`; whether `exact_match` on `custom_fields` can match another field with the same value.
- Whether the first-page scan is enough for very active persons (more than 500 activities since the registration).
- Any request-size limit; the default activity type keys.
- No sandbox or live verification.

## beehiiv (newsletter subscription + lifecycle tags; checked against the official API reference, no publication used)

Status: **source implemented in `webinars/native_providers/beehiiv.py`; tests mock-only (`tests/test_native_beehiiv.py`, fake transports, socket connects refused); not checked against a beehiiv publication.** The implementer fetched these official pages with WebFetch on 2026-10-27 and compared the code with them:

- Create Subscription (`POST /v2/publications/{publicationId}/subscriptions`): https://developers.beehiiv.com/api-reference/subscriptions/create (readable as https://developers.beehiiv.com/api-reference/subscriptions/create.md)
- Add subscription tag (`POST …/subscriptions/{subscriptionId}/tags`): https://developers.beehiiv.com/api-reference/subscription-tags/create.md (the `api-reference/subscription-tags` index URL answers "Page Not Found")
- Update subscription by email (`PUT …/subscriptions/by_email/{email}`; **not used**, read only to document the alternative): https://developers.beehiiv.com/api-reference/subscriptions/update-by-email.md (the `subscriptions/update` URL answers "Page Not Found"; `subscriptions/put` is the by-id variant, not read)
- Get subscription by email (read for the `pub_…` id example and the `Authorization: Bearer <token>` form): https://developers.beehiiv.com/api-reference/subscriptions/get-by-email.md
- Rate limiting: https://developers.beehiiv.com/welcome/rate-limiting.md
- **Not readable:** https://developers.beehiiv.com/welcome/create-an-api-key (and its `.md` form) answered 403, and https://developers.beehiiv.com/welcome/getting-started.md carries no host, header or id-format detail. Everything about API-key creation and scoping below is therefore **UNVERIFIED**; the header form comes from the endpoint pages.

**This adapter subscribes people.** beehiiv has no non-subscribed contact: the subscription *is* the record. Every delivery creates (or, assumed, converges on) a **free** subscription to the configured publication. The settings say so, and the destination cannot be enabled without the **Subscription confirmation** below. Which consent purposes gate it is the operator's **Required consent** choice (the platform has no marketing-consent purpose); the data processing confirmation covers the lawful basis.

**Settings** (schema-driven from the module's `FIELDS`):

- **API host**: `api.beehiiv.com` only (VERIFIED: every endpoint page uses `https://api.beehiiv.com/v2/…`). The operator must add it to `WEBHOOK_HOST_ALLOWLIST`; delivery pins every call to that one host (`allowed_hosts`), so the key is never sent anywhere else.
- **API key**: secret, set-only, sent only as `Authorization: Bearer <key>` (VERIFIED on the endpoint pages: "Bearer authentication of the form `Bearer <token>`"). Previews show `Authorization: [redacted]`; it is never rendered, logged or put in `last_error`. Whether a key is workspace- or publication-scoped and how it is created: UNVERIFIED (page 403).
- **Publication ID**: `pub_` + UUID (`^pub_[0-9a-fA-F]{8}-…-[0-9a-fA-F]{12}$`; VERIFIED example `pub_00000000-0000-0000-0000-000000000000`, described only as "the prefixed ID of the publication"). It becomes a URL path segment, so nothing else is accepted.
- **Subscription confirmation** (`subscribe_ack`, required `confirmed`): "Registrants are subscribed to this publication; this is the only thing beehiiv can do with them. Confirm you have a lawful basis to subscribe them and that Required consent reflects your policy." Without it: `validate` refuses to enable, and a send is dead-lettered `native_match_strategy_unconfirmed` with no call.
- **Email match confirmation** (`email_merge_ack`, required): every call is keyed by the registrant's email, so mapped custom fields and tags apply to whatever subscription has that email. Only `identity.verified` registrations are sent; that is not proof of mailbox ownership.
- **Send beehiiv's welcome email** (`send_welcome_email`): `false` (default) or `true`; sent as a boolean on every create (VERIFIED parameter, default false; the page gives it no further description).
- **Double opt-in override** (`double_opt_override`): `not_set` (default: the publication's own setting; the parameter is then **not sent**), `on` (sent: beehiiv sends a double opt-in email and the subscriber "stays inactive until confirmed") or `off` (sent: "activates the subscriber immediately with no confirmation email"). VERIFIED values and semantics. `off` additionally requires the **Opt-in override confirmation** (`optin_ack`), because it skips the confirmation the publication would otherwise ask for; `on` needs no further acknowledgement (it is the more protective option, though beehiiv then emails the registrant). Any other value is refused by `validate` and `run`.
- **Reactivate unsubscribed people** (`reactivate_existing`): `false` (default) or `true`, sent as a boolean on every create. VERIFIED: "Whether or not to reactivate the subscription if they have already unsubscribed", intended "only for subscribers who knowingly resubscribe". The default never resubscribes someone who unsubscribed.
- **Custom field mapping** (`custom_field_map`): optional `source=<existing beehiiv custom field name>` pairs, e.g. `firstName=First Name,webinar_event_title=Last Webinar`. Sources are only `firstName`, `lastName`, `company`, `jobTitle`, `webinar_event_title`, `webinar_starts_at`, `webinar_registration_status` and `webinar_attendance`; never phone or anything else. Field names are printable ASCII without `,`/`=` or leading/trailing spaces, at most 50 characters; each source once, each name once (compared case-insensitively). Profile sources are sent only when the snapshot has the **profile** group. VERIFIED: "The custom fields must already exist for the publication"; unknown names are not an error but are dropped with a `warnings` list in the 200 response (not read).
- **UTM source** (`utm_source`): optional constant (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$`). When set, the create carries `utm_source`, `utm_medium: "webinar"` and `utm_campaign: <event slug>` (the slug only when it matches the same pattern; never invented). When empty, no UTM field is sent. VERIFIED parameter names and types; `utm_term`, `utm_content`, `referring_site` and `referral_code` are never sent.
- **Tags** (`tag_registered`, `tag_attended`, `tag_no_show`): optional tag names (`^[A-Za-z0-9][A-Za-z0-9 _.:-]{0,99}$`). `registration.created` → registered; `attendance.checked_in` and `attendance.recorded`=attended → attended; `attendance.recorded`=no_show → no-show; `registration.updated` applies no tag. VERIFIED: "If the tag does not exist on the publication, it will be created automatically"; "Max limit of 100 unique tags per publication".
- **Data processing confirmation**: required, as for every provider.

| Call | Body | Notes |
| --- | --- | --- |
| `POST /v2/publications/{publication_id}/subscriptions` | `email`, `reactivate_existing` (bool), `send_welcome_email` (bool), `custom_fields: [{"name", "value"}]` (mapped, non-empty scalar values only; omitted when empty), `utm_source`/`utm_medium`/`utm_campaign` (only with a configured UTM source), `double_opt_override` (only when `on` or `off` was chosen) | VERIFIED: 200 "Subscription created" with `data.id` = "the prefixed subscription id" (`sub_` + UUID), `data.status` ∈ `validating`, `invalid`, `pending`, `active`, `inactive`, `needs_attention`, `paused`, `unset`. **Never sent:** `tier` (the subscription is free; VERIFIED `tier` defaults to none and `premium_tiers`/`premium_tier_ids` override it), `premium_tiers`, `premium_tier_ids`, `stripe_customer_id`, `referring_site`, `referral_code`, `automation_ids` (would enrol the subscriber in automations), `newsletter_list_ids`, `skip_newsletter_list_auto_subscribe`, `complimentary_gift_id`, `utm_term`, `utm_content`. The response's `data.id` must match `^sub_[A-Za-z0-9_-]{1,100}$` (it becomes the tag call's path segment); otherwise the message is retried (`subscription: response_missing_id`) before any tag call. **Existing email: UNVERIFIED.** The create page does not say what beehiiv answers when the email already has a subscription; its `warnings` text says "The subscription is still created or updated", which suggests an upsert. The adapter treats any 2xx as converged (a retry resends the identical body) and any other 4xx as dead (`subscription: http_status: 4xx`), nothing merged. No update path (`PUT …/by_email/{email}`) is used: it would let a webinar registration change an existing subscriber's tier, Stripe id or list memberships, none of which this adapter may touch. |
| `POST …/subscriptions/{id}/tags` (only when the lifecycle has a tag name) | `{"tags": ["…"]}` | VERIFIED: 200 with the subscription object (not read beyond status). Re-adding a tag already on the subscription: **UNVERIFIED** (the page does not say); assumed a no-op, so a retry after a lost lease or timeout never duplicates it. Never `DELETE …/tags` (no tag removal). |

**Identity / dedupe.** Both calls key on the email (create) or the subscription id the create returned (tags); no platform id is written to beehiiv. A retry re-sends the identical create and tag requests; that this converges on one subscription depends on the undocumented existing-email behaviour above. Nothing derives from the delivery id, so no `_require_delivery_id`. The email comes from the profile, else from `identity.email`; with neither the message is cancelled `native_no_email` before any call.

**Limits.** Rate limit per API key in fixed 60-second windows: 30 (Free), 180 (Lite), 500 (Pro), 800 (Enterprise) requests per minute, 429 when exceeded, with `RateLimit-Limit`/`RateLimit-Remaining`/`RateLimit-Reset` headers and **no documented `Retry-After`** (VERIFIED, rate-limiting page); the generic status mapping retries 429 with backoff (and honours `Retry-After` if one ever appears). The create body (compact UTF-8) must be at most 20,000 bytes (`subscription: payload_too_large`, our own bound; beehiiv documents none) and mapped values must be scalars (`subscription: payload_unsupported`), both checked before the first call. Two calls per message, within `MAX_CALLS`.

**Consent / marketability.** Not an analytics provider: which purposes gate it is the operator's **Required consent** choice, re-checked before the tag call (a withdrawal between the two calls stops the tag; the subscription already created is not recalled). **Every delivered message leaves a subscription to the publication**, marketable under the publication's own double opt-in setting (or the chosen override); that is the point of this adapter and the operator confirms it explicitly. The adapter never reactivates an unsubscribed person unless `reactivate_existing` is set to `true`, never assigns a premium tier or Stripe customer, and never enrols the subscriber in automations or newsletter lists. UNVERIFIED and the operator's responsibility: whether automations in the publication trigger on new subscriptions, custom-field changes or tag application.

**Responses.** 2xx: delivered. 408, 425, 429, 5xx and 3xx: retried. Other 4xx (400 "Bad Request", 401 "missing, invalid, or expired" token, 403, 404 "Resource Not Found", 409): dead-lettered. Errors hold only a step name (`subscription`, `tags`) and a code; the `errors[].message` bodies are never read.

**Unresolved:**

- What `POST …/subscriptions` returns for an email that already has a subscription (200 with the existing/updated record, or a 4xx) and whether the second create changes anything on it (UTM, custom fields, status).
- Whether re-adding an existing tag is a no-op, an error, or a duplicate.
- API-key creation page (403): key scope (workspace vs publication), format, permissions ("`subscriptions:write`" is listed as an OAuth scope on every page; whether API keys carry scopes is unknown).
- Whether `double_opt_override: "on"` emails the registrant immediately and what `status` the subscription then has (`pending` is assumed).
- Custom field value types for `webinar_starts_at` (sent as the ISO-8601 string; beehiiv accepts string/double/boolean/list of string) and the exact matching of custom field `name` (case, spaces).
- Any request-size limit.
- No sandbox or live verification.

## Marketer setup

1. Ask the operator to allowlist the vendor host.
2. Create the integration, choose the provider and region, and save.
3. Fill in the settings: the match strategy and its confirmation (HubSpot, Attio and Pipedrive; Salesforce only matches by our person ID), the stable ID properties, attributes, External ID fields or custom field keys, the organization namespace and target (or leave both empty), for Salesforce your own My Domain host, and for beehiiv the publication ID plus the subscription confirmation (it subscribes registrants).
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

- **Salesforce** (composite and upsert pages read 2026-10-27; see its section): the client credentials token flow, the standard `Account` relationship-by-external-ID syntax, composite size limits, duplicate rules and permissions.
- **Pipedrive** (v2 OpenAPI file read 2026-10-27; see its section): rate limits, `due_time` time zone, custom-field search semantics, activity type keys.
- **beehiiv** (create-subscription, tag and rate-limiting pages read 2026-10-27; see its section): existing-email behaviour of the create call, tag re-add idempotency, API-key scoping (page 403).

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
