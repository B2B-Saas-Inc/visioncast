'use strict';
/*
 * Workflow-automation recipes, checked against the backend's signing algorithm.
 *
 * fixtures/deliveries.json is produced by make-fixtures.py with examples/verify_webhook.py
 * (the receiver twin of webinars.crypto.sign_webhook). Each recipe must accept the valid
 * delivery, the rotation header, and reject the tampered, wrong-secret and stale cases;
 * the dedupe path must flag the second identical delivery. No network; node:test only.
 *
 *   node --test sdk/kits/automation/test/automation.test.js
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');
const fixture = JSON.parse(read('sdk/kits/automation/test/fixtures/deliveries.json'));
const zapier = require('../zapier/verify-step.js');
const make = require('../make/simulate.js');
const n8n = require('../n8n/simulate.js');

const { secret, now, deliveryId, timestamp, body, signature, rotationSignature, wrongSecretSignature } = fixture;

/** Port of webinars.crypto.sign_webhook / examples/verify_webhook.compute_signature. */
function sign(key, id, ts, raw) {
  const signed = Buffer.concat([Buffer.from(`${id}.${ts}.`, 'utf8'), Buffer.from(raw, 'utf8')]);
  return 'v1=' + crypto.createHmac('sha256', Buffer.from(key, 'utf8')).update(signed).digest('hex');
}

const tampered = body.replace('"attendance":"unknown"', '"attendance":"attended"');
assert.notEqual(tampered, body, 'the tampered body must differ');

test('fixture is signed exactly like the backend (delivery id, timestamp, raw body; sorted compact JSON)', () => {
  assert.equal(sign(secret, deliveryId, timestamp, body), signature);
  assert.equal(rotationSignature.split(', ')[1], signature);
  assert.equal(JSON.stringify(JSON.parse(body)), body, 'compact JSON');
  assert.deepEqual(Object.keys(JSON.parse(body)), [...Object.keys(JSON.parse(body))].sort(), 'sorted keys');
  assert.equal(fixture.toleranceSeconds, 300);
  assert.equal(JSON.parse(body).delivery_metadata.deliveryId, deliveryId);
});

// ---- Zapier --------------------------------------------------------------------------------

class FakeStore {
  constructor() { this.data = new Map(); this.sets = 0; }
  async get(key) { return this.data.has(key) ? JSON.parse(this.data.get(key)) : null; }
  async set(key, value) { this.sets += 1; this.data.set(key, JSON.stringify(value)); }
}

const zapInput = (over = {}) => ({ raw_body: body, signature, timestamp, delivery_id: deliveryId, secret, ...over });

test('zapier code step: valid delivery passes and lifts the common fields', async () => {
  const out = await zapier.verifyWebinarDelivery(zapInput(), { now });
  assert.equal(out.ok, true);
  assert.equal(out.duplicate, false);
  assert.equal(out.eventType, 'registration.created');
  assert.equal(out.deliveryId, deliveryId);
  assert.equal(out.email, 'ada@example.com');
  assert.equal(out.eventTitle, 'Intro to Widgets');
  assert.equal(out.sessionStartsAt, '2026-03-10T15:00:00Z');
  assert.equal(out.payload.registration.id, '0b7e9d4c-2222-4b8f-8c3a-000000000002');
  // Within the tolerance window on both sides.
  assert.equal((await zapier.verifyWebinarDelivery(zapInput(), { now: now + 300 })).ok, true);
  assert.equal((await zapier.verifyWebinarDelivery(zapInput(), { now: now - 300 })).ok, true);
});

test('zapier code step: rotation header (two signatures) is accepted', async () => {
  const out = await zapier.verifyWebinarDelivery(zapInput({ signature: rotationSignature }), { now });
  assert.equal(out.ok, true);
});

test('zapier code step: tampered body, wrong secret and forged signatures are rejected', async () => {
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput({ raw_body: tampered }), { now }), /Invalid Webinar-Signature/);
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput({ raw_body: body + ' ' }), { now }), /Invalid Webinar-Signature/);
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput({ secret: 'wrong-secret' }), { now }), /Invalid Webinar-Signature/);
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput({ signature: wrongSecretSignature }), { now }), /Invalid Webinar-Signature/);
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput({ signature: 'v1=' }), { now }), /Invalid Webinar-Signature/);
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput({ delivery_id: 'other-id' }), { now }), /Invalid Webinar-Signature/);
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput({ timestamp: String(now + 1) }), { now }), /Invalid Webinar-Signature/);
});

test('zapier code step: stale timestamps and missing inputs are rejected before any HMAC', async () => {
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput(), { now: now + 301 }), /Stale timestamp/);
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput(), { now: now - 301 }), /Stale timestamp/);
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput({ timestamp: 'yesterday' }), { now }), /not Unix seconds/);
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput({ secret: '' }), { now }), /Missing Input Data: secret/);
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput({ raw_body: undefined }), { now }), /raw_body/);
  const neverCalled = { createHmac() { throw new Error('HMAC must not run for stale input'); }, timingSafeEqual: crypto.timingSafeEqual };
  await assert.rejects(zapier.verifyWebinarDelivery(zapInput(), { now: now + 1000, crypto: neverCalled }), /Stale timestamp/);
});

test('zapier code step: duplicate delivery ids are flagged through StoreClient and old ids are pruned', async () => {
  const store = new FakeStore();
  const first = await zapier.verifyWebinarDelivery(zapInput(), { now, store });
  assert.equal(first.duplicate, false);
  const again = await zapier.verifyWebinarDelivery(zapInput(), { now: now + 5, store });
  assert.equal(again.duplicate, true, 'a retry with the same Webinar-Delivery-Id is a duplicate');
  assert.equal(store.sets, 1, 'a duplicate is not written back');
  // A different delivery id is new; entries older than the replay window are pruned.
  const other = '8d3f1c3e-0000-4000-8000-000000000002';
  const ts2 = String(now + 700);
  const out = await zapier.verifyWebinarDelivery(
    zapInput({ delivery_id: other, timestamp: ts2, signature: sign(secret, other, ts2, body) }),
    { now: now + 700, store }
  );
  assert.equal(out.duplicate, false);
  const seen = await store.get(zapier.SEEN_KEY);
  assert.deepEqual(Object.keys(seen), [other], 'the first id (older than 2*300+60 s) was pruned');
  // Stored as a string by an older Storage account: still readable.
  store.data.set(zapier.SEEN_KEY, JSON.stringify(JSON.stringify({ [other]: now + 700 })));
  const again2 = await zapier.verifyWebinarDelivery(
    zapInput({ delivery_id: other, timestamp: ts2, signature: sign(secret, other, ts2, body) }),
    { now: now + 701, store }
  );
  assert.equal(again2.duplicate, true);
});

test('zapier code step source: raw body, constant-time compare, no secrets, Zapier glue', () => {
  const src = read('sdk/kits/automation/zapier/verify-step.js');
  assert.ok(src.includes('timingSafeEqual'), 'constant-time compare');
  assert.ok(src.includes("Buffer.from(rawBody, 'utf8')"), 'HMAC over the raw body bytes');
  assert.ok(src.includes("if (typeof inputData !== 'undefined')"), 'Zapier glue');
  assert.ok(src.includes('Catch Raw Hook'), 'documents the raw-hook requirement');
  assert.ok(!/[0-9a-f]{64}/.test(src), 'no baked-in signature or secret');
  assert.ok(!src.includes(secret), 'fixture secret not in the step');
});

// ---- Make ----------------------------------------------------------------------------------

const makeHeaders = (sig = signature, ts = timestamp, id = deliveryId) => [
  { name: 'Webinar-Delivery-Id', value: id },
  { name: 'Webinar-Timestamp', value: ts },
  { name: 'Webinar-Signature', value: sig },
  { name: 'Content-Type', value: 'application/json; charset=utf-8' },
];

test('make: sha256(text; "hex"; key) is HMAC-SHA256 hex and reproduces the backend signature', () => {
  assert.equal('v1=' + make.sha256(`${deliveryId}.${timestamp}.${body}`, 'hex', secret), signature);
  assert.equal(make.sha256('abc'), crypto.createHash('sha256').update('abc').digest('hex'), 'no key = plain hash');
  assert.equal(make.sha256('abc', 'base64', 'k'), crypto.createHmac('sha256', 'k').update('abc').digest('base64'));
  assert.throws(() => make.sha256('abc', 'binary', 'k'), /unsupported encoding/);
});

test('make scenario simulation: valid, rotation, tampered, wrong secret, stale, duplicate, routing', () => {
  const seen = new Set();
  const run = (over = {}) => make.runScenario({ headers: makeHeaders(), body, secret, now, seen, ...over });
  assert.deepEqual(run(), { accepted: true, reason: 'processed', duplicate: false, eventType: 'registration.created', route: 'registration.created' });
  assert.equal(run({ headers: makeHeaders(rotationSignature), seen: new Set() }).accepted, true);
  assert.equal(run({ body: tampered, seen: new Set() }).reason, 'invalid signature');
  assert.equal(run({ secret: 'wrong-secret', seen: new Set() }).reason, 'invalid signature');
  assert.equal(run({ headers: makeHeaders(wrongSecretSignature), seen: new Set() }).reason, 'invalid signature');
  assert.equal(run({ now: now + 301, seen: new Set() }).reason, 'stale timestamp');
  assert.equal(run({ now: now - 301, seen: new Set() }).reason, 'stale timestamp');
  assert.equal(run({ now: now + 300, seen: new Set() }).accepted, true);
  const dup = run({ now: now + 10 });
  assert.equal(dup.accepted, true);
  assert.equal(dup.duplicate, true, 'the Data store lookup sees the delivery id again');
  // replaceAll: the sorted body carries eventType in delivery_metadata (first) and at the top level.
  const other = body.replaceAll('"eventType":"registration.created"', '"eventType":"recording.published"');
  const otherSig = sign(secret, deliveryId, timestamp, other);
  assert.equal(run({ headers: makeHeaders(otherSig), body: other, seen: new Set() }).route, 'other');
});

test('make blueprint.json carries the simulated expressions and the documented structure', () => {
  const bp = JSON.parse(read('sdk/kits/automation/make/blueprint.json'));
  const text = JSON.stringify(bp);
  for (const [name, expr] of Object.entries(make.EXPRESSIONS)) assert.ok(text.includes(JSON.stringify(expr).slice(1, -1)), `expression ${name}`);
  const byId = {};
  const walk = (flow) => flow.forEach((m) => { byId[m.id] = m; (m.routes || []).forEach((r) => walk(r.flow)); });
  walk(bp.flow);
  assert.equal(byId[1].module, 'gateway:CustomWebHook');
  assert.equal(byId[2].module, 'util:SetVariables');
  assert.equal(byId[3].module, 'util:SetVariable2');
  assert.equal(byId[4].module, 'builtin:BasicRouter');
  assert.equal(byId[7].module, 'json:ParseJSON');
  assert.equal(byId[5].module, 'gateway:WebhookRespond');
  assert.equal(byId[5].mapper.status, '401');
  assert.equal(byId[8].module, 'builtin:BasicRouter');
  const accept = byId[7].filter.conditions[0];
  assert.deepEqual(accept.map((c) => c.o), ['boolean:equal', 'number:lessorequal']);
  assert.equal(accept[1].b, '300');
  assert.deepEqual(byId[5].filter.conditions.map((group) => group[0].o), ['boolean:equal', 'number:greater']);
  assert.deepEqual([9, 10, 11].map((id) => byId[id].filter.conditions[0][0].b), ['registration.created', 'registration.updated', 'attendance.recorded']);
  assert.ok(text.includes('REPLACE_WITH_SIGNING_SECRET') && !text.includes(secret));
  assert.equal(bp.metadata.instant, true);
  assert.ok(bp.metadata.notes.some((n) => /UNVERIFIED/.test(n)), 'blueprint says it is unverified on Make');
});

// ---- n8n -----------------------------------------------------------------------------------

const n8nHeaders = (sig = signature, ts = timestamp, id = deliveryId) => ({
  'Webinar-Delivery-Id': id, 'Webinar-Timestamp': ts, 'Webinar-Signature': sig, 'Content-Type': 'application/json; charset=utf-8',
});

test('n8n workflow simulation: valid, rotation, tampered, wrong secret, stale, duplicate, routing', () => {
  const seen = new Set();
  const run = (over = {}) => n8n.runWorkflow({ headers: n8nHeaders(), rawBody: Buffer.from(body, 'utf8'), secret, now, seen, ...over });
  assert.deepEqual(run(), { verified: true, duplicate: false, output: 'registration.created' });
  assert.equal(run({ headers: n8nHeaders(rotationSignature), seen: new Set() }).verified, true);
  assert.equal(run({ rawBody: tampered, seen: new Set() }).verified, false);
  assert.equal(run({ secret: 'wrong-secret', seen: new Set() }).verified, false);
  assert.equal(run({ headers: n8nHeaders(wrongSecretSignature), seen: new Set() }).verified, false);
  assert.equal(run({ now: now + 301, seen: new Set() }).verified, false);
  assert.equal(run({ now: now - 300, seen: new Set() }).verified, true);
  assert.deepEqual(run({ now: now + 10 }), { verified: true, duplicate: true, output: null });
  const other = body.replaceAll('"eventType":"registration.created"', '"eventType":"reminder.due"');
  assert.equal(run({ headers: n8nHeaders(sign(secret, deliveryId, timestamp, other)), rawBody: other, seen: new Set() }).output, 'other');
  // The Crypto node hashes the UTF-8 string the Extract node produced: identical bytes to the raw body.
  const webhook = n8n.webhookNode({ headers: n8nHeaders(), rawBody: Buffer.from(body, 'utf8') });
  assert.equal(n8n.extractFromFileNode(webhook).json.data, body);
  assert.equal(webhook.json.headers['webinar-signature'], signature, 'headers are lower-cased');
});

test('n8n workflow.json carries the simulated expressions and the documented node chain', () => {
  const wf = JSON.parse(read('sdk/kits/automation/n8n/workflow.json'));
  const text = JSON.stringify(wf);
  for (const [name, expr] of Object.entries(n8n.EXPRESSIONS)) assert.ok(text.includes(JSON.stringify(expr).slice(1, -1)), `expression ${name}`);
  const byName = Object.fromEntries(wf.nodes.map((n) => [n.name, n]));
  assert.equal(byName.Webhook.type, 'n8n-nodes-base.webhook');
  assert.equal(byName.Webhook.parameters.options.rawBody, true, 'raw body on');
  assert.equal(byName.Webhook.parameters.httpMethod, 'POST');
  assert.equal(byName['Extract body'].type, 'n8n-nodes-base.extractFromFile');
  assert.equal(byName.HMAC.type, 'n8n-nodes-base.crypto');
  assert.deepEqual([byName.HMAC.parameters.action, byName.HMAC.parameters.type, byName.HMAC.parameters.encoding], ['hmac', 'SHA256', 'hex']);
  assert.ok(byName.HMAC.credentials.crypto, 'secret comes from a Crypto credential');
  assert.ok(!('secret' in byName.HMAC.parameters), 'no secret in the workflow file');
  const cond = byName['Signature valid and fresh'].parameters.conditions;
  assert.equal(cond.combinator, 'and');
  assert.deepEqual(cond.conditions.map((c) => c.operator.operation), ['contains', 'lte']);
  assert.equal(cond.conditions[1].rightValue, 300);
  assert.equal(byName['Drop repeated delivery ids'].type, 'n8n-nodes-base.removeDuplicates');
  assert.equal(byName['Route by eventType'].type, 'n8n-nodes-base.switch');
  assert.deepEqual(byName['Route by eventType'].parameters.rules.values.map((r) => r.outputKey), n8n.EVENT_TYPES);
  const chain = ['Webhook', 'Extract body', 'HMAC', 'Signature valid and fresh', 'Drop repeated delivery ids'];
  for (let i = 0; i < chain.length - 1; i += 1) assert.equal(wf.connections[chain[i]].main[0][0].node, chain[i + 1], chain[i]);
  assert.equal(wf.connections['Drop repeated delivery ids'].main[0][0].node, 'Route by eventType');
  assert.deepEqual(wf.connections['Signature valid and fresh'].main[1], [], 'the false branch goes nowhere');
  assert.ok(!text.includes(secret));
  assert.ok(/UNVERIFIED/.test(wf.meta.note));
});

test('recipes and docs stay consistent with the platform contract', () => {
  const webhooks = read('docs/webhooks.md');
  assert.ok(webhooks.includes('## Workflow automation platforms'));
  for (const p of ['sdk/kits/automation/zapier/', 'sdk/kits/automation/make/', 'sdk/kits/automation/n8n/']) assert.ok(webhooks.includes(p), p);
  const manifest = JSON.parse(read('sdk/kits/manifest.json'));
  for (const id of ['zapier', 'make', 'n8n']) {
    const kit = manifest.kits.find((k) => k.id === id);
    assert.ok(kit, id);
    assert.equal(kit.productionTested, false);
    assert.ok(/Node simulation/.test(kit.verified.summary), `${id}: verified note names the Node simulation`);
    assert.ok(kit.files.includes(`sdk/kits/automation/${id}/README.md`));
  }
});
