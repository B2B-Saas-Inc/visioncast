'use strict';
/*
 * Node simulation of workflow.json: each node's documented behaviour, with Node's crypto.
 * The test in ../test/automation.test.js asserts that workflow.json carries exactly the
 * EXPRESSIONS below, so the simulation and the importable workflow cannot drift apart.
 * This proves the algorithm, not n8n: the workflow has not been run on an n8n instance.
 *
 * Node behaviour relied on (docs.n8n.io and n8n source, 2026-10-27):
 *   Webhook (Raw Body on): request body kept as binary property "data"; headers in $json.headers.
 *   Extract from File (text): binary "data" -> text in $json.data.
 *   Crypto (Hmac, SHA256, hex): createHmac(type, hmacSecret).update(value).digest('hex'),
 *     secret from a Crypto credential, written to $json.<Property Name>.
 *   IF: both conditions must hold. Remove Duplicates: items whose dedupe value was seen in a
 *     previous execution are dropped. Switch: first matching rule, else the fallback output.
 */
const crypto = require('node:crypto');

const TOLERANCE_SECONDS = 300;
const EVENT_TYPES = ['registration.created', 'registration.updated', 'attendance.recorded'];

/** Expressions as stored in workflow.json (n8n expression syntax, "=" prefix). */
const EXPRESSIONS = {
  hmacValue: "={{ $('Webhook').item.json.headers['webinar-delivery-id'] }}.{{ $('Webhook').item.json.headers['webinar-timestamp'] }}.{{ $json.data }}",
  signatureHeader: "={{ $('Webhook').item.json.headers['webinar-signature'] }}",
  expectedSignature: '=v1={{ $json.signature }}',
  age: "={{ Math.abs(Math.floor(Date.now() / 1000) - Number($('Webhook').item.json.headers['webinar-timestamp'])) }}",
  dedupeValue: "={{ $('Webhook').item.json.headers['webinar-delivery-id'] }}",
  eventType: "={{ JSON.parse($('Extract body').item.json.data).eventType }}",
};

/** The Webhook node: headers are lower-cased by the HTTP server; the raw body becomes binary. */
function webhookNode(request) {
  const headers = {};
  for (const [k, v] of Object.entries(request.headers || {})) headers[k.toLowerCase()] = String(v);
  const raw = Buffer.isBuffer(request.rawBody) ? request.rawBody : Buffer.from(String(request.rawBody), 'utf8');
  return { json: { headers, params: {}, query: {} }, binary: { data: { data: raw.toString('base64'), mimeType: 'application/json' } } };
}

function extractFromFileNode(item) {
  return { json: { data: Buffer.from(item.binary.data.data, 'base64').toString('utf8') }, binary: item.binary };
}

function cryptoHmacNode(item, webhookItem, secret) {
  const h = webhookItem.json.headers;
  const value = `${h['webinar-delivery-id']}.${h['webinar-timestamp']}.${item.json.data}`;
  const signature = crypto.createHmac('sha256', Buffer.from(secret, 'utf8')).update(value).digest('hex');
  return { json: { ...item.json, signature }, binary: item.binary };
}

function ifNode(item, webhookItem, now, tolerance) {
  const h = webhookItem.json.headers;
  const signatureOk = String(h['webinar-signature'] || '').includes('v1=' + item.json.signature);
  const age = Math.abs(now - Number(h['webinar-timestamp']));
  return signatureOk && Number.isFinite(age) && age <= tolerance;
}

function removeDuplicatesNode(webhookItem, seen) {
  const id = webhookItem.json.headers['webinar-delivery-id'];
  if (seen.has(id)) return false;
  seen.add(id);
  return true;
}

function switchNode(extracted) {
  const eventType = JSON.parse(extracted.json.data).eventType;
  return EVENT_TYPES.includes(eventType) ? eventType : 'other';
}

/**
 * Run one request through the workflow.
 * request: { headers: {name: value}, rawBody: Buffer|string, secret, now, tolerance?, seen?: Set }
 * returns: { verified, duplicate, output }  (output = Switch output name, or null)
 */
function runWorkflow(request) {
  const tolerance = request.tolerance === undefined ? TOLERANCE_SECONDS : request.tolerance;
  const seen = request.seen || new Set();
  const webhook = webhookNode(request);
  const extracted = extractFromFileNode(webhook);
  const signed = cryptoHmacNode(extracted, webhook, request.secret);
  if (!ifNode(signed, webhook, request.now, tolerance)) return { verified: false, duplicate: false, output: null };
  if (!removeDuplicatesNode(webhook, seen)) return { verified: true, duplicate: true, output: null };
  return { verified: true, duplicate: false, output: switchNode(extracted) };
}

module.exports = { EXPRESSIONS, EVENT_TYPES, TOLERANCE_SECONDS, runWorkflow, webhookNode, extractFromFileNode, cryptoHmacNode, ifNode, removeDuplicatesNode, switchNode };
