'use strict';

// Runs the exact tag Studio generates (fixtures/gtm-tag.html, asserted byte-for-byte by
// tests/test_gtm_tracking.py) inside a sandboxed fake window, the way GTM would fire it.
// Consent reaches the tag only through window.dataLayer messages; the tag has no {{variable}}.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SDK_PATH = path.join(__dirname, '..', '..', 'webinars', 'static', 'webinar-sdk', 'webinar-form.js');
const LOADER_PATH = path.join(__dirname, '..', '..', 'webinars', 'static', 'webinar-sdk', 'gtm-loader.js');
const GOLDEN = fs.readFileSync(path.join(__dirname, 'fixtures', 'gtm-tag.html'), 'utf8');
const SDK_SOURCE = fs.readFileSync(SDK_PATH, 'utf8');
const STORAGE_KEY = 'webinarForm.attribution.v1';

const tagScript = GOLDEN.slice(GOLDEN.indexOf('<script>') + '<script>'.length, GOLDEN.indexOf('</script>'));
// The "Always denied" variant Studio emits differs only in the consent key.
const deniedTagScript = tagScript.replace('"consentKey": "webinarConsent"', '"consentKey": null');
assert.notEqual(deniedTagScript, tagScript);

const DENIED = { analytics: false, advertising: false };

/** Objects created inside the vm context have a different Object.prototype; compare by value. */
function same(actual, expected, message) {
  assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected, message);
}

/** Fires the tag exactly as GTM would: no substitution, the script text runs as-is. */
function fire(ctx, script) {
  vm.runInContext(script || tagScript, ctx);
}

/** What a CMP bridge pushes. Built inside the sandbox so the message is a sandbox-realm object. */
function push(w, message) {
  vm.runInContext('window.dataLayer.push(' + message + ');', w.ctx);
}

function grantPush(analytics, advertising) {
  return "{ event: 'webinar_consent_update', webinarConsent: { analytics: " + JSON.stringify(analytics) +
    ', advertising: ' + JSON.stringify(advertising) + ' } }';
}

function fakeElement(doc, tag) {
  const attrs = {};
  const el = {
    tagName: tag.toUpperCase(),
    ownerDocument: doc,
    children: [],
    elements: [],
    hidden: false,
    disabled: false,
    textContent: '',
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    removeAttribute(k) { delete attrs[k]; },
    appendChild(c) { el.children.push(c); return c; },
    insertBefore(c) { el.children.unshift(c); return c; },
    get firstChild() { return el.children[0] || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    contains() { return false; },
  };
  return el;
}

function fakeStorage() {
  const items = {};
  const log = [];
  return {
    log,
    getItem(k) { return Object.prototype.hasOwnProperty.call(items, k) ? items[k] : null; },
    setItem(k, v) { items[k] = String(v); log.push(['set', k, String(v)]); },
    removeItem(k) { delete items[k]; log.push(['remove', k]); },
  };
}

function makeWindow(opts) {
  opts = opts || {};
  const forms = [];
  const doc = {
    readyState: 'complete',
    cookie: '',
    referrer: '',
    currentScript: null,
    head: null,
    querySelectorAll(sel) { return sel === 'form[data-webinar-form]' ? forms.slice() : []; },
    addEventListener() {},
    createElement(tag) { return fakeElement(doc, tag); },
  };
  doc.head = fakeElement(doc, 'head');
  const warnings = [];
  const sandbox = {
    document: doc,
    URL,
    URLSearchParams,
    console: { warn: (m) => warnings.push(String(m)), log() {}, error() {} },
    sessionStorage: fakeStorage(),
    location: { search: opts.search || '', href: 'https://site.test/page' + (opts.search || '') },
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  vm.runInContext('window.dataLayer = [];', ctx); // a sandbox-realm array, as on a real page
  return { ctx, sandbox, doc, forms, scripts: doc.head.children, warnings, storage: sandbox.sessionStorage };
}

function addForm(w) {
  const form = fakeElement(w.doc, 'form');
  form.setAttribute('data-webinar-form', '');
  w.forms.push(form);
  return form;
}

function loadSdk(w) {
  vm.runInContext(SDK_SOURCE, w.ctx); // the injected script "arrives"
  return w.sandbox.WebinarForm;
}

test('fixture inlines the public loader body verbatim, holds no secrets and no GTM variable references', () => {
  const src = fs.readFileSync(LOADER_PATH, 'utf8');
  const body = src.slice(src.indexOf('// @gtm-loader-start') + '// @gtm-loader-start'.length, src.indexOf('// @gtm-loader-end')).replace(/^\n+|\n+$/g, '');
  assert.ok(tagScript.includes(body));
  assert.equal((GOLDEN.match(/<\/script/g) || []).length, 1);
  assert.equal((GOLDEN.match(/<script/g) || []).length, 1);
  assert.doesNotMatch(GOLDEN, /wbk_|Bearer|@/);
  assert.doesNotMatch(GOLDEN, /javascript:/);
  assert.doesNotMatch(GOLDEN, /\{\{/, 'no {{variable}} substitution points');
  assert.doesNotMatch(GOLDEN, /\beval\b|new Function|setTimeout\(|innerHTML/);
  assert.ok(GOLDEN.includes('"consentKey": "webinarConsent"'));
});

test('empty dataLayer: denied at boot, script injected once, SDK boots with no consent', () => {
  const w = makeWindow();
  fire(w.ctx);
  assert.equal(w.scripts.length, 1);
  assert.equal(w.scripts[0].src, 'https://www.example.com/assets/webinar-form.js');
  assert.equal(w.scripts[0].async, true);
  assert.equal(typeof w.scripts[0].onerror, 'function');
  same(w.sandbox.WebinarFormConfig, { proxyBase: '/webinar-api', autoInit: true, consent: DENIED });

  // A second fire while the script is still loading does not inject it again.
  fire(w.ctx);
  assert.equal(w.scripts.length, 1);

  const form = addForm(w);
  const sdk = loadSdk(w);
  assert.equal(typeof sdk, 'object');
  same(sdk.getConsent(), DENIED);
  assert.ok(form.__webinarForm, 'existing opted-in form is bound at boot');
  assert.equal(form.__webinarForm.proxyBase, '/webinar-api');
  same(w.storage.log, [['remove', STORAGE_KEY]], 'nothing stored without consent');
  assert.deepEqual(w.warnings, []);
});

test('grant, partial and revoke through CMP pushes and repeated fires; SDK never loads twice', () => {
  const w = makeWindow({ search: '?utm_source=news&gclid=abc' });
  fire(w.ctx);
  const sdk = loadSdk(w);

  push(w, grantPush('granted', 'granted'));
  fire(w.ctx);
  same(sdk.getConsent(), { analytics: true, advertising: true });
  assert.equal(w.scripts.length, 1);
  assert.equal(w.sandbox.WebinarForm, sdk, 'same SDK instance');
  const stored = w.storage.log.filter((e) => e[0] === 'set');
  assert.equal(stored.length, 1, 'landing parameters captured once consent is granted');
  same(JSON.parse(stored[0][2]), { utm_source: 'news', gclid: 'abc' });

  push(w, grantPush('granted', 'denied'));
  fire(w.ctx);
  same(sdk.getConsent(), { analytics: true, advertising: false });

  push(w, grantPush('denied', 'denied'));
  fire(w.ctx);
  same(sdk.getConsent(), DENIED);
  assert.deepEqual(w.storage.log[w.storage.log.length - 1], ['remove', STORAGE_KEY], 'revoke clears stored landing parameters');
  assert.equal(w.storage.getItem(STORAGE_KEY), null);

  // Only exact grant values count; CMP quirks such as "yes", "1", "GRANTED" or 1 stay denied.
  push(w, grantPush('yes', '1'));
  fire(w.ctx);
  same(sdk.getConsent(), DENIED);
  push(w, "{ webinarConsent: { analytics: 'GRANTED', advertising: 1 } }");
  fire(w.ctx);
  same(sdk.getConsent(), DENIED);
  push(w, "{ webinarConsent: { analytics: true, advertising: 'true' } }");
  fire(w.ctx);
  same(sdk.getConsent(), { analytics: true, advertising: true });

  // Messages without the key (page views, other events) are skipped; the newest with the key still wins.
  push(w, "{ event: 'gtm.historyChange' }");
  push(w, "{ event: 'page_view', page: '/x' }");
  fire(w.ctx);
  same(sdk.getConsent(), { analytics: true, advertising: true });
  assert.deepEqual(w.warnings, []);
});

test('a malformed newest consent message denies instead of falling back to an earlier grant', () => {
  const w = makeWindow();
  fire(w.ctx);
  const sdk = loadSdk(w);
  const malformed = [
    "{ webinarConsent: 'granted' }",
    "{ webinarConsent: ['granted', 'granted'] }",
    '{ webinarConsent: null }',
    '{ webinarConsent: true }',
    '{ webinarConsent: {} }',
    "{ webinarConsent: { analytics: { granted: true }, advertising: 'granted' } }",
    "{ webinarConsent: Object.create({ analytics: 'granted', advertising: 'granted' }) }", // inherited, not own
    "{ webinarConsent: { statistics: 'granted', marketing: 'granted' } }", // wrong purpose names
  ];
  for (const m of malformed) {
    push(w, grantPush('granted', 'granted'));
    fire(w.ctx);
    same(sdk.getConsent(), { analytics: true, advertising: true }, 'precondition: granted');
    push(w, m);
    fire(w.ctx);
    same(sdk.getConsent(), DENIED, 'denied after ' + m);
  }
  // Only own properties on the message are read: an inherited key is skipped, not matched.
  push(w, grantPush('granted', 'granted'));
  push(w, "Object.create({ webinarConsent: { analytics: 'denied', advertising: 'denied' } })");
  fire(w.ctx);
  same(sdk.getConsent(), { analytics: true, advertising: true }, 'inherited key skipped, newest own key wins');
  // Non-object entries (GTM pushes arguments objects and functions too) are skipped.
  push(w, "'string'");
  push(w, 'function () { return { analytics: "granted" }; }');
  push(w, "(function () { return arguments; })('webinarConsent')");
  fire(w.ctx);
  same(sdk.getConsent(), { analytics: true, advertising: true });
});

test('quotes, newlines and script strings in consent values are values, never code', () => {
  const w = makeWindow();
  fire(w.ctx);
  const sdk = loadSdk(w);
  w.sandbox.pwned = false;
  const payloads = [
    '";window.pwned=true;//',
    "';window.pwned=true;//",
    '</script><script>window.pwned=true</script>',
    'granted\n',
    ' granted',
    '{{DLV - consent analytics}}',
    '${window.pwned=true}',
  ];
  for (const p of payloads) {
    push(w, grantPush('granted', 'granted'));
    fire(w.ctx);
    push(w, grantPush(p, p));
    assert.doesNotThrow(() => fire(w.ctx), 'tag must not throw on ' + JSON.stringify(p));
    same(sdk.getConsent(), DENIED, 'denied for ' + JSON.stringify(p));
    assert.equal(w.sandbox.pwned, false);
  }
  assert.equal(w.scripts.length, 1);
});

test('"Always denied" tag ignores grants in the dataLayer', () => {
  const w = makeWindow();
  push(w, grantPush('granted', 'granted'));
  fire(w.ctx, deniedTagScript);
  same(w.sandbox.WebinarFormConfig.consent, DENIED);
  const sdk = loadSdk(w);
  push(w, grantPush(true, true));
  fire(w.ctx, deniedTagScript);
  same(sdk.getConsent(), DENIED);
});

test('consent granted before the SDK arrives is honoured at boot; a missing dataLayer denies', () => {
  const w = makeWindow();
  fire(w.ctx);
  push(w, grantPush('granted', 'denied'));
  fire(w.ctx);
  same(w.sandbox.WebinarFormConfig.consent, { analytics: true, advertising: false });
  same(loadSdk(w).getConsent(), { analytics: true, advertising: false });

  const noDl = makeWindow();
  vm.runInContext('delete window.dataLayer;', noDl.ctx);
  assert.doesNotThrow(() => fire(noDl.ctx));
  same(noDl.sandbox.WebinarFormConfig.consent, DENIED);
  vm.runInContext("window.dataLayer = { push: function () {}, length: 1, 0: { webinarConsent: { analytics: 'granted' } } };", noDl.ctx);
  fire(noDl.ctx);
  same(noDl.sandbox.WebinarFormConfig.consent, DENIED, 'array-likes that are not arrays are ignored');
});

test('a failed script load clears the guard so the next fire retries; a loading script is never duplicated', () => {
  const w = makeWindow();
  fire(w.ctx);
  fire(w.ctx);
  fire(w.ctx);
  assert.equal(w.scripts.length, 1, 'duplicate fires while loading inject nothing');
  w.scripts[0].onerror(); // 404 / CSP block
  assert.equal(w.sandbox.__webinarFormGtmLoading, false);
  fire(w.ctx);
  assert.equal(w.scripts.length, 2, 'retried after the failure');
  assert.equal(w.scripts[1].src, 'https://www.example.com/assets/webinar-form.js');
  loadSdk(w);
  fire(w.ctx);
  fire(w.ctx);
  assert.equal(w.scripts.length, 2, 'never injected once the SDK is present');
});

test('SPA: forms rendered after load are bound on the next explicit fire, bound forms are left alone', () => {
  const w = makeWindow();
  fire(w.ctx);
  loadSdk(w);
  const first = addForm(w);
  assert.equal(first.__webinarForm, undefined, 'nothing binds without a fire or init()');
  fire(w.ctx); // the app's after-render custom event
  assert.ok(first.__webinarForm);
  const controller = first.__webinarForm;
  const second = addForm(w);
  push(w, grantPush('granted', 'denied'));
  fire(w.ctx);
  assert.equal(first.__webinarForm, controller, 'not re-bound');
  assert.ok(second.__webinarForm, 'late form bound');
  assert.equal(w.scripts.length, 1);
});

test('an unrelated form without the opt-in attribute is never touched; the tag pushes nothing', () => {
  const w = makeWindow();
  push(w, grantPush('granted', 'granted'));
  fire(w.ctx);
  const plain = fakeElement(w.doc, 'form');
  w.doc.querySelectorAll = () => []; // nothing opted in on this page
  loadSdk(w);
  fire(w.ctx);
  assert.equal(plain.__webinarForm, undefined);
  assert.equal(vm.runInContext('window.dataLayer.length', w.ctx), 1, 'only the CMP push is in the dataLayer');
});

test('SDK already loaded by a per-session script tag: consent applies, custom proxy path warns once', () => {
  const w = makeWindow();
  loadSdk(w); // <script src> from the embed snippet booted first, default proxy
  const customProxy = tagScript.replace('"proxyBase": "/webinar-api"', '"proxyBase": "/forms/api"');
  push(w, grantPush('granted', 'denied'));
  fire(w.ctx, customProxy);
  fire(w.ctx, customProxy);
  same(w.sandbox.WebinarForm.getConsent(), { analytics: true, advertising: false });
  assert.equal(w.scripts.length, 0, 'never injects a second copy');
  assert.equal(w.warnings.length, 1);
  assert.match(w.warnings[0], /proxy path in the GTM tag is ignored/);

  const def = makeWindow();
  loadSdk(def);
  fire(def.ctx);
  assert.deepEqual(def.warnings, [], 'no warning when the paths agree');
});
