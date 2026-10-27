import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  checkCsrf,
  checkOrigin,
  clientIp,
  isJsonContentType,
  parseCookies,
  parseProxyConfig,
  profileQuery,
  projectCalendar,
  projectError,
  projectMyRegistration,
  projectProfile,
  projectRegistration,
  readBounded,
  rewriteIcsUrl,
  rewriteSetCookie,
  singleOccurrenceId,
  webCalendarLink,
} from '../lib/proxy-core.ts';

const KEY = 'wbk_abcdef_' + 'x'.repeat(40);
const OCC = '0f8b2d4e-6c1a-4b7e-9f3d-2a5c8e1b7d90';
const TOKEN = 'A'.repeat(43);

test('config requires an https origin-only base and a key', () => {
  assert.equal(parseProxyConfig({}).ok, false);
  assert.equal(parseProxyConfig({ VISIONCAST_API_BASE: 'http://api.example.com', VISIONCAST_PROXY_KEY: KEY }).ok, false);
  assert.equal(parseProxyConfig({ VISIONCAST_API_BASE: 'https://api.example.com/v1', VISIONCAST_PROXY_KEY: KEY }).ok, false);
  assert.equal(parseProxyConfig({ VISIONCAST_API_BASE: 'https://u:p@api.example.com', VISIONCAST_PROXY_KEY: KEY }).ok, false);
  assert.equal(parseProxyConfig({ VISIONCAST_API_BASE: 'https://api.example.com', VISIONCAST_PROXY_KEY: 'short' }).ok, false);
  const ok = parseProxyConfig({ VISIONCAST_API_BASE: 'https://api.example.com', VISIONCAST_PROXY_KEY: KEY });
  assert.ok(ok.ok);
  if (ok.ok) {
    assert.equal(ok.config.apiBase, 'https://api.example.com');
    assert.deepEqual([...ok.config.allowedOrigins], ['https://visioncast-ten.vercel.app']);
  }
});

test('config errors never include the key value', () => {
  const res = parseProxyConfig({ VISIONCAST_API_BASE: 'nope', VISIONCAST_PROXY_KEY: 'secret value with spaces' });
  assert.equal(res.ok, false);
  if (!res.ok) assert.ok(res.errors.every((e) => !e.includes('secret value')));
});

test('insecure backend only outside production and only when opted in', () => {
  const env = { VISIONCAST_API_BASE: 'http://127.0.0.1:8000', VISIONCAST_PROXY_KEY: KEY, VISIONCAST_ALLOW_INSECURE_BACKEND: '1' };
  assert.equal(parseProxyConfig({ ...env, NODE_ENV: 'development' }).ok, true);
  assert.equal(parseProxyConfig({ ...env, NODE_ENV: 'production' }).ok, false);
});

test('allowed origins must be exact', () => {
  const base = { VISIONCAST_API_BASE: 'https://api.example.com', VISIONCAST_PROXY_KEY: KEY };
  assert.equal(parseProxyConfig({ ...base, VISIONCAST_ALLOWED_ORIGINS: 'https://visioncast-ten.vercel.app/' }).ok, false);
  assert.equal(parseProxyConfig({ ...base, VISIONCAST_ALLOWED_ORIGINS: 'http://evil.example' }).ok, false);
  assert.equal(parseProxyConfig({ ...base, VISIONCAST_ALLOWED_ORIGINS: 'https://visioncast-ten.vercel.app,http://localhost:3000' }).ok, true);
});

test('origin and fetch-site checks', () => {
  const allowed = ['https://visioncast-ten.vercel.app'];
  assert.equal(checkOrigin('https://visioncast-ten.vercel.app', 'same-origin', 'POST', allowed), true);
  assert.equal(checkOrigin(null, null, 'POST', allowed), false);
  assert.equal(checkOrigin('https://evil.example', null, 'POST', allowed), false);
  assert.equal(checkOrigin('https://visioncast-ten.vercel.app', 'cross-site', 'POST', allowed), false);
  assert.equal(checkOrigin(null, 'same-origin', 'GET', allowed), true);
  assert.equal(checkOrigin(null, 'none', 'GET', allowed), true);
  assert.equal(checkOrigin(null, 'none', 'POST', allowed), false);
  assert.equal(checkOrigin(null, 'cross-site', 'GET', allowed), false);
});

test('double-submit CSRF', () => {
  assert.equal(checkCsrf(TOKEN, TOKEN), true);
  assert.equal(checkCsrf(TOKEN, 'B'.repeat(43)), false);
  assert.equal(checkCsrf(null, TOKEN), false);
  assert.equal(checkCsrf('short', 'short'), false);
});

test('cookie parsing keeps the first value', () => {
  const c = parseCookies('a=1; webinar_session=abc; webinar_session=evil');
  assert.equal(c.get('webinar_session'), 'abc');
});

test('Set-Cookie rewrite pins attributes and drops other cookies', () => {
  const out = rewriteSetCookie('webinar_session=tok123; Domain=api.example.com; Path=/; Max-Age=100; SameSite=None', 'webinar_session');
  assert.equal(out, 'webinar_session=tok123; Path=/webinar-api; Max-Age=100; HttpOnly; Secure; SameSite=Lax');
  assert.equal(rewriteSetCookie('sessionid=x; Path=/', 'webinar_session'), null);
  assert.equal(rewriteSetCookie('csrftoken=x', 'webinar_session'), null);
  const cleared = rewriteSetCookie('webinar_session=""; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/', 'webinar_session');
  assert.equal(cleared, 'webinar_session=; Path=/webinar-api; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; HttpOnly; Secure; SameSite=Lax');
});

test('query validation', () => {
  assert.equal(singleOccurrenceId(new URLSearchParams({ occurrenceId: OCC.toUpperCase() })), OCC);
  assert.equal(singleOccurrenceId(new URLSearchParams(`occurrenceId=${OCC}&x=1`)), null);
  assert.equal(singleOccurrenceId(new URLSearchParams(`occurrenceId=${OCC}&occurrenceId=${OCC}`)), null);
  assert.deepEqual(profileQuery(new URLSearchParams({ occurrenceId: OCC, formVersion: '2025-default' })), {
    occurrenceId: OCC,
    formVersion: '2025-default',
  });
  assert.equal(profileQuery(new URLSearchParams({ occurrenceId: OCC, formVersion: '../x' })), null);
  assert.equal(profileQuery(new URLSearchParams({ occurrenceId: 'nope', formVersion: 'v1' })), null);
});

test('json content type', () => {
  assert.equal(isJsonContentType('application/json'), true);
  assert.equal(isJsonContentType('application/json; charset=utf-8'), true);
  assert.equal(isJsonContentType('text/plain'), false);
  assert.equal(isJsonContentType('application/json; charset=latin1'), false);
});

test('client ip', () => {
  assert.equal(clientIp('203.0.113.9, 10.0.0.1'), '203.0.113.9');
  assert.equal(clientIp('<script>'), null);
});

test('errors keep only codes and field keys', () => {
  const out = projectError({
    error: { code: 'validation_error', message: 'Enter a valid email for ada@example.com', fields: { email: 'bad' } },
    fieldErrors: { 'custom.role_focus': 'bad' },
  });
  assert.deepEqual(out, { error: { code: 'validation_error' }, fieldErrors: { 'custom.role_focus': 'invalid', email: 'invalid' } });
  assert.ok(!JSON.stringify(out).includes('ada@'));
});

test('profile projection drops unknown keys', () => {
  assert.deepEqual(projectProfile({ recognized: true, missingFields: ['jobTitle', 4], formVersion: 'v1', email: 'x@y.z' }), {
    recognized: true,
    missingFields: ['jobTitle'],
    formVersion: 'v1',
  });
});

test('ICS urls are rewritten to this origin only', () => {
  assert.equal(rewriteIcsUrl(`/api/v1/occurrences/${OCC}/calendar.ics`), `/webinar-api/calendar.ics?occurrenceId=${OCC}`);
  assert.equal(rewriteIcsUrl(`https://api.example.com/api/v1/occurrences/${OCC}/calendar.ics`), `/webinar-api/calendar.ics?occurrenceId=${OCC}`);
  assert.equal(rewriteIcsUrl(`/webinar-api/calendar.ics?occurrenceId=${OCC}`), `/webinar-api/calendar.ics?occurrenceId=${OCC}`);
  assert.equal(rewriteIcsUrl('https://evil.example/x.ics'), undefined);
});

test('web calendar links must match the vendor path exactly', () => {
  assert.ok(webCalendarLink('https://calendar.google.com/calendar/render?action=TEMPLATE', 'https://calendar.google.com/calendar/render'));
  assert.equal(webCalendarLink('https://calendar.google.com.evil.example/calendar/render', 'https://calendar.google.com/calendar/render'), null);
  assert.equal(webCalendarLink('http://calendar.google.com/calendar/render', 'https://calendar.google.com/calendar/render'), null);
  const cal = projectCalendar({ icsUrl: `/api/v1/occurrences/${OCC}/calendar.ics`, googleUrl: 'javascript:alert(1)', joinUrl: 'https://zoom.example/j/1' });
  assert.equal(cal.googleUrl, null);
  assert.ok(!('joinUrl' in cal));
});

test('registration projection never passes through extra keys', () => {
  const out = projectRegistration({
    status: 'registered',
    occurrenceId: OCC,
    thankYou: { mode: 'redirect', redirectUrl: 'javascript:alert(1)', message: 'Thanks' },
    calendar: { title: 'T', icsUrl: `/api/v1/occurrences/${OCC}/calendar.ics` },
    personId: 'p-1',
    joinUrl: 'https://provider.example/join',
  });
  assert.equal(out.thankYou.redirectUrl, undefined);
  assert.equal(out.calendar?.icsUrl, `/webinar-api/calendar.ics?occurrenceId=${OCC}`);
  assert.ok(!JSON.stringify(out).includes('provider.example'));
  assert.ok(!JSON.stringify(out).includes('p-1'));
});

test('my-registration projection omits blocks unless registered', () => {
  const out = projectMyRegistration({ occurrenceId: OCC, recognized: true, registered: false, status: null, thankYou: { mode: 'message' } });
  assert.ok(!('thankYou' in out));
  assert.equal(out.registered, false);
});

test('readBounded enforces the limit', async () => {
  const stream = (n: number) =>
    new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new Uint8Array(n));
        c.close();
      },
    });
  assert.equal((await readBounded(stream(10), 10))?.byteLength, 10);
  assert.equal(await readBounded(stream(11), 10), null);
  assert.equal((await readBounded(null, 10))?.byteLength, 0);
});
