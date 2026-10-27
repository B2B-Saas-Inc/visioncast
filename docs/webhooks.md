# Webhooks

The platform delivers events to **generic HTTPS webhook destinations** (kinds
`email`, `crm`, `generic` are labels only — there are no native vendor connectors).
Deliveries come from a PostgreSQL transactional outbox processed by the
`run_worker` process.

## Event types

`registration.created`, `registration.updated`, `occurrence.cancelled`,
`occurrence.rescheduled`, `reminder.due`, `recording.published`,
`attendance.recorded`, `attendance.checked_in`, `follow_up.requested`.

Occurrence-level events (cancel, reschedule, recording) are fanned out **per
registration**, so each delivery describes one registrant.

`attendance.recorded` is emitted by every explicit attendance write: the management API,
waiting-room finalization, and attendance imports (provider pull or report file; see
[hosting.md](hosting.md#attendance-import)). The payload is the same in every case; the
origin is kept on the registration's `AttendanceEvent.source` (`api`, `waiting_room`,
`import:zoom`, `import:riverside`, `import:file`) and is not part of the payload. An import
records each registration at most once, so re-importing the same list never re-sends. A destination receives
an event only if it is `enabled`, lists the type in `event_types`, and every purpose
in `required_consents` is currently granted for the person (latest consent record
per purpose is `granted` and not withdrawn). People who have been erased receive no
further deliveries.

## Payload (schemaVersion `"1"`)

| Group | Contents |
|---|---|
| `identity` | `personId` (server UUID), `email`, `verified` |
| `profile` | canonical fields `email, phone, firstName, lastName, name, company, jobTitle, companySize` (null when unknown) + `customFields` |
| `organization` | `externalIds: [{namespace, value, verified, source}]` (`browser` = unverified; `trusted_api` = verified) |
| `consents` | `sms`, `analytics`, `advertising`: `{granted, disclosureVersion, recordedAt, withdrawnAt, source, serverVerified}` |
| `attribution` | `firstTouch`, `registrationTouch`: `{parameters, identifiers, pageUrl, referrer, recordedAt}` (URLs stored without query/hash) |
| `registration` | `id, status, attendance, formVersion, identityVerified, createdAt, updatedAt` |
| `event` | self-contained snapshot: `id, slug, title, summary, heroImageUrl, aida{attention,interest,decision,action}, learningOutcomes, hosts[], topics[], thankYou, cta, revision, occurrence{id,startsAt,endsAt,timezone,status,icsUid,icsSequence,cancelledAt, recordingUrl?, managedRoomUrl? \| (joinUrl?, waitingRoomUrl?)}` |
| `delivery_metadata` | `deliveryId, destinationId, destinationKind, eventType, createdAt, attempt` |
| `mapped` | only when the destination has a `field_mapping` (see below) |

Top-level keys `schemaVersion`, `eventType`, `occurredAt` are always present.
Payload data is a snapshot taken when the event was enqueued, but every send
(including retries and requeues) re-filters that snapshot against the **current**
destination and occurrence — see [Send-time re-filtering](#send-time-re-filtering).
`Webinar-Delivery-Id`, `delivery_metadata.deliveryId` and the event type never
change; `delivery_metadata.attempt` does, and so may the filtered groups, links and
`mapped` if the destination or room changed between attempts.

**Unverified registrations** (`registration.identityVerified=false`, `identity.verified=false`):
an unknown browser registered with the email of an existing person. The profile
group then contains only what *that submission* contained (`Registration.submitted_profile`),
not the stored person profile; `organization` contains only the IDs that submission
supplied (all `verified: false`, `source: browser`); `consents` reports every purpose
as not granted; and `attribution.firstTouch` is `null`.
Treat these as unconfirmed claims; do not overwrite CRM records from them.

**Links** (`event.occurrence`, every event type including `attendance.checked_in`):

| Occurrence state at send time | `include_private_links` off | `include_private_links` on |
|---|---|---|
| Managed waiting room **enabled** | `managedRoomUrl` | `managedRoomUrl` (provider links are **never** sent) |
| Managed waiting room disabled | no links | `joinUrl`, `waitingRoomUrl` (provider links) |

`managedRoomUrl` is the stable attendee URL `PUBLIC_BASE_URL + /w/<code>/` (the same
value as the management API's `waitingRoom.url`). It is meant to be shared in
invites, so it is sent whatever `include_private_links` says. While the room is on,
the provider `joinUrl` / `waitingRoomUrl` are withheld from every destination so all
attendees go through admission and check-in. `recordingUrl` is included once the
recording is published.

**Consent evidence**: `serverVerified` is never true for browser-submitted claims
(`source: client_claim`).

## Destination restrictions

* `allowed_groups`: groups this destination may receive. `event` and
  `delivery_metadata` are always included. If empty, only `event`, `registration`
  and `delivery_metadata` are sent (no person data).
* `required_consents`: purposes (e.g. `["advertising"]`) that must be granted.
* `field_mapping`: `{"profile.email": "contact_email", "event.occurrence.startsAt": "webinar_start"}`
  adds `"mapped": {"contact_email": ..., "webinar_start": ...}`. Only paths in
  groups the destination is allowed to receive are mapped; others are omitted.
  Original groups are kept alongside `mapped`. Mapping is applied exactly once,
  from canonical group paths only: a source path starting with `mapped.` or naming
  another mapping target is ignored, so `{"profile.email": "a", "a": "b"}` yields only
  `"a"` (no chaining). A mapped private link (e.g. `"event.occurrence.joinUrl": "join_link"`)
  follows the link rules above and is `null` when the link is not sent.

### Send-time re-filtering

The stored outbox payload keeps the canonical (unmapped) groups the destination was
allowed when the event was queued; `mapped` is derived data. Immediately before each
send the worker re-reads the destination and:

1. cancels and purges the message if the person was erased/deleted or a
   `required_consents` purpose is no longer granted (unchanged behaviour);
2. drops the stored `mapped` object and every link key in `event.occurrence`, then
   recomputes the links from the **current** occurrence and `include_private_links`
   (table above). Turning the flag off, or enabling the managed room, after a message
   was queued therefore stops the provider link being sent on the next attempt;
   disabling the room drops a stale `managedRoomUrl`;
3. applies the **current** `allowed_groups` (narrowing only: groups not stored at
   enqueue are never added later) and rebuilds `mapped` from the **current**
   `field_mapping`.

Messages queued by older versions have the same shape and are handled the same way:
private links stored under mapped names disappear with the old `mapped` object and
are only re-derived if the current policy allows them. The body is then signed as
usual.

## HTTP request

`POST <destination url>` with:

| Header | Value |
|---|---|
| `Content-Type` | `application/json; charset=utf-8` |
| `User-Agent` | `webinar-platform-webhook/1` |
| `Webinar-Delivery-Id` | stable UUID (same on every retry) |
| `Webinar-Event-Type` | event type |
| `Webinar-Timestamp` | Unix seconds at send time |
| `Webinar-Signature` | `v1=<hex>` |

Body is compact JSON with sorted keys (UTF-8).

### Signature

```
signed    = UTF8("{delivery_id}.{timestamp}.") + raw_body_bytes
signature = "v1=" + hex(HMAC_SHA256(key=UTF8(signing_secret), msg=signed))
```

Receivers must: verify against the **raw** body bytes, compare in constant time,
reject timestamps more than 300 s from their clock, and reject/ack-without-processing
delivery ids already seen. See [`examples/verify_webhook.py`](../examples/verify_webhook.py)
(`python3 examples/verify_webhook.py --self-test`).

Set or rotate the secret with:

```
python manage.py set_destination_secret --destination <uuid> --generate
```

Secrets (and optional `--credentials-stdin` JSON) are Fernet-encrypted with
`INTEGRATION_MASTER_KEY` from the environment. For master-key rotation put the new key
in `INTEGRATION_MASTER_KEY` and old keys (comma separated) in
`INTEGRATION_MASTER_KEY_PREVIOUS`, then re-set secrets.

## Delivery, retries and dead letters

* Rows are written in the same DB transaction as the business change.
* Workers claim due rows with `SELECT … FOR UPDATE SKIP LOCKED`, take a lease
  (`OUTBOX_LEASE_SECONDS`, default 120), send outside the transaction, then record
  the result only if they still hold the lease. Expired leases are re-claimed and
  count as an attempt. Delivery is therefore **at-least-once**: deduplicate on
  `Webinar-Delivery-Id`.
* 2xx = delivered. Anything else (including **3xx — redirects are never followed**),
  timeouts, TLS and connection errors are failures.
* Backoff after attempt *n*: `min(3600, 30 × 2^n)` seconds ±20% jitter.
* After `max_attempts` (per destination, default 8) the message is `dead`.
  SSRF-policy violations dead-letter immediately; DNS failures are retried.
* Disabled destinations: messages are held and rechecked every 15 minutes.
* Admins can requeue dead messages (admin action → `requeue_message`, audited).
  Messages whose payload has been purged cannot be requeued.
* `last_error` holds only a short reason and status code — never response bodies,
  payload values or secrets. At most 2 KB of response body is read and it is not stored.
* Timeout: `WEBHOOK_TIMEOUT_SECONDS` (default 10). Batch size: `OUTBOX_BATCH_SIZE` (20).

## SSRF protections

A destination URL must:

* use `https`, contain no userinfo and no fragment;
* use port 443 or a port in `WEBHOOK_ALLOWED_PORTS`;
* have a DNS hostname (not an IP literal, nor numeric shorthand) equal to, or a
  subdomain of, an entry in `WEBHOOK_HOST_ALLOWLIST` (empty allowlist = nothing allowed);
* not be `localhost` or end in `.localhost`, `.local`, `.internal`, `.localdomain`,
  `.home.arpa`, `.lan`, `.intranet`, `.corp`.

At send time **every** resolved address must be globally routable: private,
loopback, link-local (incl. `169.254.169.254` metadata), CGNAT `100.64.0.0/10`,
multicast, reserved, unspecified, documentation/benchmark ranges, ULA
(`fc00::/7`, incl. `fd00:ec2::254`), site-local, and IPv4-mapped / NAT64 /
6to4 / Teredo addresses (embedded IPv4 checked, 6to4/Teredo rejected) are refused.
The connection is opened to the vetted IP (no second lookup, defeating DNS
rebinding) while TLS SNI, full certificate verification and the `Host` header use
the hostname.

## Retention

`python manage.py purge_personal_data` replaces outbox payloads with
`{"purged": true}` for erased people (undelivered messages are cancelled) and for
delivered/cancelled/dead messages older than `OUTBOX_PAYLOAD_RETENTION_DAYS` (30).

## Example payload (fake data)

```json
{
  "schemaVersion": "1",
  "eventType": "registration.created",
  "occurredAt": "2026-03-02T10:15:00Z",
  "identity": {"personId": "6f1c2a8e-1111-4c1d-9a7e-000000000001", "email": "ada@example.com", "verified": true},
  "profile": {
    "email": "ada@example.com", "phone": null, "firstName": "Ada", "lastName": "Example",
    "name": null, "company": "Example Ltd", "jobTitle": "Engineer", "companySize": "51-200",
    "customFields": {"custom.industry": "Software"}
  },
  "organization": {"externalIds": [{"namespace": "crm", "value": "ACC-0001", "verified": false, "source": "browser"}]},
  "consents": {
    "sms": {"granted": false, "disclosureVersion": null, "recordedAt": null, "source": null, "serverVerified": false},
    "analytics": {"granted": true, "disclosureVersion": null, "recordedAt": "2026-03-02T10:15:00Z", "withdrawnAt": null, "source": "client_claim", "serverVerified": false},
    "advertising": {"granted": false, "disclosureVersion": null, "recordedAt": "2026-03-02T10:15:00Z", "withdrawnAt": null, "source": "client_claim", "serverVerified": false}
  },
  "attribution": {
    "firstTouch": {"parameters": {"utm_source": "newsletter"}, "identifiers": {}, "pageUrl": "https://www.example.com/webinars/intro", "referrer": null, "recordedAt": "2026-03-01T09:00:00Z"},
    "registrationTouch": {"parameters": {"utm_source": "newsletter"}, "identifiers": {}, "pageUrl": "https://www.example.com/webinars/intro", "referrer": null, "recordedAt": "2026-03-02T10:15:00Z"}
  },
  "registration": {"id": "0b7e9d4c-2222-4b8f-8c3a-000000000002", "status": "registered", "attendance": "unknown", "formVersion": "default-v1", "identityVerified": true, "createdAt": "2026-03-02T10:15:00Z", "updatedAt": "2026-03-02T10:15:00Z"},
  "event": {
    "id": "3c9d6b1a-3333-4e2f-a1b2-000000000003", "slug": "intro-to-widgets", "title": "Intro to Widgets",
    "summary": "A 45-minute tour.", "heroImageUrl": "https://cdn.example.com/hero.jpg",
    "aida": {"attention": "…", "interest": "…", "decision": "…", "action": "Save your seat"},
    "learningOutcomes": ["Build a widget"], "hosts": [{"name": "Grace Host", "slug": "grace-host", "title": "CTO", "imageUrl": null}],
    "topics": [{"name": "Widgets", "slug": "widgets"}], "thankYou": {"mode": "message", "message": "See you there!"}, "revision": 3,
    "occurrence": {"id": "9a8b7c6d-4444-4d3c-b2a1-000000000004", "startsAt": "2026-03-10T15:00:00Z", "endsAt": "2026-03-10T15:45:00Z",
      "timezone": "Europe/London", "status": "scheduled", "icsUid": "9a8b7c6d-4444-4d3c-b2a1-000000000004@webinars.example.com", "icsSequence": 0, "cancelledAt": null}
  },
  "delivery_metadata": {"deliveryId": "5e4d3c2b-5555-4a1b-9c8d-000000000005", "destinationId": "7d6c5b4a-6666-4f5e-8d7c-000000000006", "destinationKind": "crm", "eventType": "registration.created", "createdAt": "2026-03-02T10:15:00Z", "attempt": 1}
}
```
