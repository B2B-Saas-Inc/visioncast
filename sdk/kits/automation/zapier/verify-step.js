/*
 * Code by Zapier (JavaScript) step: verify one webinar-platform webhook delivery.
 *
 * Paste the whole file into a "Code by Zapier → Run JavaScript" action placed right after
 * a "Webhooks by Zapier → Catch Raw Hook" trigger (Catch Raw Hook keeps the unparsed body
 * and the headers; the plain Catch Hook parses the body, which breaks the signature).
 *
 * Input Data (names on the left, mapped from the trigger on the right):
 *   raw_body     the trigger's raw body (the unparsed JSON text)
 *   signature    header Webinar-Signature
 *   timestamp    header Webinar-Timestamp
 *   delivery_id  header Webinar-Delivery-Id
 *   secret       the destination's signing secret (studio → Integrations → Signing secret)
 *   store_secret optional: a Storage by Zapier secret (UUID) used to remember delivery ids
 *
 * What it does, exactly as examples/verify_webhook.py in the platform repository:
 *   1. signed = "<delivery id>.<timestamp>." + raw body bytes (UTF-8)
 *      expected = "v1=" + hex(HMAC-SHA256(secret, signed)); constant-time compare with any of
 *      the comma/space separated values in the header (several during secret rotation);
 *   2. the timestamp must be within 300 s of now;
 *   3. with store_secret set, each Webinar-Delivery-Id is accepted once per window: a retry of
 *      an already processed delivery comes back with duplicate = true. Add a Filter step
 *      "only continue if duplicate is false" (Zapier does not retry on its own, but the
 *      platform re-sends with the same id after a failed or slow response).
 * An invalid signature, a stale timestamp or an unparsable body throws, which stops the Zap
 * run and shows the reason in Zap history; nothing downstream runs.
 *
 * Limits (Zapier docs, 2026-10-27): Code steps run on Node.js 22 with the standard library;
 * Input Data values are strings; Zapier recommends against secrets in Code steps and Input
 * Data, and this step has no better place for the signing secret — treat the Zap as holding
 * it. StoreClient keeps the seen ids under ONE key as a JSON object pruned to the timestamp
 * window (Storage by Zapier: 500 keys per store, 1 MB per value, no atomic read-modify-write,
 * so two deliveries verified in the same second can both pass; the downstream system should
 * also key on delivery_id).
 *
 * Verified only by the Node simulation in sdk/kits/automation/test/automation.test.js, not on
 * a live Zapier account. Exact trigger field names (raw body, header fields) are what the
 * Catch Raw Hook shows in the mapping picker and were not verified.
 */
'use strict';

const TOLERANCE_SECONDS = 300;
const SEEN_KEY = 'webinar_seen_deliveries';

function text(value) {
  return value === undefined || value === null ? '' : String(value).trim();
}

/** HMAC-SHA256 hex over "<deliveryId>.<timestamp>." + rawBody, "v1=" prefixed. */
function expectedSignature(crypto, secret, deliveryId, timestamp, rawBody) {
  const signed = Buffer.concat([Buffer.from(`${deliveryId}.${timestamp}.`, 'utf8'), Buffer.from(rawBody, 'utf8')]);
  return 'v1=' + crypto.createHmac('sha256', Buffer.from(secret, 'utf8')).update(signed).digest('hex');
}

function signatureMatches(crypto, header, expected) {
  const want = Buffer.from(expected, 'utf8');
  return text(header)
    .replace(/,/g, ' ')
    .split(/\s+/)
    .filter(Boolean)
    .some((candidate) => {
      const got = Buffer.from(candidate, 'utf8');
      return got.length === want.length && crypto.timingSafeEqual(got, want);
    });
}

/**
 * Remember deliveryId in the store; returns true when it is new, false when already seen.
 * One key holds {deliveryId: unixSeconds}; entries older than the replay window are pruned.
 */
async function rememberDelivery(store, deliveryId, now, tolerance) {
  let seen = await store.get(SEEN_KEY);
  if (typeof seen === 'string') {
    try { seen = JSON.parse(seen); } catch (e) { seen = {}; }
  }
  if (!seen || typeof seen !== 'object' || Array.isArray(seen)) seen = {};
  const ttl = 2 * tolerance + 60;
  for (const [id, at] of Object.entries(seen)) {
    if (!(Number(at) > now - ttl)) delete seen[id];
  }
  if (Object.prototype.hasOwnProperty.call(seen, deliveryId)) return false;
  seen[deliveryId] = now;
  await store.set(SEEN_KEY, seen);
  return true;
}

async function verifyWebinarDelivery(input, options) {
  options = options || {};
  const crypto = options.crypto || require('crypto');
  const now = options.now === undefined ? Math.floor(Date.now() / 1000) : Number(options.now);
  const tolerance = options.tolerance === undefined ? TOLERANCE_SECONDS : Number(options.tolerance);
  const store = options.store || null;

  const secret = text(input.secret);
  const rawBody = input.raw_body;
  const deliveryId = text(input.delivery_id);
  const timestamp = text(input.timestamp);
  const signature = text(input.signature);
  const missing = [];
  if (!secret) missing.push('secret');
  if (typeof rawBody !== 'string' || !rawBody) missing.push('raw_body');
  if (!deliveryId) missing.push('delivery_id');
  if (!timestamp) missing.push('timestamp');
  if (!signature) missing.push('signature');
  if (missing.length) throw new Error('Missing Input Data: ' + missing.join(', '));
  if (!/^\d{1,12}$/.test(timestamp)) throw new Error('Webinar-Timestamp is not Unix seconds');
  const ts = Number(timestamp);
  if (Math.abs(now - ts) > tolerance) throw new Error('Stale timestamp: delivery rejected (replay protection)');
  if (!signatureMatches(crypto, signature, expectedSignature(crypto, secret, deliveryId, ts, rawBody))) {
    throw new Error('Invalid Webinar-Signature: body altered, wrong secret or not from the platform');
  }
  let duplicate = false;
  if (store) duplicate = !(await rememberDelivery(store, deliveryId, now, tolerance));

  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch (e) {
    throw new Error('Body is not JSON');
  }
  if (!payload || payload.schemaVersion !== '1') throw new Error('Unsupported schemaVersion');
  const occurrence = (payload.event && payload.event.occurrence) || {};
  return {
    ok: true,
    duplicate,
    deliveryId,
    eventType: payload.eventType,
    occurredAt: payload.occurredAt || null,
    // Common fields lifted to the top level for easy mapping (null when the group is not sent).
    email: payload.identity ? payload.identity.email || null : null,
    personId: payload.identity ? payload.identity.personId || null : null,
    registrationId: payload.registration ? payload.registration.id || null : null,
    eventTitle: payload.event ? payload.event.title || null : null,
    eventSlug: payload.event ? payload.event.slug || null : null,
    sessionStartsAt: occurrence.startsAt || null,
    sessionTimezone: occurrence.timezone || null,
    // The full verified payload (Zapier flattens nested objects as payload__group__field).
    payload,
  };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { verifyWebinarDelivery, rememberDelivery, expectedSignature, signatureMatches, TOLERANCE_SECONDS, SEEN_KEY };
}

// Inside Zapier: inputData and StoreClient are provided; return the step output.
if (typeof inputData !== 'undefined') {
  const store = typeof StoreClient === 'function' && inputData.store_secret ? StoreClient(inputData.store_secret) : null;
  return verifyWebinarDelivery(inputData, { store });
}
