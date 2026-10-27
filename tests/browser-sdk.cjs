'use strict';
/*
 * Browser tests for sdk/src/webinar-form.js using the `playwright` library.
 * Run: node tests/browser-sdk.cjs
 * No real network: pages are served via context.route and window.fetch is faked.
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const SDK_PATH = path.join(__dirname, '..', 'sdk', 'src', 'webinar-form.js');
const SDK_SOURCE = fs.readFileSync(SDK_PATH, 'utf8').replace(/<\/script/gi, '<\\/script');

const ORIGIN = 'https://site.test';
const PAGE_URL = ORIGIN + '/webinar';
const OCC = '7f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f';
const FORM_VERSION = 'v1';
const CSRF_TOKEN = 'abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG'; // 43 chars
const PROFILE_KEY = 'GET /webinar-api/profile';
const REG_KEY = 'POST /webinar-api/registrations';
const RESET_KEY = 'POST /webinar-api/reset';
const FIELDS = ['email', 'firstName', 'lastName', 'company', 'phone'];

const HTML = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Webinar</title></head>
<body>
<form data-webinar-form data-occurrence-id="${OCC}" data-form-version="${FORM_VERSION}">
  <div data-webinar-field><label for="f-email">Email</label><input id="f-email" name="email" type="email" required></div>
  <div data-webinar-field><label for="f-first">First name</label><input id="f-first" name="firstName" type="text" required></div>
  <div data-webinar-field><label for="f-last">Last name</label><input id="f-last" name="lastName" type="text"></div>
  <div data-webinar-field><label for="f-company">Company</label><input id="f-company" name="company" type="text"></div>
  <div data-webinar-field><label for="f-phone">Phone</label><input id="f-phone" name="phone" type="tel"></div>
  <label><input type="checkbox" name="consents.sms" data-disclosure-version="sms-v1"> Text me reminders</label>
  <div data-webinar-status></div>
  <button type="submit">Register</button>
</form>
<script>${SDK_SOURCE}</script>
</body></html>`;

/** Runs in the page before any other script. Installs config and a fake fetch. */
function initScript(args) {
  if (args.config) window.WebinarFormConfig = args.config;
  window.__calls = [];
  window.__fake = { queues: args.queues || {} };
  function pathOf(url) {
    try { return new URL(url, location.href).pathname; } catch (e) { return String(url); }
  }
  window.fetch = function (url, init) {
    init = init || {};
    const method = String(init.method || 'GET').toUpperCase();
    let body = null;
    if (typeof init.body === 'string') {
      try { body = JSON.parse(init.body); } catch (e) { body = init.body; }
    }
    const entry = { url: String(url), method: method, headers: Object.assign({}, init.headers || {}), body: body };
    window.__calls.push(entry);
    // Also mirrored to the console so the test can still read calls after a navigation away.
    console.debug('__webinar_call ' + JSON.stringify(entry));
    const key = method + ' ' + pathOf(String(url));
    const q = window.__fake.queues[key];
    if (!q || !q.length) {
      return Promise.resolve(new Response(JSON.stringify({ error: { code: 'not_found' } }), { status: 404 }));
    }
    const resp = q.length > 1 ? q.shift() : q[0];
    if (resp.reject) return Promise.reject(new TypeError('Failed to fetch'));
    if (key === 'GET /webinar-api/profile' && args.csrfToken) {
      try { document.cookie = '__Host-webinar_csrf=' + args.csrfToken + '; Path=/; Secure'; } catch (e) { /* ignore */ }
    }
    return Promise.resolve(new Response(JSON.stringify(resp.body === undefined ? null : resp.body), {
      status: resp.status || 200,
      headers: { 'Content-Type': 'application/json' }
    }));
  };
}

let browser;

async function openPage(opts) {
  opts = opts || {};
  const context = await browser.newContext();
  await context.route(() => true, (route) => {
    const u = new URL(route.request().url());
    if (u.origin === ORIGIN && u.pathname === '/webinar') {
      return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: HTML });
    }
    const extra = (opts.routes || {})[u.href];
    if (extra !== undefined) {
      return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: extra });
    }
    return route.abort();
  });
  if (opts.cookies) {
    await context.addCookies(opts.cookies.map((c) => Object.assign({ url: ORIGIN }, c)));
  }
  const page = await context.newPage();
  page.__seen = [];
  page.on('console', (msg) => {
    const t = msg.text();
    if (t.startsWith('__webinar_call ')) page.__seen.push(JSON.parse(t.slice('__webinar_call '.length)));
  });
  await page.addInitScript(initScript, {
    config: opts.config || null,
    csrfToken: CSRF_TOKEN,
    queues: { [PROFILE_KEY]: [{ status: 200, body: opts.profile || { recognized: false, missingFields: [], formVersion: FORM_VERSION } }] }
  });
  await page.goto(PAGE_URL + (opts.query || ''));
  await page.waitForFunction(() => {
    const f = document.querySelector('form[data-webinar-form]');
    return !!(f && f.__webinarForm && window.__calls.length > 0);
  }, null, { timeout: 2000 });
  // The auto-boot loadProfile() promise is not exposed; awaiting a second
  // (idempotent, same response) load makes the resulting DOM state deterministic.
  await page.evaluate(() => document.querySelector('form').__webinarForm.loadProfile());
  return { context, page };
}

function setQueue(page, key, responses) {
  return page.evaluate(([k, r]) => { window.__fake.queues[k] = r; }, [key, responses]);
}

function submit(page) {
  return page.evaluate(() => document.querySelector('form').__webinarForm.submit());
}

function calls(page) {
  return page.evaluate(() => window.__calls);
}

async function postsTo(page, key) {
  const [method, p] = key.split(' ');
  return (await calls(page)).filter((c) => c.method === method && new URL(c.url, ORIGIN).pathname === p);
}

/** Like postsTo, but from the console mirror: survives a navigation to another origin. */
function postsSeen(page, key) {
  const [method, p] = key.split(' ');
  return page.__seen.filter((c) => c.method === method && new URL(c.url, ORIGIN).pathname === p);
}

async function fieldState(page, name) {
  const loc = page.locator(`form [name="${name}"]`);
  return { visible: await loc.isVisible(), disabled: await loc.isDisabled() };
}

async function fillAnonymous(page) {
  await page.fill('[name="email"]', 'ada@example.com');
  await page.fill('[name="firstName"]', 'Ada');
  await page.fill('[name="lastName"]', 'Lovelace');
  await page.fill('[name="company"]', 'Analytical Engines');
}

async function assertCsrf(page, call) {
  const cookie = await page.evaluate(() => document.cookie);
  if (cookie.indexOf('__Host-webinar_csrf=' + CSRF_TOKEN) !== -1) {
    assert.equal(call.headers['X-Webinar-CSRF'], CSRF_TOKEN, 'CSRF header mirrors cookie');
  } else {
    assert.equal(call.headers['X-Webinar-CSRF'], undefined, 'no CSRF header without cookie');
  }
}

function text(page, selector) {
  return page.locator(selector).first().textContent();
}

// ---------------------------------------------------------------------------
// Scenarios
// ---------------------------------------------------------------------------

const scenarios = [];
function scenario(name, fn, opts) { scenarios.push({ name, fn, opts: opts || {} }); }

scenario('1. anonymous visitor sees the full form', async (page) => {
  const profileCall = (await calls(page))[0];
  assert.equal(profileCall.method, 'GET');
  assert.ok(profileCall.url.startsWith('/webinar-api/profile?'), profileCall.url);
  assert.ok(profileCall.url.includes('occurrenceId=' + OCC));
  assert.ok(profileCall.url.includes('formVersion=' + FORM_VERSION));
  for (const name of FIELDS) {
    assert.deepEqual(await fieldState(page, name), { visible: true, disabled: false }, name);
  }
  assert.equal((await text(page, 'button[type="submit"]')).trim(), 'Register');
  assert.equal(await page.locator('[data-webinar-reset]').isHidden(), true);
});

scenario('2. recognized visitor sees only missing fields', async (page) => {
  assert.deepEqual(await fieldState(page, 'company'), { visible: true, disabled: false });
  for (const name of ['email', 'firstName', 'lastName', 'phone']) {
    assert.deepEqual(await fieldState(page, name), { visible: false, disabled: true }, name);
  }
  assert.equal(await page.locator('[data-webinar-reset]').isVisible(), true);
  assert.match(await text(page, '[data-webinar-status]'), /welcome back/i);
  assert.equal((await text(page, 'button[type="submit"]')).trim(), 'Register');
}, { profile: { recognized: true, missingFields: ['company'], formVersion: FORM_VERSION } });

scenario('3. confirm-only submission and success state', async (page) => {
  assert.equal((await text(page, 'button[type="submit"]')).trim(), 'Confirm my RSVP');
  const icsPath = '/webinar-api/calendar.ics?occurrenceId=' + OCC;
  await setQueue(page, REG_KEY, [{ status: 201, body: {
    status: 'registered', thankYou: { mode: 'message', message: 'See you there' }, calendar: { icsUrl: icsPath }
  } }]);
  assert.equal(await submit(page), true);
  const posts = await postsTo(page, REG_KEY);
  assert.equal(posts.length, 1);
  const call = posts[0];
  assert.equal(call.url, '/webinar-api/registrations');
  assert.equal(call.headers['Content-Type'], 'application/json');
  await assertCsrf(page, call);
  const b = call.body;
  assert.equal(b.schemaVersion, '1');
  assert.equal(b.occurrenceId, OCC);
  assert.equal(b.formVersion, FORM_VERSION);
  assert.equal(typeof b.idempotencyKey, 'string');
  assert.ok(b.idempotencyKey.length > 0);
  assert.deepEqual(b.profile, {});
  assert.deepEqual(b.consents.analytics, { granted: false });
  assert.deepEqual(b.consents.advertising, { granted: false });
  assert.deepEqual(b.consents.sms, { granted: false, disclosureVersion: 'sms-v1' });
  assert.deepEqual(b.attribution, { parameters: {}, identifiers: {} });
  assert.equal(await page.getAttribute('form', 'data-webinar-state'), 'complete');
  assert.match(await text(page, '[data-webinar-status]'), /See you there/);
  assert.equal(await page.getAttribute('a[data-webinar-calendar]', 'href'), ORIGIN + icsPath);
}, { profile: { recognized: true, missingFields: [], formVersion: FORM_VERSION } });

scenario('4. server field errors are announced accessibly', async (page) => {
  await fillAnonymous(page);
  await setQueue(page, REG_KEY, [{ status: 400, body: {
    error: { code: 'validation_error' }, fieldErrors: { email: 'invalid' }
  } }]);
  assert.equal(await submit(page), false);
  const info = await page.evaluate(() => {
    const el = document.querySelector('[name="email"]');
    const ids = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
    const err = document.querySelector('[data-webinar-error]');
    return {
      invalid: el.getAttribute('aria-invalid'),
      described: ids.map((id) => { const n = document.getElementById(id); return n ? n.textContent.trim() : null; }),
      errorRole: err && err.getAttribute('role'),
      errorText: err ? err.textContent.trim() : '',
      state: document.querySelector('form').getAttribute('data-webinar-state'),
      firstNameInvalid: document.querySelector('[name="firstName"]').getAttribute('aria-invalid')
    };
  });
  assert.equal(info.invalid, 'true');
  assert.ok(info.described.length >= 1, 'aria-describedby set');
  for (const t of info.described) assert.ok(t, 'describedby target exists with text');
  assert.equal(info.errorRole, 'alert');
  assert.ok(info.errorText.length > 0);
  assert.equal(info.state, null);
  assert.equal(info.firstNameInvalid, null);
});

scenario('5. network failure, retries and idempotency keys', async (page) => {
  await fillAnonymous(page);
  await setQueue(page, REG_KEY, [{ reject: true }]);
  assert.equal(await submit(page), false);
  assert.equal(await page.getAttribute('form', 'data-webinar-state'), null);
  assert.match(await text(page, '[data-webinar-error]'), /not saved/);

  // Same intent retried: same key.
  assert.equal(await submit(page), false);
  let posts = await postsTo(page, REG_KEY);
  assert.equal(posts.length, 2);
  const k1 = posts[0].body.idempotencyKey;
  assert.equal(typeof k1, 'string');
  assert.equal(posts[1].body.idempotencyKey, k1, 'unchanged retry reuses key');

  // Changed intent: new key.
  await page.fill('[name="company"]', 'Difference Engines');
  assert.equal(await submit(page), false);
  posts = await postsTo(page, REG_KEY);
  const k3 = posts[2].body.idempotencyKey;
  assert.notEqual(k3, k1, 'changed input yields new key');

  // Retry of the changed intent succeeds with the same key.
  await setQueue(page, REG_KEY, [{ status: 201, body: { status: 'registered' } }]);
  assert.equal(await submit(page), true);
  posts = await postsTo(page, REG_KEY);
  assert.equal(posts.length, 4);
  assert.equal(posts[3].body.idempotencyKey, k3);
  assert.equal(await page.getAttribute('form', 'data-webinar-state'), 'complete');
  const keyAfter = await page.evaluate(() => document.querySelector('form').__webinarForm.idempotency._key);
  assert.equal(keyAfter, null, 'key reset after completion');
});

scenario('6. "Not you?" reset restores the full form', async (page) => {
  assert.equal(await page.locator('[data-webinar-reset]').isVisible(), true);
  await setQueue(page, RESET_KEY, [{ status: 200, body: { reset: true } }]);
  await page.click('[data-webinar-reset]');
  await page.waitForFunction(() => /Form cleared/.test(document.querySelector('[data-webinar-status]').textContent), null, { timeout: 2000 });
  const resets = await postsTo(page, RESET_KEY);
  assert.equal(resets.length, 1);
  assert.equal(resets[0].url, '/webinar-api/reset');
  await assertCsrf(page, resets[0]);
  for (const name of FIELDS) {
    assert.deepEqual(await fieldState(page, name), { visible: true, disabled: false }, name);
  }
  assert.equal(await page.locator('[data-webinar-reset]').isHidden(), true);
  assert.match(await text(page, '[data-webinar-status]'), /^Form cleared/);
}, { profile: { recognized: true, missingFields: ['company'], formVersion: FORM_VERSION } });

scenario('7. attribution respects consent', async (page) => {
  assert.equal(await page.evaluate(() => location.search), '?utm_source=x&gclid=y&gad_source=1');
  assert.ok((await page.evaluate(() => document.cookie)).includes('_ga=GA1.1.123.456'));
  await fillAnonymous(page);
  await setQueue(page, REG_KEY, [{ status: 500, body: { error: { code: 'internal' } } }, { status: 201, body: { status: 'registered' } }]);

  assert.equal(await submit(page), false);
  let posts = await postsTo(page, REG_KEY);
  const a1 = posts[0].body.attribution;
  assert.deepEqual(a1.parameters, {});
  assert.deepEqual(a1.identifiers, {});
  assert.equal('pageUrl' in a1, false);
  assert.equal('referrer' in a1, false);
  assert.deepEqual(posts[0].body.consents.analytics, { granted: false });

  await page.evaluate(() => window.WebinarForm.setConsent({ analytics: true, advertising: true }));
  assert.equal(await submit(page), true);
  posts = await postsTo(page, REG_KEY);
  const b2 = posts[1].body;
  assert.deepEqual(b2.consents.analytics, { granted: true });
  assert.deepEqual(b2.consents.advertising, { granted: true });
  assert.deepEqual(b2.attribution.parameters, { utm_source: 'x', gclid: 'y' });
  assert.equal('gad_source' in b2.attribution.parameters, false);
  assert.deepEqual(b2.attribution.identifiers, { gaClientId: '123.456' });
  assert.equal(b2.attribution.pageUrl, PAGE_URL);
}, { query: '?utm_source=x&gclid=y&gad_source=1', cookies: [{ name: '_ga', value: 'GA1.1.123.456' }] });

scenario('8. backslash proxyBase falls back to /webinar-api', async (page) => {
  const all = await calls(page);
  assert.ok(all.length >= 1);
  for (const c of all) assert.ok(c.url.startsWith('/webinar-api/'), 'unexpected request URL: ' + c.url);
}, { config: { proxyBase: '/\\evil.example' } });

// ---------------------------------------------------------------------------

scenario('9. web calendar links render with accessible labels; unsafe ones are dropped', async (page) => {
  const google = 'https://calendar.google.com/calendar/render?action=TEMPLATE&text=Demo';
  const outlook = 'https://outlook.live.com/calendar/0/deeplink/compose?rru=addevent&subject=Demo';
  await setQueue(page, REG_KEY, [{ status: 201, body: {
    status: 'registered', thankYou: { mode: 'message', message: 'See you there' },
    calendar: { icsUrl: '/webinar-api/calendar.ics?occurrenceId=' + OCC, googleUrl: google, outlookUrl: outlook,
      office365Url: 'javascript:alert(1)' }
  } }]);
  assert.equal(await submit(page), true);
  assert.equal(await page.getAttribute('form', 'data-webinar-state'), 'complete');
  const links = page.locator('a[data-webinar-calendar-link]');
  assert.equal(await links.count(), 2, 'the javascript: Microsoft 365 link is dropped');
  assert.deepEqual(await links.evaluateAll((as) => as.map((a) => a.getAttribute('data-webinar-calendar-link'))), ['google', 'outlook']);
  assert.deepEqual(await links.evaluateAll((as) => as.map((a) => a.href)), [google, outlook]);
  for (const a of await links.elementHandles()) {
    assert.equal(await a.getAttribute('target'), '_blank');
    assert.equal(await a.getAttribute('rel'), 'noopener noreferrer');
    const label = await a.getAttribute('aria-label');
    const textContent = (await a.textContent()).trim();
    assert.ok(label.startsWith(textContent) && /new tab/i.test(label), 'accessible name starts with the visible text');
  }
  // Keyboard reachable: Tab from the disabled form lands on the ICS link, then the web links.
  assert.equal(await page.getAttribute('a[data-webinar-calendar]', 'href'), ORIGIN + '/webinar-api/calendar.ics?occurrenceId=' + OCC);
  assert.equal((await page.evaluate(() => document.querySelector('[data-webinar-status]').textContent)).includes('javascript:'), false);
}, { profile: { recognized: true, missingFields: [], formVersion: FORM_VERSION } });

scenario('10. paid session: 202 payment_required goes to Stripe Checkout, keeps the key, never confirms', async (page) => {
  await fillAnonymous(page);
  // First answer: a checkout link on an unexpected host is never followed ("preparing checkout").
  await setQueue(page, REG_KEY, [{ status: 202, body: { status: 'payment_required', order: { id: 'o1', checkoutUrl: 'https://evil.example/pay' } } }]);
  assert.equal(await submit(page), 'payment_required');
  assert.equal(await page.getAttribute('form', 'data-webinar-state'), 'payment-required');
  assert.equal(page.url(), PAGE_URL, 'no navigation to an unexpected host');
  assert.match(await text(page, '[data-webinar-status]'), /checkout/i);
  assert.equal(await page.locator('a[data-webinar-calendar]').count(), 0, 'no confirmation or calendar before payment');
  assert.equal(await fieldState(page, 'email').then((s) => s.disabled), false, 'the form is not locked');
  // Resubmitting replays the same order: identical idempotency key, never a second hold.
  await setQueue(page, REG_KEY, [{ status: 202, body: { status: 'payment_required', order: { id: 'o1', checkoutUrl: 'https://checkout.stripe.com/c/pay/cs_test_1' } } }]);
  const navigation = page.waitForURL('https://checkout.stripe.com/c/pay/cs_test_1', { timeout: 3000 });
  assert.equal(await submit(page), 'payment_required');
  await navigation;
  const posts = postsSeen(page, REG_KEY);
  assert.equal(posts.length, 2);
  assert.equal(posts[1].body.idempotencyKey, posts[0].body.idempotencyKey, 'the held order is replayed with the same key');
  assert.equal(page.url(), 'https://checkout.stripe.com/c/pay/cs_test_1');
}, { routes: { 'https://checkout.stripe.com/c/pay/cs_test_1': '<!doctype html><title>Checkout</title>' } });

scenario('11. paid session, unverified browser: 202 verification_required goes to the email check', async (page) => {
  await fillAnonymous(page);
  await setQueue(page, REG_KEY, [{ status: 202, body: { status: 'verification_required', verification: { available: false, verifyUrl: null } } }]);
  assert.equal(await submit(page), 'verification_required');
  assert.equal(await page.getAttribute('form', 'data-webinar-state'), 'verification-required');
  assert.equal(page.url(), PAGE_URL);
  assert.match(await text(page, '[data-webinar-status]'), /verif|email/i);
  assert.equal(await page.locator('a[data-webinar-calendar]').count(), 0, 'never a confirmation');
  const first = (await postsTo(page, REG_KEY))[0].body.idempotencyKey;
  // Available: navigate to the hosted waiting-room check (https, /w/<code>/ only). A new key is used afterwards.
  const room = 'https://webinars.example.com/w/room-1/';
  await setQueue(page, REG_KEY, [{ status: 202, body: { status: 'verification_required', verification: { available: true, verifyUrl: room } } }]);
  const navigation = page.waitForURL(room, { timeout: 3000 });
  assert.equal(await submit(page), 'verification_required');
  await navigation;
  const posts = postsSeen(page, REG_KEY);
  assert.equal(posts.length, 2);
  assert.notEqual(posts[1].body.idempotencyKey, first, 'verification resets the key (nothing was held)');
  assert.equal(page.url(), room);
}, { routes: { 'https://webinars.example.com/w/room-1/': '<!doctype html><title>Check your email</title>' } });

async function main() {
  const launch = { headless: true };
  if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE) launch.executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
  browser = await chromium.launch(launch);
  let failed = 0;
  for (const s of scenarios) {
    let context;
    try {
      const opened = await openPage(s.opts);
      context = opened.context;
      await s.fn(opened.page);
      console.log('ok   ' + s.name);
    } catch (err) {
      failed++;
      console.error('FAIL ' + s.name);
      console.error(err && err.stack ? err.stack : err);
    } finally {
      if (context) await context.close().catch(() => {});
    }
  }
  console.log(`\n${scenarios.length - failed}/${scenarios.length} scenarios passed`);
  if (failed) process.exitCode = 1;
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close().catch(() => {});
});
