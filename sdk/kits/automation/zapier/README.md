# Zapier recipe: verified webinar deliveries

**Status: maintained reference, not run on a Zapier account.** `verify-step.js` is exercised by
`sdk/kits/automation/test/automation.test.js` against deliveries signed with the backend's
algorithm (`examples/verify_webhook.py`): valid, secret-rotation header, tampered, wrong secret,
stale and duplicate. The Zap set-up below follows Zapier's help pages read on 2026-10-27; the
exact field names the Catch Raw Hook exposes in the mapping picker were not verified.

## What you get

A Zap whose trigger is one platform destination. Every run verifies `Webinar-Signature`
(HMAC-SHA256 over `<delivery id>.<timestamp>.<raw body>`), rejects stale timestamps (300 s),
flags repeated `Webinar-Delivery-Id`s, and exposes the verified payload plus a few lifted fields
(`email`, `eventTitle`, `sessionStartsAt`, `registrationId`, ...) to the following actions.

## Set-up

1. **Platform side.** Studio → Integrations → New: name it, set *Platform preset* to **Zapier**,
   paste the Catch Hook URL (`https://hooks.zapier.com/hooks/catch/<account id>/<hook id>/`),
   pick the events and data groups, save, then **Generate a new secret** and keep it for step 4.
   The operator must have `hooks.zapier.com` on `WEBHOOK_HOST_ALLOWLIST`. (MCP:
   `save_destination` with `"preset": "zapier"`.)
2. **Trigger.** *Webhooks by Zapier* → **Catch Raw Hook** (not Catch Hook: the plain one parses
   the body, and the signature is over the raw bytes). Copy its URL into step 1. Zapier's docs:
   "The request body will be unparsed (max 2MB) and also include headers."
3. **Action.** *Code by Zapier* → **Run JavaScript**. Paste the whole of `verify-step.js`.
4. **Input Data** (left: name, right: mapped field from the trigger):

   | Name | Value |
   |---|---|
   | `raw_body` | the trigger's raw body field |
   | `signature` | header `Webinar-Signature` |
   | `timestamp` | header `Webinar-Timestamp` |
   | `delivery_id` | header `Webinar-Delivery-Id` |
   | `secret` | the signing secret from step 1 |
   | `store_secret` | optional: a Storage by Zapier secret (UUID) to remember delivery ids |

   Zapier advises against secrets in Code steps and Input Data; there is no other place for the
   signing secret in a Zap, so treat the Zap (and its history) as holding it, and rotate the
   secret from the studio if the Zap is shared.
5. **Filter** (*Filter by Zapier*): continue only if `duplicate` **(Boolean) is false**.
6. Your actions, mapped from the Code step's output (`payload__profile__firstName`, ...).
7. **Test**: studio → the integration → *Send test delivery* (or MCP `send_test_delivery`)
   sends the fixed `webhook.test` fixture; the Code step must show `ok: true`. Then send a
   tampered replay from a terminal to see the step fail.

## Behaviour and limits

- Invalid signature, stale timestamp, non-JSON body or an unsupported `schemaVersion` **throw**:
  the run errors in Zap history and nothing downstream runs. Zapier answers the platform with
  200 regardless (the response is not customisable), so the platform does not retry forged
  requests — correct — but it also does not retry a run that failed later; the platform's own
  retries only happen when Zapier itself fails to answer 2xx (then with the same
  `Webinar-Delivery-Id`, hence the dedupe).
- Dedupe uses one StoreClient key (`webinar_seen_deliveries`) holding `{deliveryId: time}`,
  pruned to the replay window. Storage by Zapier: 500 keys per store, 1 MB per value, keys
  deleted after two months of inactivity, and no atomic read-modify-write, so two deliveries
  verified in the same second can both pass. Make the downstream action idempotent on
  `deliveryId` too (for example a "find or create" keyed on it).
- Code steps run on Node.js 22 with the standard library (`require('crypto')`), 1 s on the Free
  plan, 30 s on Professional/Team; verification takes milliseconds.
- `Webinar-Delivery-Id`, `delivery_metadata.deliveryId` and `eventType` are stable across the
  platform's retries; `delivery_metadata.attempt` changes.
