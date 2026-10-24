'use strict';
/*
 * In-memory stand-in for the webinar backend, for the local demo only.
 * It mimics the contract shape; it is NOT the real backend and stores
 * nothing durably. Sessions are opaque random tokens, hashed in memory.
 */
const http = require('node:http');
const crypto = require('node:crypto');

const REQUIRED = ['email', 'firstName', 'lastName', 'company', 'jobTitle'];

function createMockBackend({ proxyKey, sessionCookieName = 'webinar_session' }) {
  const people = new Map(); // sessionHash -> profile
  const idempotency = new Map(); // key -> body fingerprint
  const hash = (v) => crypto.createHash('sha256').update(v).digest('hex');

  function session(req) {
    const m = String(req.headers.cookie || '').match(new RegExp(`(?:^|; )${sessionCookieName}=([^;]+)`));
    return m ? hash(m[1]) : null;
  }

  function send(res, status, body, cookie) {
    const headers = { 'content-type': 'application/json' };
    if (cookie) headers['set-cookie'] = cookie;
    res.writeHead(status, headers);
    res.end(JSON.stringify(body));
  }

  return http.createServer((req, res) => {
    if (req.headers.authorization !== `Bearer ${proxyKey}`) return send(res, 401, { error: 'unauthorized' });
    const url = new URL(req.url, 'http://backend.invalid');
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const sid = session(req);
      const known = sid && people.has(sid) ? people.get(sid) : null;
      if (req.method === 'GET' && url.pathname === '/api/v1/profile') {
        const formVersion = url.searchParams.get('formVersion') || '';
        if (!known) return send(res, 200, { recognized: false, missingFields: REQUIRED.slice(), formVersion });
        return send(res, 200, { recognized: true, missingFields: REQUIRED.filter((f) => !known[f]), formVersion });
      }
      if (req.method === 'POST' && url.pathname === '/api/v1/reset') {
        if (sid) people.delete(sid);
        return send(res, 200, { status: 'reset' }, `${sessionCookieName}=; Max-Age=0; Path=/`);
      }
      if (req.method === 'POST' && url.pathname === '/api/v1/registrations') {
        let body;
        try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch (e) { return send(res, 400, { error: 'invalid_json' }); }
        const fingerprint = hash(JSON.stringify(body));
        const prior = idempotency.get(body.idempotencyKey);
        if (prior && prior !== fingerprint) return send(res, 409, { error: 'idempotency_conflict' });
        idempotency.set(body.idempotencyKey, fingerprint);
        const profile = body.profile || {};
        if (!known && !profile.email) return send(res, 422, { errors: { email: ['required'] } });
        let cookie = null;
        if (known) {
          // Valid capability: update only this profile; blanks never erase.
          for (const [k, v] of Object.entries(profile)) if (v) known[k] = v;
        } else {
          // Unknown browser always gets a NEW person; matching email never rebinds identity.
          const token = crypto.randomBytes(32).toString('base64url');
          people.set(hash(token), { ...profile });
          cookie = `${sessionCookieName}=${token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=31536000`;
        }
        return send(res, 201, {
          status: 'registered',
          thankYou: { message: 'Thanks — your RSVP is confirmed. Check your inbox for details.' },
          calendar: { icsUrl: null },
        }, cookie);
      }
      send(res, 404, { error: 'not_found' });
    });
  });
}

module.exports = { createMockBackend };
