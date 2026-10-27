'use strict';
/*
 * Node simulation of the Make scenario in blueprint.json: the same expressions, evaluated
 * with Node's crypto instead of Make's runtime. The test in ../test/automation.test.js checks
 * that blueprint.json carries exactly the EXPRESSIONS below, so the simulation and the
 * importable scenario cannot drift apart. It proves the algorithm, not Make itself: the
 * scenario has not been run on a Make account.
 *
 * Make functions used (help.make.com, "Text and binary functions", 2026-10-27):
 *   sha256(text; [encoding]; [key])  "If the key argument is specified, sha256 HMAC hash is
 *                                     returned instead." Encodings: hex (default), base64, latin1.
 *   contains(text; search), abs(n), get(array; index), map(array; key; filterKey; filterValue)
 *   timestamp                        current Unix time in seconds (general variable; unverified)
 */
const crypto = require('node:crypto');

const TOLERANCE_SECONDS = 300;

/** The mapping expressions as they appear in blueprint.json (module ids 1-3). */
const EXPRESSIONS = {
  deliveryId: '{{get(map(1.headers; "value"; "name"; "webinar-delivery-id"); 1)}}',
  timestamp: '{{get(map(1.headers; "value"; "name"; "webinar-timestamp"); 1)}}',
  signature: '{{get(map(1.headers; "value"; "name"; "webinar-signature"); 1)}}',
  body: '{{1.body}}',
  expected: 'v1={{sha256(2.deliveryId + "." + 2.timestamp + "." + 2.body; "hex"; "REPLACE_WITH_SIGNING_SECRET")}}',
  signatureOk: '{{contains(2.signature; 3.expected)}}',
  age: '{{abs(timestamp - 2.timestamp)}}',
  eventType: '{{7.eventType}}',
};

/** Make's sha256(text; encoding; key): plain hash, or HMAC when a key is given. */
function sha256(textValue, encoding, key) {
  const enc = encoding || 'hex';
  if (!['hex', 'base64', 'latin1'].includes(enc)) throw new Error('unsupported encoding');
  const h = key === undefined || key === null || key === ''
    ? crypto.createHash('sha256')
    : crypto.createHmac('sha256', Buffer.from(String(key), 'utf8'));
  return h.update(Buffer.from(String(textValue), 'utf8')).digest(enc);
}

/** get(map(headers; "value"; "name"; <name>); 1): first header with that name (case-insensitive). */
function header(headers, name) {
  const hit = (headers || []).find((h) => String(h.name).toLowerCase() === name);
  return hit ? String(hit.value) : '';
}

/**
 * Run the scenario's logic for one request.
 * request: { headers: [{name, value}], body: string, secret: string, now: unix seconds, seen?: Set }
 * returns: { accepted, reason, duplicate, eventType, route }
 */
function runScenario(request) {
  const tolerance = request.tolerance === undefined ? TOLERANCE_SECONDS : request.tolerance;
  // Module 2 (Set multiple variables)
  const vars = {
    deliveryId: header(request.headers, 'webinar-delivery-id'),
    timestamp: header(request.headers, 'webinar-timestamp'),
    signature: header(request.headers, 'webinar-signature'),
    body: request.body,
  };
  // Module 3 (Set variable "expected")
  const expected = 'v1=' + sha256(`${vars.deliveryId}.${vars.timestamp}.${vars.body}`, 'hex', request.secret);
  // Router 4, route 1 filter: contains(2.signature; 3.expected) = true AND abs(timestamp - 2.timestamp) <= 300
  const signatureOk = vars.signature.includes(expected);
  const age = Math.abs(request.now - Number(vars.timestamp));
  const fresh = Number.isFinite(age) && age <= tolerance;
  if (!(signatureOk && fresh)) {
    return { accepted: false, reason: !signatureOk ? 'invalid signature' : 'stale timestamp', duplicate: false, eventType: null, route: 'reject (401)' };
  }
  // Data store dedupe (described in README.md; not part of the blueprint): key = delivery id.
  const seen = request.seen || new Set();
  const duplicate = seen.has(vars.deliveryId);
  if (!duplicate) seen.add(vars.deliveryId);
  // Module 7 (Parse JSON) then router 8 on {{7.eventType}}
  const parsed = JSON.parse(vars.body);
  const known = ['registration.created', 'registration.updated', 'attendance.recorded'];
  return {
    accepted: true,
    reason: duplicate ? 'duplicate delivery' : 'processed',
    duplicate,
    eventType: parsed.eventType,
    route: known.includes(parsed.eventType) ? parsed.eventType : 'other',
  };
}

module.exports = { EXPRESSIONS, TOLERANCE_SECONDS, sha256, header, runScenario };
