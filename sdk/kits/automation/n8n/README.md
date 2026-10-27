# n8n recipe: verified webinar deliveries

**Status: maintained reference, not run on an n8n instance.** `workflow.json` is importable;
its expressions are exercised by `simulate.js` in `sdk/kits/automation/test/automation.test.js`
against deliveries signed with the backend's algorithm (valid, rotation header, tampered, wrong
secret, stale, duplicate, routing). Node parameter names follow the n8n docs and source read on
2026-10-27 (Webhook, Extract from File, Crypto v2, IF v2, Remove Duplicates v2, Switch v3);
check them after import on your version.

## The chain

```
Webhook (POST /webhook/webinar-platform, Raw Body on)
  → Extract body (Extract from File: binary data → text $json.data)
  → HMAC (Crypto: Hmac SHA256 hex over "<delivery id>.<timestamp>.<body>", secret from a Crypto credential)
  → Signature valid and fresh (IF: header contains "v1=<hmac>" AND |now − timestamp| ≤ 300)
  → Drop repeated delivery ids (Remove Duplicates on Webinar-Delivery-Id, workflow scope)
  → Route by eventType (Switch: registration.created / registration.updated / attendance.recorded / other)
```

n8n lower-cases request headers, so the expressions read `headers['webinar-signature']` and
so on. The Crypto node's Hmac action hashes the string you give it with
`createHmac(type, secret).update(value).digest('hex')`; the Extract node yields the body as the
same UTF-8 text the platform signed, so the two agree byte for byte.

## Set-up

1. **n8n.** Import `workflow.json`. Create a **Crypto** credential whose *HMAC Secret* is the
   destination's signing secret (step 2) and select it in the *HMAC* node. Publish the workflow
   and copy the **Production URL** (`https://<your n8n host>/webhook/webinar-platform`). The
   test URL (`/webhook-test/...`) listens only while *Listen for test event* runs; the platform
   refuses it as a destination.
2. **Platform.** Studio → Integrations → New: *Platform preset* **n8n**, paste the production
   URL, pick events and groups, save, **Generate a new secret** (then go back to step 1's
   credential). Host rule: an n8n Cloud host `<name>.app.n8n.cloud` is accepted by suffix;
   any other host must be an exact entry of the operator's `WEBHOOK_HOST_ALLOWLIST` (and, like
   every destination, pass the SSRF policy: https, public DNS name). (MCP: `save_destination`
   with `"preset": "n8n"`.)
3. Connect your actions to the Switch outputs. The parsed payload is
   `{{ JSON.parse($('Extract body').item.json.data) }}`.
4. **Test**: studio → the integration → *Send test delivery* (fixed `webhook.test` payload; it
   leaves through the Switch's fallback output). Replay it with one changed byte from a terminal:
   the IF node must send it to the false branch.

## Behaviour and limits

- The Webhook node answers `200` as soon as the workflow starts (*Respond: Immediately*), so the
  platform treats every delivery as delivered and will not retry a workflow that fails later;
  a forged or replayed request is also just dropped, which is what you want. To make the
  platform retry on failure instead, switch *Respond* to *Using 'Respond to Webhook' Node* and
  answer 2xx only after your actions succeed (then the retry carries the same
  `Webinar-Delivery-Id`, which *Drop repeated delivery ids* would reject — remove that node or
  dedupe in your target system instead).
- Remove Duplicates (*Remove Items Processed in Previous Executions*) keeps the last 10,000
  values per workflow; on an n8n version without it, look the delivery id up in a Data table or
  database before acting.
- Keep *Raw Body* on and do not put a *Respond to Webhook* node before the IF unless it only
  answers after verification; the body must not be re-serialised before hashing.
