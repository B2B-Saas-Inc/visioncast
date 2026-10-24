'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const {
  loadConfig, createProxyServer, rewriteSetCookie, parseCookies, projectMyRegistration, webCalendarLink, CALENDAR_WEB_LINKS,
} = require('../proxy/server.js');

const KEY = 'k'.repeat(40);
const ORIGIN = 'https://www.example.com';
const CSRF = 'A'.repeat(43);

function listen(server) {
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
}

function close(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

function request(port, { method = 'GET', path, headers = {}, body }) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path, headers }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let json = null;
        try { json = text ? JSON.parse(text) : null; } catch (e) { json = null; }
        resolve({ status: res.statusCode, headers: res.headers, text, json });
      });
    });
    req.on('error', reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

async function setup(t, backendImpl) {
  const seen = [];
  const backend = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const record = { method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString('utf8') };
      seen.push(record);
      backendImpl(record, res);
    });
  });
  const backendPort = await listen(backend);
  const config = loadConfig({
    PROXY_BACKEND_URL: `http://127.0.0.1:${backendPort}`,
    PROXY_ALLOW_INSECURE_BACKEND: '1',
    PROXY_API_KEY: KEY,
    PROXY_ALLOWED_ORIGINS: ORIGIN,
    PROXY_BODY_LIMIT: '2048',
    PROXY_TIMEOUT_MS: '2000',
  });
  const proxy = createProxyServer(config);
  const port = await listen(proxy);
  t.after(async () => {
    await close(proxy);
    await close(backend);
  });
  return { port, seen };
}

function json(res, status, body, extra = {}) {
  res.writeHead(status, { 'content-type': 'application/json', ...extra });
  res.end(JSON.stringify(body));
}

const okBackend = (rec, res) => {
  if (rec.url.startsWith('/api/v1/profile')) {
    return json(res, 200, { recognized: true, missingFields: ['company'], formVersion: 'v1', email: 'leak@example.com', firstName: 'Leak' });
  }
  if (rec.url === '/api/v1/registrations') {
    return json(res, 201, { status: 'registered', thankYou: { message: 'Thanks' } }, {
      'set-cookie': [
        'webinar_session=abc123; Domain=evil.example; Path=/api; Max-Age=3600; SameSite=None',
        'tracking=1; Path=/',
      ],
    });
  }
  if (rec.url === '/api/v1/reset') {
    return json(res, 200, { status: 'reset' }, { 'set-cookie': 'webinar_session=; Max-Age=0; Path=/' });
  }
  json(res, 404, { error: 'nope' });
};

const postHeaders = (extra = {}) => ({
  origin: ORIGIN,
  'content-type': 'application/json',
  'x-webinar-csrf': CSRF,
  cookie: `__Host-webinar_csrf=${CSRF}; webinar_session=sess1; other=secret`,
  ...extra,
});

const validBody = JSON.stringify({ schemaVersion: '1', occurrenceId: 'occ_1', formVersion: 'v1', idempotencyKey: 'abc', profile: { email: 'a@example.com' } });

test('loadConfig fails closed', () => {
  assert.throws(() => loadConfig({}), /PROXY_BACKEND_URL/);
  assert.throws(() => loadConfig({ PROXY_BACKEND_URL: 'http://backend', PROXY_API_KEY: KEY, PROXY_ALLOWED_ORIGINS: ORIGIN }), /https/);
  assert.throws(() => loadConfig({ PROXY_BACKEND_URL: 'https://backend', PROXY_API_KEY: 'short', PROXY_ALLOWED_ORIGINS: ORIGIN }), /PROXY_API_KEY/);
  assert.throws(() => loadConfig({ PROXY_BACKEND_URL: 'https://backend', PROXY_API_KEY: KEY, PROXY_ALLOWED_ORIGINS: 'https://www.example.com/path' }), /exact/);
  assert.throws(() => loadConfig({ PROXY_BACKEND_URL: 'https://backend/api', PROXY_API_KEY: KEY, PROXY_ALLOWED_ORIGINS: ORIGIN }), /origin only/);
  assert.throws(() => loadConfig({ PROXY_BACKEND_URL: 'https://backend', PROXY_API_KEY: KEY, PROXY_ALLOWED_ORIGINS: 'http://www.example.com' }), /exact https origin/);
  const c = loadConfig({ PROXY_BACKEND_URL: 'https://backend.internal', PROXY_API_KEY: KEY, PROXY_ALLOWED_ORIGINS: ORIGIN });
  assert.equal(c.backendOrigin, 'https://backend.internal');
});

test('fixed path allowlist and methods', async (t) => {
  const { port, seen } = await setup(t, okBackend);
  for (const path of ['/', '/api/v1/profile', '/webinar-api/profile/extra', '/webinar-api/../api/v1/profile', '/webinar-api/%2e%2e/api', '/webinar-api/Profile']) {
    const r = await request(port, { path });
    assert.equal(r.status, 404, path);
  }
  const r = await request(port, { method: 'POST', path: '/webinar-api/profile?occurrenceId=a&formVersion=b', headers: postHeaders(), body: '{}' });
  assert.equal(r.status, 405);
  assert.equal(r.headers.allow, 'GET');
  const r2 = await request(port, { method: 'GET', path: '/webinar-api/registrations' });
  assert.equal(r2.status, 405);
  assert.equal(seen.length, 0);
});

test('profile: validates query, forwards key and only the session cookie, projects response, sets CSRF cookie', async (t) => {
  const { port, seen } = await setup(t, okBackend);
  const bad = await request(port, { path: '/webinar-api/profile?occurrenceId=a' });
  assert.equal(bad.status, 400);
  const bad2 = await request(port, { path: '/webinar-api/profile?occurrenceId=%3Cx%3E&formVersion=v1' });
  assert.equal(bad2.status, 400);

  const r = await request(port, {
    path: '/webinar-api/profile?occurrenceId=occ_1&formVersion=v1&email=x@example.com',
    headers: { cookie: 'webinar_session=sess1; _ga=GA1.1.1.2; other=secret', authorization: 'Bearer attacker', 'x-forwarded-for': '6.6.6.6' },
  });
  assert.equal(r.status, 200);
  assert.deepEqual(r.json, { recognized: true, missingFields: ['company'], formVersion: 'v1' });
  assert.equal(r.headers['cache-control'], 'no-store');
  const setCookie = [].concat(r.headers['set-cookie'] || []);
  assert.equal(setCookie.length, 1);
  assert.match(setCookie[0], /^__Host-webinar_csrf=[A-Za-z0-9_-]{43}; Path=\/; Secure; SameSite=Strict/);

  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, '/api/v1/profile?occurrenceId=occ_1&formVersion=v1');
  assert.equal(seen[0].headers.authorization, `Bearer ${KEY}`);
  assert.equal(seen[0].headers.cookie, 'webinar_session=sess1');
  assert.notEqual(seen[0].headers['x-forwarded-for'], '6.6.6.6');
});

test('profile: cross-origin and cross-site requests rejected', async (t) => {
  const { port, seen } = await setup(t, okBackend);
  const a = await request(port, { path: '/webinar-api/profile?occurrenceId=o&formVersion=v', headers: { origin: 'https://evil.example' } });
  assert.equal(a.status, 403);
  const b = await request(port, { path: '/webinar-api/profile?occurrenceId=o&formVersion=v', headers: { 'sec-fetch-site': 'cross-site' } });
  assert.equal(b.status, 403);
  const c = await request(port, { path: '/webinar-api/profile?occurrenceId=o&formVersion=v', headers: { 'sec-fetch-site': 'same-site', origin: ORIGIN } });
  assert.equal(c.status, 403);
  assert.equal(seen.length, 0);
});

test('profile: existing valid CSRF cookie is not rotated', async (t) => {
  const { port } = await setup(t, okBackend);
  const r = await request(port, { path: '/webinar-api/profile?occurrenceId=o&formVersion=v', headers: { cookie: `__Host-webinar_csrf=${CSRF}` } });
  assert.equal(r.status, 200);
  assert.equal(r.headers['set-cookie'], undefined);
});

test('POST: origin, CSRF, content type, size and JSON checks', async (t) => {
  const { port, seen } = await setup(t, okBackend);
  const path = '/webinar-api/registrations';
  const cases = [
    [{ origin: undefined }, 403],
    [{ origin: 'https://evil.example' }, 403],
    [{ origin: 'null' }, 403],
    [{ 'x-webinar-csrf': undefined }, 403],
    [{ 'x-webinar-csrf': 'B'.repeat(43) }, 403],
    [{ cookie: 'webinar_session=sess1' }, 403],
    [{ 'content-type': 'text/plain' }, 415],
    [{ 'content-type': 'application/x-www-form-urlencoded' }, 415],
  ];
  for (const [override, status] of cases) {
    const headers = postHeaders();
    for (const [k, v] of Object.entries(override)) {
      if (v === undefined) delete headers[k];
      else headers[k] = v;
    }
    const r = await request(port, { method: 'POST', path, headers, body: validBody });
    assert.equal(r.status, status, JSON.stringify(override));
  }
  const big = await request(port, { method: 'POST', path, headers: postHeaders(), body: JSON.stringify({ schemaVersion: '1', pad: 'x'.repeat(5000) }) });
  assert.equal(big.status, 413);
  const notJson = await request(port, { method: 'POST', path, headers: postHeaders(), body: '{nope' });
  assert.equal(notJson.status, 400);
  const arr = await request(port, { method: 'POST', path, headers: postHeaders(), body: '[]' });
  assert.equal(arr.status, 400);
  const wrongSchema = await request(port, { method: 'POST', path, headers: postHeaders(), body: '{"schemaVersion":"2"}' });
  assert.equal(wrongSchema.status, 400);
  assert.equal(seen.length, 0);
});

test('POST registrations: forwards body and rewrites only the session cookie', async (t) => {
  const { port, seen } = await setup(t, okBackend);
  const r = await request(port, { method: 'POST', path: '/webinar-api/registrations', headers: postHeaders(), body: validBody });
  assert.equal(r.status, 201);
  assert.deepEqual(r.json, { status: 'registered', thankYou: { message: 'Thanks' } });
  const cookies = [].concat(r.headers['set-cookie'] || []);
  assert.equal(cookies.length, 1);
  assert.equal(cookies[0], 'webinar_session=abc123; Path=/; Max-Age=3600; HttpOnly; Secure; SameSite=Lax');
  assert.equal(seen[0].method, 'POST');
  assert.equal(seen[0].url, '/api/v1/registrations');
  assert.deepEqual(JSON.parse(seen[0].body), JSON.parse(validBody));
  assert.equal(seen[0].headers.cookie, 'webinar_session=sess1');
  assert.equal(seen[0].headers['x-webinar-csrf'], undefined);
  assert.equal(seen[0].headers.authorization, `Bearer ${KEY}`);
});

test('POST reset: accepts empty body and clears session cookie', async (t) => {
  const { port, seen } = await setup(t, okBackend);
  const headers = postHeaders();
  delete headers['content-type'];
  headers['content-length'] = '0';
  const r = await request(port, { method: 'POST', path: '/webinar-api/reset', headers, body: '' });
  assert.equal(r.status, 200);
  assert.equal(seen[0].url, '/api/v1/reset');
  assert.equal(seen[0].body, '{}');
  assert.match([].concat(r.headers['set-cookie'])[0], /^webinar_session=; Path=\/; Max-Age=0; HttpOnly; Secure; SameSite=Lax$/);
});

test('upstream 4xx passes through, 5xx/non-JSON/unreachable become 502', async (t) => {
  let mode = '422';
  const { port } = await setup(t, (rec, res) => {
    if (mode === '422') return json(res, 422, { error: 'validation_failed', message: 'ada@example.com already belongs to Ada', errors: { email: ['ada@example.com is taken'] } });
    if (mode === '500') return json(res, 500, { trace: 'secret stack' });
    res.writeHead(200, { 'content-type': 'text/html' });
    res.end('<html>');
  });
  const send = () => request(port, { method: 'POST', path: '/webinar-api/registrations', headers: postHeaders(), body: validBody });
  let r = await send();
  assert.equal(r.status, 422);
  // Error bodies are projected: code + field keys only, no server text or PII.
  assert.deepEqual(r.json, { error: { code: 'validation_failed' }, fieldErrors: { email: 'invalid' } });
  mode = '500';
  r = await send();
  assert.equal(r.status, 502);
  assert.deepEqual(r.json, { error: 'bad_gateway' });
  mode = 'html';
  r = await send();
  assert.equal(r.status, 502);
});

// Regression (B2): the real backend shape from webinars/registration.py error_body().
test('backend field errors survive projection without server text', async (t) => {
  const backendBody = {
    error: { code: 'validation_error', message: 'Some required fields are missing.', fields: { firstName: 'Required.', 'consents.sms.disclosureVersion': 'Does not match' } },
    fieldErrors: { firstName: 'Required.', 'consents.sms.disclosureVersion': 'Does not match' },
  };
  const { port } = await setup(t, (rec, res) => json(res, 400, backendBody));
  const r = await request(port, { method: 'POST', path: '/webinar-api/registrations', headers: postHeaders(), body: validBody });
  assert.equal(r.status, 400);
  assert.deepEqual(r.json, {
    error: { code: 'validation_error' },
    fieldErrors: { firstName: 'invalid', 'consents.sms.disclosureVersion': 'invalid' },
  });
  assert.doesNotMatch(r.text, /Required|missing|Does not match/);
  // The SDK reads the projected body and highlights the matching controls.
  const sdk = require('../src/webinar-form.js');
  assert.deepEqual(sdk._internal.errorFieldKeys(r.json), ['firstName', 'consents.sms']);
  assert.deepEqual(sdk._internal.errorFieldKeys(backendBody), ['firstName', 'consents.sms']);
});

const OCC = '7f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f';
const GOOGLE = 'https://calendar.google.com/calendar/render?action=TEMPLATE&text=Demo&dates=20261101T150000Z%2F20261101T160000Z';
const OUTLOOK = 'https://outlook.live.com/calendar/0/deeplink/compose?path=/calendar/action/compose&rru=addevent&subject=Demo';
const OFFICE365 = 'https://outlook.office.com/calendar/0/deeplink/compose?path=/calendar/action/compose&rru=addevent&subject=Demo';

// Regression (B3): the backend's relative icsUrl is unreachable on the marketing origin.
test('registration icsUrl is rewritten to the proxy calendar route', async (t) => {
  const { port } = await setup(t, (rec, res) => json(res, 201, {
    status: 'registered',
    calendar: { title: 'T', icsUrl: `/api/v1/occurrences/${OCC}/calendar.ics`, uid: 'u', sequence: 0 },
  }));
  const r = await request(port, { method: 'POST', path: '/webinar-api/registrations', headers: postHeaders(), body: validBody });
  assert.equal(r.status, 201);
  assert.equal(r.json.calendar.icsUrl, `/webinar-api/calendar.ics?occurrenceId=${OCC}`);
  assert.equal(r.json.calendar.uid, 'u');
});

test('registration icsUrl with an unexpected shape is dropped', async (t) => {
  const { port } = await setup(t, (rec, res) => json(res, 201, { status: 'registered', calendar: { icsUrl: 'https://evil.example/x.ics' } }));
  const r = await request(port, { method: 'POST', path: '/webinar-api/registrations', headers: postHeaders(), body: validBody });
  assert.equal(r.json.calendar.icsUrl, undefined);
});

test('calendar route: fixed upstream path, UUID only, no key or cookies forwarded', async (t) => {
  const { port, seen } = await setup(t, (rec, res) => {
    if (rec.url === `/api/v1/occurrences/${OCC}/calendar.ics`) {
      res.writeHead(200, { 'content-type': 'text/calendar; charset=utf-8', 'content-disposition': 'attachment; filename="../../evil.ics"','set-cookie': 'webinar_session=zzz' });
      return res.end('BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n');
    }
    json(res, 404, { error: { code: 'not_found', message: 'Occurrence not found.' } });
  });
  const r = await request(port, { path: `/webinar-api/calendar.ics?occurrenceId=${OCC}`, headers: { cookie: 'webinar_session=sess1', 'sec-fetch-site': 'same-origin' } });
  assert.equal(r.status, 200);
  assert.match(r.headers['content-type'], /^text\/calendar/);
  assert.equal(r.headers['content-disposition'], 'attachment; filename="webinar.ics"');
  assert.equal(r.headers['set-cookie'], undefined);
  assert.match(r.text, /^BEGIN:VCALENDAR/);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, `/api/v1/occurrences/${OCC}/calendar.ics`);
  assert.equal(seen[0].headers.authorization, undefined);
  assert.equal(seen[0].headers.cookie, undefined);

  for (const q of ['', '?occurrenceId=../../api/v1/profile', `?occurrenceId=${OCC}&x=1`, '?occurrenceId=occ_1', `?occurrenceId=${OCC}%2F..`]) {
    const bad = await request(port, { path: `/webinar-api/calendar.ics${q}` });
    assert.equal(bad.status, 400, q);
  }
  const post = await request(port, { method: 'POST', path: '/webinar-api/calendar.ics', headers: postHeaders(), body: '{}' });
  assert.equal(post.status, 405);
  const cross = await request(port, { path: `/webinar-api/calendar.ics?occurrenceId=${OCC}`, headers: { 'sec-fetch-site': 'cross-site' } });
  assert.equal(cross.status, 403);
  const missing = await request(port, { path: '/webinar-api/calendar.ics?occurrenceId=00000000-0000-4000-8000-000000000000' });
  assert.equal(missing.status, 404);
  assert.equal(seen.length, 2);
});

test('my-registration: CSRF token, UUID-only query, forwards key and session, projects body', async (t) => {
  const { port, seen } = await setup(t, (rec, res) => json(res, 200, {
    occurrenceId: OCC, recognized: true, registered: true, status: 'registered', email: 'leak@example.com',
    personId: 'p1', thankYou: { mode: 'message', message: 'Thanks' },
    calendar: { icsUrl: `/api/v1/occurrences/${OCC}/calendar.ics`, uid: 'u', googleUrl: GOOGLE },
  }));
  const path = `/webinar-api/my-registration?occurrenceId=${OCC.toUpperCase()}`;
  const getHeaders = { 'x-webinar-csrf': CSRF, cookie: `__Host-webinar_csrf=${CSRF}; webinar_session=sess1; other=secret` };
  const r = await request(port, { path, headers: getHeaders });
  assert.equal(r.status, 200);
  assert.deepEqual(r.json, {
    occurrenceId: OCC, recognized: true, registered: true, status: 'registered',
    thankYou: { mode: 'message', message: 'Thanks' },
    calendar: { icsUrl: `/webinar-api/calendar.ics?occurrenceId=${OCC}`, uid: 'u', googleUrl: GOOGLE, outlookUrl: null, office365Url: null },
  });
  assert.equal(r.headers['cache-control'], 'no-store');
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, `/api/v1/my-registration?occurrenceId=${OCC}`);
  assert.equal(seen[0].headers.authorization, `Bearer ${KEY}`);
  assert.equal(seen[0].headers.cookie, 'webinar_session=sess1');

  const rejected = [
    [{ cookie: 'webinar_session=sess1' }, 403], // no CSRF cookie/header
    [{ 'x-webinar-csrf': 'B'.repeat(43), cookie: `__Host-webinar_csrf=${CSRF}` }, 403],
    [{ ...getHeaders, origin: 'https://evil.example' }, 403],
    [{ ...getHeaders, 'sec-fetch-site': 'cross-site' }, 403],
  ];
  for (const [headers, status] of rejected) {
    const bad = await request(port, { path, headers });
    assert.equal(bad.status, status, JSON.stringify(headers));
  }
  for (const q of ['', '?occurrenceId=occ_1', `?occurrenceId=${OCC}&email=a@example.com`, `?occurrenceId=${OCC}&occurrenceId=${OCC}`]) {
    const bad = await request(port, { path: `/webinar-api/my-registration${q}`, headers: getHeaders });
    assert.equal(bad.status, 400, q);
  }
  const post = await request(port, { method: 'POST', path: '/webinar-api/my-registration', headers: postHeaders(), body: '{}' });
  assert.equal(post.status, 405);
  assert.equal(seen.length, 1);
});

test('my-registration: unregistered body carries no thank-you or calendar', async (t) => {
  const { port } = await setup(t, (rec, res) => json(res, 200, {
    occurrenceId: OCC, recognized: false, registered: false, status: null, calendar: { icsUrl: 'x' },
  }));
  const r = await request(port, {
    path: `/webinar-api/my-registration?occurrenceId=${OCC}`,
    headers: { 'x-webinar-csrf': CSRF, cookie: `__Host-webinar_csrf=${CSRF}` },
  });
  assert.deepEqual(r.json, { occurrenceId: OCC, recognized: false, registered: false, status: null });
});

// Regression (review F2): nested thankYou/calendar objects are allowlisted, not copied whole.
test('my-registration: nested thankYou/calendar keys are allowlisted; PII and unknown keys dropped', async (t) => {
  const { port } = await setup(t, (rec, res) => json(res, 200, {
    occurrenceId: OCC, recognized: true, registered: true, status: 'registered',
    thankYou: { mode: 'message', message: 'Thanks', email: 'ada@example.com', joinUrl: 'https://zoom.example/j/1' },
    calendar: {
      title: 'Demo', startsAt: '2026-11-01T15:00:00Z', endsAt: '2026-11-01T16:00:00Z', timezone: 'Europe/London',
      icsUrl: `/api/v1/occurrences/${OCC}/calendar.ics`, googleUrl: GOOGLE, outlookUrl: OUTLOOK, office365Url: OFFICE365,
      uid: 'u@example', sequence: 2,
      joinUrl: 'https://zoom.example/j/1', waitingRoomUrl: 'https://w.example/w/abc', attendee: { email: 'ada@example.com' },
      description: 'Hi Ada',
    },
  }));
  const r = await request(port, {
    path: `/webinar-api/my-registration?occurrenceId=${OCC}`,
    headers: { 'x-webinar-csrf': CSRF, cookie: `__Host-webinar_csrf=${CSRF}; webinar_session=sess1` },
  });
  assert.equal(r.status, 200);
  assert.deepEqual(r.json, {
    occurrenceId: OCC, recognized: true, registered: true, status: 'registered',
    thankYou: { mode: 'message', message: 'Thanks' },
    calendar: {
      title: 'Demo', startsAt: '2026-11-01T15:00:00Z', endsAt: '2026-11-01T16:00:00Z', timezone: 'Europe/London',
      // Existing ICS behaviour: relative backend path rewritten to the proxy route.
      icsUrl: `/webinar-api/calendar.ics?occurrenceId=${OCC}`,
      googleUrl: GOOGLE, outlookUrl: OUTLOOK, office365Url: OFFICE365, uid: 'u@example', sequence: 2,
    },
  });
  assert.doesNotMatch(r.text, /ada@example\.com|zoom\.example|w\.example|Hi Ada/);
});

test('projectMyRegistration: malicious or foreign calendar and redirect URLs are dropped', () => {
  const malicious = [
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)//https://calendar.google.com/calendar/render',
    'data:text/html,<script>alert(1)</script>',
    'vbscript:msgbox(1)',
    'http://calendar.google.com/calendar/render?x=1',
    'https://calendar.google.com.evil.example/calendar/render',
    'https://evil.example/calendar/render',
    'https://user:pw@calendar.google.com/calendar/render',
    'https://calendar.google.com:8443/calendar/render',
    'https://calendar.google.com/calendar/render#frag',
    'https://calendar.google.com/calendar/other',
    '//calendar.google.com/calendar/render',
    '/calendar/render',
    `https://calendar.google.com/calendar/render?${'a'.repeat(9000)}`,
    42,
    { href: GOOGLE },
    ['x'],
  ];
  for (const bad of malicious) {
    const out = projectMyRegistration({
      occurrenceId: OCC, recognized: true, registered: true, status: 'registered',
      thankYou: { mode: 'redirect', redirectUrl: bad },
      calendar: { googleUrl: bad, outlookUrl: bad, office365Url: bad, icsUrl: bad },
    });
    assert.deepEqual(out.calendar, { googleUrl: null, outlookUrl: null, office365Url: null }, String(bad));
    // Thank-you redirects may be any https URL (backend-validated); every other scheme is dropped.
    if (!(typeof bad === 'string' && /^https:\/\//i.test(bad) && bad.length <= 2048)) {
      assert.deepEqual(out.thankYou, { mode: 'redirect' }, String(bad));
    }
  }
  // Each link must match its own service: a Google URL in outlookUrl is dropped.
  const swapped = projectMyRegistration({ registered: true, calendar: { outlookUrl: GOOGLE, office365Url: OUTLOOK, googleUrl: OFFICE365 } });
  assert.deepEqual(swapped.calendar, { googleUrl: null, outlookUrl: null, office365Url: null });
  // Non-object nested values, unknown modes and bad sequence values.
  const odd = projectMyRegistration({ registered: true, thankYou: { mode: 'eval', message: { x: 1 } }, calendar: { sequence: -1, title: ['x'] } });
  assert.deepEqual(odd.thankYou, { mode: 'message' });
  assert.deepEqual(odd.calendar, { googleUrl: null, outlookUrl: null, office365Url: null });
  assert.equal(projectMyRegistration({ registered: true, thankYou: ['x'], calendar: 'x' }).calendar, undefined);
  assert.equal(webCalendarLink(GOOGLE, CALENDAR_WEB_LINKS.googleUrl), GOOGLE);
  const redirect = projectMyRegistration({ registered: true, thankYou: { mode: 'redirect', redirectUrl: 'https://www.example.com/thanks' } });
  assert.deepEqual(redirect.thankYou, { mode: 'redirect', redirectUrl: 'https://www.example.com/thanks' });
});

test('unreachable backend returns 502', async (t) => {
  const dead = http.createServer();
  const deadPort = await listen(dead);
  await close(dead);
  const config = loadConfig({
    PROXY_BACKEND_URL: `http://127.0.0.1:${deadPort}`,
    PROXY_ALLOW_INSECURE_BACKEND: '1',
    PROXY_API_KEY: KEY,
    PROXY_ALLOWED_ORIGINS: ORIGIN,
  });
  const proxy = createProxyServer(config);
  const port = await listen(proxy);
  t.after(() => close(proxy));
  const r = await request(port, { path: '/webinar-api/profile?occurrenceId=o&formVersion=v' });
  assert.equal(r.status, 502);
});

test('cookie helpers', () => {
  const config = { sessionCookieName: 'webinar_session' };
  assert.equal(rewriteSetCookie('other=1; Path=/', config), null);
  assert.equal(rewriteSetCookie('webinar_session=a"b; Path=/', config), null);
  assert.equal(rewriteSetCookie('webinar_session=v; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Domain=x', config),
    'webinar_session=v; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT; HttpOnly; Secure; SameSite=Lax');
  const parsed = parseCookies('a=1; a=2; bad name=3; b=x=y');
  assert.equal(parsed.get('a'), '1');
  assert.equal(parsed.get('b'), 'x=y');
  assert.equal(parsed.has('bad name'), false);
});
