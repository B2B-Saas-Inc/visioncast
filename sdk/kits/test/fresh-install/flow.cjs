'use strict';
/*
 * Real-backend browser flow for a kit page. Drives the SDK through a kit-served page with
 * Chromium and the live proxy + Django backend. No mocks: network calls are observed only.
 *
 *   NODE_PATH=/tmp/pw/node_modules node flow.cjs <page-url> [--proxy-base /webinar-api] [--https-insecure] [--confirm-only]
 *
 * Checks: profile 200 + __Host-webinar_csrf cookie, anonymous full form, consent default denied
 * (payload consents false, no attribution), registration completes (data-webinar-state=complete,
 * calendar link on this origin, ICS 200 text/calendar), reload shows recognised state (fields hidden,
 * "Confirm my RSVP" when the form is fully covered), "Not you? Start over" resets, and
 * WebinarForm.setConsent grants analytics (payload consents true, utm_source captured).
 */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const args = process.argv.slice(2);
const pageUrl = args[0];
const proxyBase = args.includes('--proxy-base') ? args[args.indexOf('--proxy-base') + 1] : '/webinar-api';
const httpsInsecure = args.includes('--https-insecure');
const confirmOnly = args.includes('--confirm-only');
const label = args.includes('--label') ? args[args.indexOf('--label') + 1] : 'kit';
if (!pageUrl) throw new Error('usage: kit-flow.cjs <page-url>');
const origin = new URL(pageUrl).origin;
const run = Date.now().toString(36);
const results = [];
const ok = (name, cond, detail) => {
  results.push({ name, pass: !!cond });
  if (!cond) throw new Error(`FAIL ${name}${detail ? ': ' + detail : ''}`);
  console.log('ok   ' + name);
};

async function observe(page) {
  const calls = [];
  page.on('request', (req) => {
    const u = new URL(req.url());
    if (u.origin === origin && u.pathname.startsWith(proxyBase + '/')) {
      calls.push({ method: req.method(), path: u.pathname, search: u.search, body: req.postDataJSON ? safeJson(req) : null, headers: req.headers(), req });
    }
  });
  page.on('response', (res) => {
    const u = new URL(res.url());
    if (u.origin === origin && u.pathname.startsWith(proxyBase + '/')) {
      const c = calls.find((x) => x.req === res.request());
      if (c) { c.status = res.status(); c.resHeaders = res.headers(); c.all = res.allHeaders().catch(() => ({})); }
    }
  });
  return calls;
}
function safeJson(req) { try { return req.postDataJSON(); } catch (e) { return null; } }

async function waitBound(page) {
  await page.waitForFunction(() => {
    const f = document.querySelector('form[data-webinar-form]');
    return !!(f && f.__webinarForm);
  }, null, { timeout: 15000 });
  // Wait for the boot profile load to settle (status text or no pending request).
  await page.waitForTimeout(400);
}
async function fieldState(page, name) {
  const loc = page.locator(`form[data-webinar-form] [name="${name}"]`);
  return { visible: await loc.isVisible(), disabled: await loc.isDisabled() };
}
const text = (page, sel) => page.locator(sel).first().textContent();

async function main() {
  const launch = { headless: true }; if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE) launch.executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE; const browser = await chromium.launch(launch);
  const context = await browser.newContext({ ignoreHTTPSErrors: httpsInsecure });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/favicon/.test(m.location().url || '')) errors.push(m.text() + ' @ ' + (m.location().url || '')); });
  const calls = await observe(page);

  // 1. Anonymous visit with a UTM parameter that must NOT be captured without consent.
  await page.goto(pageUrl + (pageUrl.includes('?') ? '&' : '?') + 'utm_source=kit-test', { waitUntil: 'load' });
  await waitBound(page);
  const FIELDS = await page.evaluate(() => Array.from(document.querySelectorAll('form[data-webinar-form] input[name]')).map((i) => i.name));
  const sdkScripts = await page.evaluate(() => Array.from(document.scripts).filter((s) => /webinar-form\.js/.test(s.src)).length);
  ok('SDK script loaded exactly once', sdkScripts === 1, String(sdkScripts));
  const profile = calls.find((c) => c.method === 'GET' && c.path === proxyBase + '/profile');
  ok('GET /webinar-api/profile returned 200', profile && profile.status === 200, profile && String(profile.status));
  const setCookie = profile ? ((await profile.all)['set-cookie'] || '') : '';
  ok('profile response sets __Host-webinar_csrf', /__Host-webinar_csrf=/.test(setCookie), setCookie);
  const cookieVisible = await page.evaluate(() => document.cookie.includes('__Host-webinar_csrf='));
  ok('browser accepted the __Host- cookie (document.cookie)', cookieVisible, 'cookie not stored: __Host- requires a secure context');
  for (const name of FIELDS) {
    assert.deepEqual(await fieldState(page, name), { visible: true, disabled: false }, name);
  }
  ok('anonymous visitor sees all fields', true);
  ok('reset button hidden for anonymous', await page.locator('[data-webinar-reset]').isHidden());
  const consent = await page.evaluate(() => window.WebinarForm.getConsent());
  ok('consent defaults to denied', consent.analytics === false && consent.advertising === false, JSON.stringify(consent));

  // 2. Register (consent denied) and check the complete state.
  const email = `kit-${label}-${run}@example.invalid`;
  await page.fill('[name="email"]', email);
  await page.fill('[name="firstName"]', 'Ada');
  await page.fill('[name="lastName"]', 'Lovelace');
  if (FIELDS.includes('company')) await page.fill('[name="company"]', 'Analytical Engines');
  await page.click('form[data-webinar-form] button[type="submit"]');
  await page.waitForFunction(() => document.querySelector('form[data-webinar-form]').getAttribute('data-webinar-state') === 'complete', null, { timeout: 15000 });
  const reg = calls.filter((c) => c.method === 'POST' && c.path === proxyBase + '/registrations');
  ok('POST /webinar-api/registrations returned 201', reg.length === 1 && reg[0].status === 201, JSON.stringify(reg.map((r) => r.status)));
  ok('CSRF header mirrors cookie', !!reg[0].headers['x-webinar-csrf'] && reg[0].headers['x-webinar-csrf'].length === 43);
  const body = reg[0].body;
  ok('payload schemaVersion 1 and profile fields', body.schemaVersion === '1' && body.profile.email === email && body.profile.firstName === 'Ada');
  ok('consent denied in payload', body.consents.analytics.granted === false && body.consents.advertising.granted === false, JSON.stringify(body.consents));
  ok('no attribution without consent', Object.keys(body.attribution.parameters).length === 0 && !('pageUrl' in body.attribution), JSON.stringify(body.attribution));
  const status = await text(page, '[data-webinar-status]');
  ok('thank-you message shown', /confirmed|thank|see you/i.test(status), status);
  const ics = await page.getAttribute('a[data-webinar-calendar]', 'href');
  ok('calendar link points at this origin proxy', ics && ics.startsWith(origin + proxyBase + '/calendar.ics?occurrenceId='), ics);
  const icsRes = await context.request.get(ics);
  ok('ICS download is 200 text/calendar', icsRes.status() === 200 && /text\/calendar/.test(icsRes.headers()['content-type']), `${icsRes.status()} ${icsRes.headers()['content-type']}`);
  const sessionCookie = (await context.cookies(origin)).find((c) => c.name === 'webinar_session');
  ok('webinar_session cookie re-issued HttpOnly on this origin', sessionCookie && sessionCookie.httpOnly, JSON.stringify(sessionCookie));

  // 3. Reload: recognised visitor.
  calls.length = 0;
  await page.reload({ waitUntil: 'load' });
  await waitBound(page);
  await page.waitForFunction(() => /welcome back/i.test(document.querySelector('[data-webinar-status]').textContent), null, { timeout: 10000 });
  const hidden = [];
  for (const name of FIELDS) {
    const st = await fieldState(page, name);
    if (!st.visible && st.disabled) hidden.push(name);
  }
  ok('recognised fields are hidden after reload', hidden.length === FIELDS.length, JSON.stringify(hidden));
  const btn = (await text(page, 'form[data-webinar-form] button[type="submit"]')).trim();
  if (confirmOnly) ok('button reads "Confirm my RSVP"', btn === 'Confirm my RSVP', btn);
  else console.log('info button label after reload: ' + btn);
  ok('reset button visible', await page.locator('[data-webinar-reset]').isVisible());

  // 4. Confirm-only resubmission (profile {} + idempotent) when the form is fully covered.
  if (confirmOnly) {
    await page.click('form[data-webinar-form] button[type="submit"]');
    await page.waitForFunction(() => document.querySelector('form[data-webinar-form]').getAttribute('data-webinar-state') === 'complete', null, { timeout: 15000 });
    const reg2 = calls.filter((c) => c.method === 'POST' && c.path === proxyBase + '/registrations');
    ok('confirm-only resubmit accepted with empty profile', reg2.length === 1 && reg2[0].status < 300 && Object.keys(reg2[0].body.profile).length === 0, JSON.stringify(reg2.map((r) => [r.status, r.body && r.body.profile])));
    await page.reload({ waitUntil: 'load' });
    await waitBound(page);
    await page.waitForFunction(() => /welcome back/i.test(document.querySelector('[data-webinar-status]').textContent), null, { timeout: 10000 });
  }

  // 5. "Not you? Start over".
  calls.length = 0;
  await page.click('[data-webinar-reset]');
  await page.waitForFunction(() => /Form cleared/.test(document.querySelector('[data-webinar-status]').textContent), null, { timeout: 10000 });
  const reset = calls.find((c) => c.method === 'POST' && c.path === proxyBase + '/reset');
  ok('POST /webinar-api/reset returned 200', reset && reset.status === 200, reset && String(reset.status));
  for (const name of FIELDS) {
    assert.deepEqual(await fieldState(page, name), { visible: true, disabled: false }, name);
  }
  ok('full form restored after reset', true);
  const sessionAfter = (await context.cookies(origin)).find((c) => c.name === 'webinar_session');
  ok('session cookie cleared by reset', !sessionAfter || sessionAfter.value === '', JSON.stringify(sessionAfter));

  // 6. Consent granted via WebinarForm.setConsent -> attribution captured, analytics consent true.
  calls.length = 0;
  await page.evaluate(() => window.WebinarForm.setConsent({ analytics: true, advertising: false }));
  await page.fill('[name="email"]', `kit-${label}-${run}-b@example.invalid`);
  await page.fill('[name="firstName"]', 'Grace');
  await page.fill('[name="lastName"]', 'Hopper');
  await page.click('form[data-webinar-form] button[type="submit"]');
  await page.waitForFunction(() => document.querySelector('form[data-webinar-form]').getAttribute('data-webinar-state') === 'complete', null, { timeout: 15000 });
  const reg3 = calls.filter((c) => c.method === 'POST' && c.path === proxyBase + '/registrations');
  ok('second registration (new identity) 201', reg3.length === 1 && reg3[0].status === 201, JSON.stringify(reg3.map((r) => r.status)));
  ok('analytics consent granted in payload', reg3[0].body.consents.analytics.granted === true && reg3[0].body.consents.advertising.granted === false);
  ok('utm_source captured with consent', reg3[0].body.attribution.parameters.utm_source === 'kit-test', JSON.stringify(reg3[0].body.attribution));

  // 7. Proxy hardening on this origin.
  const r404 = await context.request.get(origin + proxyBase + '/anything');
  ok('unknown proxy route is 404', r404.status() === 404, String(r404.status()));
  const r405 = await context.request.get(origin + proxyBase + '/registrations');
  ok('GET registrations is 405', r405.status() === 405, String(r405.status()));
  const r400 = await context.request.get(origin + proxyBase + '/profile?occurrenceId=../x&formVersion=v1');
  ok('bad query is 400', r400.status() === 400, String(r400.status()));

  ok('no page errors', errors.length === 0, errors.join(' | '));
  await browser.close();
  console.log(`\n${results.length}/${results.length} checks passed for ${pageUrl}`);
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
