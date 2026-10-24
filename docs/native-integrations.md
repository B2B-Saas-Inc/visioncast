# Native integrations

Status: **all four adapters are implemented on `ssrf.request_json_bounded`; none is production-ready.** That function is a bounded POST/PUT transport: response body up to 64 KB, `Retry-After` parsed, and credentialed requests pinned to the vendor hosts. If the function were ever missing, HubSpot and Attio fail validation, and their deliveries are dead-lettered with `native_transport_unavailable` without any network call.

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
    - A request carrying `Authorization` is refused unless its host is in the adapter's vendor host set.
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

  The operator must also add the host to `WEBHOOK_HOST_ALLOWLIST`. The destination URL is set from provider and region and is read-only in the studio. Self-hosted PostHog and HubSpot EU-specific hosts are **not supported**.
- **Studio.**
  - A provider dropdown, with fields for the provider's settings.
  - A separate credential form for secrets. Blank keeps the stored value, and values are never rendered.
  - A "Check before enabling" panel and a redacted request preview.
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
  - PostHog alone keeps a built-in rule: it requires Analytics consent, because it is an analytics tool and the platform's consent model has that purpose.
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
  - A changed or invalid configuration dead-letters it.
  - **Writes already completed by earlier steps are not recalled or deleted in the vendor.** Requeueing a dead message reruns the sequence; Customer.io events are not idempotent.
- **Test sends.** The synthetic `webhook.test` fixture (MCP `send_test_delivery`) has no person, so no vendor contract applies. For native destinations it is cancelled with `native_test_send_unsupported`, never reported as delivered. Destinations with required consents cancel it earlier, with `consent_not_granted`.

## Provider contracts implemented

| Provider | Calls | Identity / dedupe |
| --- | --- | --- |
| PostHog | `POST /batch/` with `api_key` in the body, containing `$identify` (with `$set`, only when the profile group is allowed), `$groupidentify` (`$group_type`, `$group_key`, `$group_set`) and the event, with `$groups` | `distinct_id` = platform person ID; group key = verified org ID. Each event has a deterministic `uuid` (uuid5 of delivery ID and step) and a fixed timestamp. Whether PostHog dedupes on it is **not assumed**. Requires Analytics consent on the destination. |
| Customer.io | Track API v2 `POST /api/v2/entity`, up to three calls: identify the org object (`object_type_id`, `object_id`); identify the person with `cio_relationships`; send the person event. Basic auth with site ID and API key. The event `timestamp` is **integer Unix seconds**, per the primary docs at https://docs.customer.io/integrations/api/track/tag/track_v2/entity/ (searched 2026-10-23). It is omitted if `occurredAt` can't be parsed. | `identifiers.id` = person ID. Identify calls are keyed by that ID and assumed idempotent (unverified). A retry after a partial failure **can repeat the event** because no event dedupe ID is sent (format unverified). |
| HubSpot | `POST /crm/v3/objects/contacts/batch/upsert` (`idProperty` = `email` in email mode, per the primary docs at https://developers.hubspot.com/docs/api-reference/crm-contacts-v3/batch/post-crm-v3-objects-contacts-batch-upsert; or the configured unique contact property in custom-ID mode); `POST /crm/v3/objects/companies/batch/upsert`; `POST /crm/v3/objects/{participation object}/batch/upsert` (`idProperty` = registration ID property); `POST /crm/v4/associations/contacts/companies/batch/associate/default`; `POST /crm/v4/associations/contacts/{object}/batch/associate/default` | Upserts are keyed by person, org and registration IDs, so a retry targets the same records (no plain create). A single record per key relies on unique-value properties, which is unverified. Participation is a **custom object** with properties `webinar_event_id`, `webinar_event_title`, `webinar_occurrence_id`, `webinar_starts_at`, `webinar_registration_status`, `webinar_attendance` and `webinar_last_event_type`, plus a default association to contacts that the HubSpot admin must create. |
| Attio | `PUT /v2/objects/companies/records?matching_attribute=…`; `PUT /v2/objects/people/records?matching_attribute=email_addresses` (email mode) or `…?matching_attribute=<custom attribute>` (custom-ID mode), with `email_addresses` and a `company` record reference; `PUT /v2/lists/{list}/entries` (parent person, `entry_values` with the `webinar_*` attributes and `webinar_registration_id`) | Assert by configured unique attributes. Results in **one list entry per person per list**, not one per registration. The workspace must define these attributes. |

## Marketer setup

1. Ask the operator to allowlist the vendor host.
2. Create the integration, choose the provider and region, and save.
3. Fill in the settings: the match strategy and its confirmation (HubSpot and Attio), the stable ID properties or attributes, and the organization namespace and target (or leave both empty).
4. Enter secrets in the provider credentials form.
5. Tick the supported events and the identity, registration and event groups. Choose **Required consent** according to your policy; PostHog requires Analytics. Then set the data processing confirmation.
6. Review the redacted preview, then enable. Enabling re-validates.

## Gaps and assumptions to verify (official docs needed)

Evidence so far:

- Pi read the primary docs on 2026-10-23 for two facts: the Customer.io Track v2 entity `timestamp` is integer seconds, and HubSpot contacts batch upsert supports `idProperty: "email"`.
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

Other known gaps:

- **Idempotency is not guaranteed for every call.**
  - HubSpot and Attio record writes are upserts or asserts keyed by our IDs. Tests prove a retry resends the same key and never a plain create. That this means no duplicate *in the vendor* depends on the configured property or attribute being unique, which must be verified in the workspace.
  - PostHog events carry stable uuids, but vendor dedupe on them is unverified.
  - Customer.io events have no dedupe ID, and a retry after a partial failure resends the event (covered by a test).
  - The HubSpot association calls and the Attio list-entry assert are assumed idempotent but unverified.
- There's no "send test to a chosen record" button. That would need a new URL route, and this work doesn't own that file.
- Deletion and erasure propagation to vendors is not implemented.
- There are no owner-approved sandbox checks yet.
