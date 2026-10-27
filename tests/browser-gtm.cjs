'use strict';
/*
 * Real Chromium check of the site-wide GTM install tag: the exact generated fixture
 * (sdk/test/fixtures/gtm-tag.html, byte-identical to Studio's output per
 * tests/test_gtm_tracking.py) plus the real SDK, fired the way GTM fires a Custom HTML
 * tag (a script element appended to the page, once per trigger).
 *
 * Run: node tests/browser-gtm.cjs        (needs the `playwright` package and Chromium)
 *
 * No network: the page, the SDK file at the fixture's exact URL and the first-party
 * proxy routes are all served from context.route; every other request is aborted and
 * counted. No backend, no GTM container and no consent platform is involved: consent
 * arrives only through window.dataLayer pushes, as the CMP bridge example would do.
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const ROOT = path.join(__dirname, '..');
const GOLDEN = fs.readFileSync(path.join(ROOT, 'sdk', 'test', 'fixtures', 'gtm-tag.html'), 'utf8');
const SDK_SOURCE = fs.readFileSync(path.join(ROOT, 'webinars', 'static', 'webinar-sdk', 'webinar-form.js'), 'utf8');

const TAG = GOLDEN.slice(GOLDEN.indexOf('<script>') + '<script>'.length, GOLDEN.indexOf('</script>'));
const TAG_DENIED = TAG.replace('"consentKey": "webinarConsent"', '"consentKey": null');
assert.notEqual(TAG_DENIED, TAG, 'fixture carries the consent key');
assert.doesNotMatch(TAG, /\{\{/, 'fixture has no GTM variable references');

const SDK_URL = 'https://www.example.com/assets/webinar-form.js'; // exactly what the fixture loads
assert.ok(TAG.includes('"src": "' + SDK_URL + '"'));
const ORIGIN = 'https://site.test';
const PAGE_PATH = '/webinar';
const QUERY = '?utm_source=news&utm_medium=email&gclid=abc123&gad_source=1';
const OCC = '7f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f';
const FORM_VERSION = 'v1';
const STORAGE_KEY = 'webinarForm.attribution.v1';
const DENIED = { analytics: false, advertising: false };

function pageHtml(withForm) {
  const form = withForm ? `
<form data-webinar-form data-occurrence-id="${OCC}" data-form-version="${FORM_VERSION}">
  <div data-webinar-field><label for="f-email">Email</label><input id="f-email" name="email" type="email" required></div>
  <div data-webinar-field><label for="f-first">First name</label><input id="f-first" name="firstName" type="text" required></div>
  <div data-webinar-status></div>
  <button type="submit">Register</button>
</form>
<form id="plain"><input name="email"><button type="submit">Newsletter</button></form>` : '<p>No form here.</p>';
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Webinar</title></head>
<body>
<script>window.dataLayer = window.dataLayer || [];</script>
${form}
</body></html>`;
}

let browser;

/**
 * Opens a page where every request is intercepted. `opts.sdkStatuses` is a queue of HTTP
 * statuses for the SDK file (default: always 200) so a failed first load can be simulated.
 */
async function openPage(opts) {
  opts = opts || {};
  const context = await browser.newContext();
  const log = { sdk: [], profile: [], posts: [], foreign: [] };
  const sdkStatuses = (opts.sdkStatuses || []).slice();
  await context.route(() => true, async (route) => {
    const req = route.request();
    const u = new URL(req.url());
    if (req.url() === SDK_URL) {
      const status = sdkStatuses.length ? sdkStatuses.shift() : 200;
      log.sdk.push(status);
      if (status !== 200) return route.fulfill({ status, contentType: 'text/plain', body: 'not found' });
      return route.fulfill({ status: 200, contentType: 'application/javascript; charset=utf-8', body: SDK_SOURCE });
    }
    if (u.origin === ORIGIN && u.pathname === PAGE_PATH) {
      return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: pageHtml(opts.withForm !== false) });
    }
    if (u.origin === ORIGIN && u.pathname === '/webinar-api/profile' && req.method() === 'GET') {
      log.profile.push(u.search);
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ recognized: false, missingFields: [], formVersion: FORM_VERSION }) });
    }
    if (u.origin === ORIGIN && u.pathname === '/webinar-api/registrations' && req.method() === 'POST') {
      log.posts.push(JSON.parse(req.postData() || 'null'));
      return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ status: 'registered' }) });
    }
    if (u.origin === ORIGIN && u.pathname === '/favicon.ico') return route.abort();
    log.foreign.push(req.url());
    return route.abort();
  });
  await context.addCookies([{ url: ORIGIN, name: '_ga', value: 'GA1.1.123.456' }]);
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const warnings = [];
  page.on('console', (m) => { if (m.type() === 'warning') warnings.push(m.text()); });
  await page.goto(ORIGIN + PAGE_PATH + (opts.query === undefined ? QUERY : opts.query));
  return { context, page, log, errors, warnings };
}

/** Fires the tag the way GTM does: the Custom HTML is appended to the document as a script element. */
function fire(page, script) {
  return page.addScriptTag({ content: script || TAG });
}

/** What the CMP bridge pushes. */
function pushConsent(page, analytics, advertising) {
  return page.evaluate(([a, b]) => {
    window.dataLayer.push({ event: 'webinar_consent_update', webinarConsent: { analytics: a, advertising: b } });
  }, [analytics, advertising]);
}

function pushRaw(page, js) {
  return page.evaluate((code) => { window.dataLayer.push(new Function('return (' + code + ')')()); }, js); // test-side only
}

async function waitFor(page, fn, what) {
  await page.waitForFunction(fn, null, { timeout: 3000 }).catch(() => { throw new Error('timed out waiting for ' + what); });
}

/** Node-side poll for intercepted-route bookkeeping, which lags the page by a tick. */
async function until(cond, what) {
  const deadline = Date.now() + 3000;
  while (!cond()) {
    if (Date.now() > deadline) throw new Error('timed out waiting for ' + what);
    await new Promise((r) => setTimeout(r, 25));
  }
}

const sdkLoaded = () => !!(window.WebinarForm && document.querySelector('form[data-webinar-form]') && document.querySelector('form[data-webinar-form]').__webinarForm);
const consent = (page) => page.evaluate(() => window.WebinarForm.getConsent());
const sdkScripts = (page) => page.evaluate(() => document.querySelectorAll('script[src$="webinar-form.js"]').length);
const stored = (page) => page.evaluate((k) => sessionStorage.getItem(k), STORAGE_KEY);

async function submit(page) {
  await page.fill('[name="email"]', 'ada@example.com');
  await page.fill('[name="firstName"]', 'Ada');
  const ok = await page.evaluate(() => document.querySelector('form[data-webinar-form]').__webinarForm.submit());
  assert.equal(ok, true, 'registration accepted');
}

const scenarios = [];
function scenario(name, fn, opts) { scenarios.push({ name, fn, opts: opts || {} }); }

scenario('1. DOM Ready fire with an empty dataLayer: SDK loaded once from the fixture URL, denied, form bound', async (t) => {
  const { page, log } = t;
  assert.equal(await page.evaluate(() => typeof window.WebinarForm), 'undefined');
  await fire(page);
  await fire(page); // a second trigger while the file is still loading
  await waitFor(page, sdkLoaded, 'SDK boot');
  assert.deepEqual(log.sdk, [200], 'SDK requested exactly once');
  assert.equal(await sdkScripts(page), 1);
  assert.deepEqual(await consent(page), DENIED);
  await until(() => log.profile.length >= 1, 'recognition call');
  assert.equal(log.profile.length, 1, 'recognition call is functional, not consent-gated');
  assert.ok(log.profile[0].includes('occurrenceId=' + OCC));
  assert.equal(await stored(page), null, 'nothing kept without consent');
  assert.equal(await page.evaluate(() => !!document.getElementById('plain').__webinarForm), false, 'unrelated form untouched');
  await submit(page);
  const body = log.posts[0];
  assert.deepEqual(body.attribution, { parameters: {}, identifiers: {} }, 'no URL, cookie or page data without consent');
  assert.deepEqual(body.consents.analytics, { granted: false });
  assert.equal(await page.evaluate(() => window.dataLayer.filter((m) => m && m.event === 'webinar_registration').length), 0);
});

scenario('2. CMP grant, partial grant and revoke through dataLayer pushes; sessionStorage follows', async (t) => {
  const { page, log } = t;
  await fire(page);
  await waitFor(page, sdkLoaded, 'SDK boot');

  await pushConsent(page, 'granted', 'granted');
  await fire(page);
  assert.deepEqual(await consent(page), { analytics: true, advertising: true });
  assert.deepEqual(JSON.parse(await stored(page)), { utm_source: 'news', utm_medium: 'email', gclid: 'abc123' }, 'landing parameters captured on grant, allowlist only');
  await submit(page);
  const granted = log.posts[0];
  assert.deepEqual(granted.attribution.parameters, { utm_source: 'news', utm_medium: 'email', gclid: 'abc123' });
  assert.deepEqual(granted.attribution.identifiers, { gaClientId: '123.456' });
  assert.equal(granted.attribution.pageUrl, ORIGIN + PAGE_PATH);
  assert.equal(await page.evaluate(() => window.dataLayer.filter((m) => m && m.event === 'webinar_registration').length), 1);

  await pushConsent(page, 'granted', 'denied');
  await fire(page);
  assert.deepEqual(await consent(page), { analytics: true, advertising: false });
  assert.deepEqual(JSON.parse(await stored(page)), { utm_source: 'news', utm_medium: 'email' }, 'ad click ID dropped on partial revoke');

  await pushConsent(page, 'denied', 'denied');
  await fire(page);
  assert.deepEqual(await consent(page), DENIED);
  assert.equal(await stored(page), null, 'revoke clears stored landing parameters');

  await pushConsent(page, true, 'true');
  await fire(page);
  assert.deepEqual(await consent(page), { analytics: true, advertising: true });
  await pushConsent(page, 'yes', 1);
  await fire(page);
  assert.deepEqual(await consent(page), DENIED, 'non-exact values deny');
  assert.deepEqual(log.sdk, [200]);
  assert.equal(await sdkScripts(page), 1, 'never injected twice');
});

scenario('3. malformed newest message denies rather than falling back to the earlier grant', async (t) => {
  const { page } = t;
  await fire(page);
  await waitFor(page, sdkLoaded, 'SDK boot');
  const malformed = [
    "{ webinarConsent: 'granted' }",
    "{ webinarConsent: ['granted'] }",
    '{ webinarConsent: null }',
    '{ webinarConsent: {} }',
    "{ webinarConsent: { statistics: 'granted', marketing: 'granted' } }",
    "{ webinarConsent: Object.create({ analytics: 'granted', advertising: 'granted' }) }",
  ];
  for (const m of malformed) {
    await pushConsent(page, 'granted', 'granted');
    await fire(page);
    assert.deepEqual(await consent(page), { analytics: true, advertising: true }, 'precondition');
    await pushRaw(page, m);
    await fire(page);
    assert.deepEqual(await consent(page), DENIED, 'denied after ' + m);
    assert.equal(await stored(page), null);
  }
  // Messages without the key are skipped; an inherited key is not an own key.
  await pushConsent(page, 'granted', 'denied');
  await pushRaw(page, "{ event: 'gtm.historyChange' }");
  await pushRaw(page, "Object.create({ webinarConsent: { analytics: 'denied', advertising: 'denied' } })");
  await fire(page);
  assert.deepEqual(await consent(page), { analytics: true, advertising: false });
});

scenario('4. quotes, newlines and script strings in consent values are values, never code', async (t) => {
  const { page, errors } = t;
  await fire(page);
  await waitFor(page, sdkLoaded, 'SDK boot');
  await page.evaluate(() => { window.pwned = false; });
  const payloads = [
    '";window.pwned=true;//',
    "';window.pwned=true;//",
    '</script><script>window.pwned=true</script>',
    'granted\n',
    '{{DLV - consent analytics}}',
    '${window.pwned=true}',
  ];
  for (const p of payloads) {
    await pushConsent(page, 'granted', 'granted');
    await fire(page);
    await pushConsent(page, p, p);
    await fire(page);
    assert.deepEqual(await consent(page), DENIED, 'denied for ' + JSON.stringify(p));
    assert.equal(await page.evaluate(() => window.pwned), false);
  }
  assert.deepEqual(errors, [], 'no uncaught errors');
});

scenario('5. "Always denied" tag ignores dataLayer grants', async (t) => {
  const { page } = t;
  await pushConsent(page, 'granted', 'granted');
  await fire(page, TAG_DENIED);
  await waitFor(page, sdkLoaded, 'SDK boot');
  assert.deepEqual(await consent(page), DENIED);
  await pushConsent(page, true, true);
  await fire(page, TAG_DENIED);
  assert.deepEqual(await consent(page), DENIED);
  assert.equal(await stored(page), null);
});

scenario('6. consent pushed between the first fire and SDK arrival is honoured at boot', async (t) => {
  const { page } = t;
  // addScriptTag resolves once the inline tag has run, not when the injected async SDK file has
  // executed, so the push usually lands while the file is still loading. If the file wins the race,
  // the second fire applies the consent through setConsent instead; the outcome must be the same.
  await fire(page);
  await pushConsent(page, 'granted', 'denied');
  await fire(page);
  await waitFor(page, sdkLoaded, 'SDK boot');
  assert.deepEqual(await consent(page), { analytics: true, advertising: false });
  assert.equal(await sdkScripts(page), 1);
});

scenario('7. failed first load: the next fire retries, later fires never duplicate', async (t) => {
  const { page, log, errors } = t;
  await fire(page);
  await waitFor(page, () => document.querySelectorAll('script[src$="webinar-form.js"]').length === 1 && window.__webinarFormGtmLoading === false, 'onerror to clear the guard');
  assert.deepEqual(log.sdk, [404]);
  assert.equal(await page.evaluate(() => typeof window.WebinarForm), 'undefined');
  await fire(page);
  await waitFor(page, sdkLoaded, 'SDK boot after retry');
  assert.deepEqual(log.sdk, [404, 200]);
  await fire(page);
  await fire(page);
  assert.deepEqual(log.sdk, [404, 200], 'no further requests once loaded');
  assert.deepEqual(errors, []);
}, { sdkStatuses: [404] });

scenario('8. SPA: a form rendered after load is bound by the next (after-render) fire only', async (t) => {
  const { page, log } = t;
  await fire(page);
  await waitFor(page, sdkLoaded, 'SDK boot');
  await page.evaluate((occ) => {
    const f = document.createElement('form');
    f.setAttribute('data-webinar-form', '');
    f.setAttribute('data-occurrence-id', occ);
    f.setAttribute('data-form-version', 'v2');
    f.id = 'late';
    f.innerHTML = '<div data-webinar-field><label for="l-email">Email</label><input id="l-email" name="email" type="email"></div><button type="submit">Go</button>';
    document.body.appendChild(f);
    history.pushState({}, '', '/webinar/next');
  }, OCC);
  await page.waitForTimeout(150);
  assert.equal(await page.evaluate(() => !!document.getElementById('late').__webinarForm), false, 'pushState alone binds nothing');
  await page.evaluate(() => window.dataLayer.push({ event: 'app_view_rendered' }));
  await fire(page); // the app's after-render custom event trigger
  await waitFor(page, () => !!document.getElementById('late').__webinarForm, 'late form bound');
  await until(() => log.profile.length >= 2, 'late form recognition call');
  assert.equal(log.profile.length, 2, 'late form made its own recognition call, the first form was not re-bound');
  assert.equal(await sdkScripts(page), 1);
});

scenario('9. page without an embedded form: SDK loads, nothing is bound or requested', async (t) => {
  const { page, log } = t;
  await pushConsent(page, 'granted', 'granted');
  await fire(page);
  await waitFor(page, () => !!window.WebinarForm, 'SDK boot');
  await fire(page);
  await page.waitForTimeout(150);
  assert.equal(log.profile.length, 0);
  assert.equal(log.posts.length, 0);
  assert.equal(await page.evaluate(() => window.dataLayer.length), 1, 'tag pushes nothing of its own');
}, { withForm: false });

scenario('10. SDK already loaded by the per-session script tag: consent applies, custom proxy warns once', async (t) => {
  const { page, log, warnings } = t;
  await page.addScriptTag({ url: SDK_URL }); // the embed snippet's <script src>
  await waitFor(page, sdkLoaded, 'SDK boot from the embed script');
  const custom = TAG.replace('"proxyBase": "/webinar-api"', '"proxyBase": "/forms/api"');
  await pushConsent(page, 'granted', 'denied');
  await fire(page, custom);
  await fire(page, custom);
  assert.deepEqual(await consent(page), { analytics: true, advertising: false });
  assert.deepEqual(log.sdk, [200], 'never loaded a second time');
  assert.equal(warnings.filter((w) => /proxy path in the GTM tag is ignored/.test(w)).length, 1);
  await until(() => log.profile.length >= 1, 'recognition call');
  assert.equal(log.profile.length, 1, 'recognition went to the default /webinar-api path (the intercepted route)');
  assert.equal(log.foreign.filter((u) => u.includes('/forms/api/')).length, 0);
});

async function main() {
  const launch = { headless: true };
  if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE) launch.executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
  browser = await chromium.launch(launch);
  let failed = 0;
  for (const s of scenarios) {
    let opened;
    try {
      opened = await openPage(s.opts);
      await s.fn(opened);
      assert.deepEqual(opened.log.foreign, [], 'no request left the intercepted routes');
      assert.deepEqual(opened.errors, [], 'no uncaught page errors');
      console.log('ok   ' + s.name);
    } catch (err) {
      failed++;
      console.error('FAIL ' + s.name);
      console.error(err && err.stack ? err.stack : err);
    } finally {
      if (opened) await opened.context.close().catch(() => {});
    }
  }
  console.log(`\n${scenarios.length - failed}/${scenarios.length} gtm scenarios passed`);
  if (failed) process.exitCode = 1;
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close().catch(() => {});
});
