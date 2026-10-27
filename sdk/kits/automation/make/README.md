# Make recipe: verified webinar deliveries

**Status: instructions, with an importable blueprint that is unverified on a Make account.**
The expressions below (and in `blueprint.json`) are exercised by `simulate.js` in
`sdk/kits/automation/test/automation.test.js` against deliveries signed with the backend's
algorithm: valid, rotation header, tampered, wrong secret, stale, duplicate, and routing by
`eventType`. Make's own runtime was not used; the blueprint's module/filter structure follows
Make's export format from memory and may need adjusting on import — the expressions are the
part to keep.

## Why it works without code

Make's built-in `sha256(text; [encoding]; [key])` returns the **HMAC** when a key is given
("Text and binary functions", help.make.com, 2026-10-27; encodings `hex` (default), `base64`,
`latin1`). The platform signs `<delivery id>.<timestamp>.<raw body>` with HMAC-SHA256 and sends
`Webinar-Signature: v1=<hex>`, so a filter can recompute and compare it.

## Set-up

1. **Make.** Create a scenario, add *Webhooks → Custom webhook*, **Add** a webhook and open its
   advanced settings: turn on **JSON pass-through** (the scenario needs the raw body text, not
   parsed fields) and **Get request headers**. Copy the URL (`https://hook.<zone>.make.com/<id>`;
   zones `eu1`, `eu2`, `us1`, `us2`).
2. **Platform.** Studio → Integrations → New: *Platform preset* **Make**, paste the URL, pick
   events and groups, save, **Generate a new secret**. The operator must have the
   `hook.<zone>.make.com` host on `WEBHOOK_HOST_ALLOWLIST`. (MCP: `save_destination` with
   `"preset": "make"`.)
3. **Import** `blueprint.json` (scenario menu → *Import Blueprint*), or build the modules by hand:

   | # | Module | Mapping |
   |---|---|---|
   | 1 | Custom webhook | the one from step 1 (re-select it after import) |
   | 2 | Tools → *Set multiple variables* | `deliveryId` = `{{get(map(1.headers; "value"; "name"; "webinar-delivery-id"); 1)}}` · `timestamp` = same with `webinar-timestamp` · `signature` = same with `webinar-signature` · `body` = the webhook's raw JSON item (shown as `{{1.body}}` in the blueprint; map whatever the webhook names it with JSON pass-through on — unverified) |
   | 3 | Tools → *Set variable* `expected` | `v1={{sha256(2.deliveryId + "." + 2.timestamp + "." + 2.body; "hex"; "<signing secret>")}}` |
   | 4 | Router, route **accept** filter | `{{contains(2.signature; 3.expected)}}` *equal (boolean)* `true` **and** `{{abs(timestamp - 2.timestamp)}}` *less than or equal (number)* `300` |
   | 7 | JSON → *Parse JSON* | `{{2.body}}` |
   | 8 | Router on `{{7.eventType}}` | `registration.created`, `registration.updated`, `attendance.recorded`, … (replace the placeholder *Set variable* modules with your actions) |
   | 5 | Route **reject** (`contains(...)` = `false` **or** age `> 300`) → *Webhook response* | status `401` |

   `contains` rather than *equal* so a header carrying two signatures during secret rotation
   still passes. `timestamp` is Make's current Unix time (seconds).
4. **Dedupe** (recommended): *Data store → Get a record* with key `{{2.deliveryId}}` before the
   actions, a filter "record does not exist", then *Add/replace a record* with the same key.
   The simulation models this; the blueprint leaves it out because a data store is account
   specific.
5. **Test**: studio → the integration → *Send test delivery* (fixed `webhook.test` payload, no
   personal data) should take the accept route; a replay with one changed byte must take the
   reject route.

## Behaviour and limits

- Make queues the request and answers `200 Accepted` **before** the scenario runs (unless a
  *Webhook response* module answers first), so the platform sees every delivery as delivered;
  scenario errors are not re-requested. The platform retries only when Make does not answer 2xx
  (queue full → 400, or an outage), always with the same `Webinar-Delivery-Id` — hence the dedupe.
- The secret lives in the scenario mapping (Make has no secret store for functions); restrict
  who can open the scenario and rotate the secret from the studio if that changes.
- Each delivery is one bundle; occurrence-level events are already fanned out per registrant.
