'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
// The canonical SDK (there is no sdk/src copy).
const sdk = require('../../webinars/static/webinar-sdk/webinar-form.js');

const i = sdk._internal;
const NONE = { analytics: false, advertising: false };
const ANALYTICS = { analytics: true, advertising: false };
const ALL = { analytics: true, advertising: true };

function control(name, value, extra = {}) {
  const attrs = extra.attrs || {};
  return {
    name,
    value,
    type: extra.type || 'text',
    checked: !!extra.checked,
    disabled: !!extra.disabled,
    getAttribute: (k) => (Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null),
  };
}

test('loads in Node without a DOM and does not auto-boot', () => {
  assert.equal(typeof sdk.init, 'function');
  assert.deepEqual(sdk.init(), []);
  assert.deepEqual(sdk.getConsent(), NONE);
});

test('stripUrl keeps origin and path only', () => {
  assert.equal(i.stripUrl('https://www.example.com/webinars/a?utm_source=x&email=a@b.c#frag'), 'https://www.example.com/webinars/a');
  assert.equal(i.stripUrl('javascript:alert(1)'), '');
  assert.equal(i.stripUrl('not a url'), '');
  assert.equal(i.stripUrl(''), '');
});

test('no attribution of any kind before consent', () => {
  const ctx = {
    search: '?utm_source=news&gclid=abc',
    href: 'https://www.example.com/w?utm_source=news',
    referrer: 'https://google.com/search?q=secret',
    cookie: '_ga=GA1.1.123.456; _fbp=fb.1.1.2',
  };
  assert.deepEqual(i.buildAttribution(ctx, NONE), { parameters: {}, identifiers: {} });
  assert.deepEqual(i.buildAttribution(ctx, undefined), { parameters: {}, identifiers: {} });
  assert.deepEqual(i.buildAttribution(ctx, { analytics: 'yes' }), { parameters: {}, identifiers: {} });
});

test('analytics consent: UTMs, GA client id, stripped URLs; no ad IDs', () => {
  const a = i.buildAttribution({
    search: '?utm_source=news&utm_medium=email&gclid=abc&fbclid=f&email=a@b.c&random=1',
    href: 'https://www.example.com/w?utm_source=news#x',
    referrer: 'https://google.com/search?q=secret',
    cookie: '_ga=GA1.1.123456.789; _fbp=fb.1.1.2; session=topsecret',
  }, ANALYTICS);
  assert.deepEqual(a.parameters, { utm_source: 'news', utm_medium: 'email' });
  assert.deepEqual(a.identifiers, { gaClientId: '123456.789' });
  assert.equal(a.pageUrl, 'https://www.example.com/w');
  const ph = encodeURIComponent(JSON.stringify({ distinct_id: '0192f4c1-5b7e-7a3c-9d2e-1f0a2b3c4d5e', $sesid: [1, 'x'] }));
  const withPh = i.buildAttribution({ search: '', href: '', referrer: '', cookie: 'ph_phc_abc123_posthog=' + ph + '; _ga=GA1.1.1.2' }, ANALYTICS);
  assert.deepEqual(withPh.identifiers, { gaClientId: '1.2', posthogDistinctId: '0192f4c1-5b7e-7a3c-9d2e-1f0a2b3c4d5e' });
  const noConsent = i.buildAttribution({ search: '', href: '', referrer: '', cookie: 'ph_phc_abc123_posthog=' + ph }, { analytics: false, advertising: true });
  assert.deepEqual(noConsent.identifiers, {});
  assert.equal(i.readPostHogDistinctId('ph_phc_abc_posthog=%7Bnot-json'), null);
  assert.equal(i.readPostHogDistinctId('ph_phc_abc_posthog=' + encodeURIComponent(JSON.stringify({ distinct_id: 'has space' }))), null);
  assert.equal(i.readPostHogDistinctId('other=' + ph), null);
  const mp = encodeURIComponent(JSON.stringify({ distinct_id: '$device:1a2b3c4d-0000-4000-8000-000000000001', $device_id: '1a2b3c4d-0000-4000-8000-000000000001', __alias: 'x' }));
  const amp = btoa(encodeURIComponent(JSON.stringify({ deviceId: 'f0e1d2c3-b4a5-4968-8776-655443322110', sessionId: 1, lastEventTime: 2 })));
  const vendors = i.buildAttribution({ search: '', href: '', referrer: '', cookie: 'mp_phc123_mixpanel=' + mp + '; AMP_a2dbce0e18=' + amp }, ANALYTICS);
  assert.deepEqual(vendors.identifiers, { mixpanelDeviceId: '1a2b3c4d-0000-4000-8000-000000000001', amplitudeDeviceId: 'f0e1d2c3-b4a5-4968-8776-655443322110' });
  assert.equal(i.readMixpanelDeviceId('mp_t_mixpanel=' + encodeURIComponent(JSON.stringify({ distinct_id: '$device:abcdefgh-1234' }))), 'abcdefgh-1234');
  assert.equal(i.readMixpanelDeviceId('mp_t_mixpanel=%7Bnope'), null);
  assert.equal(i.readAmplitudeDeviceId('AMP_a2dbce0e18=' + encodeURIComponent(amp)), 'f0e1d2c3-b4a5-4968-8776-655443322110');
  assert.equal(i.readAmplitudeDeviceId('AMP_MKTG_a2dbce0e18=' + amp), null);
  assert.equal(i.readAmplitudeDeviceId('AMP_a2dbce0e18=not-base64!'), null);
  assert.deepEqual(i.buildAttribution({ search: '', href: '', referrer: '', cookie: 'mp_phc123_mixpanel=' + mp + '; AMP_a2dbce0e18=' + amp }, { analytics: false, advertising: true }).identifiers, {});
  assert.equal(a.referrer, 'https://google.com/search');
});

test('advertising consent only: ad IDs, no page URL / GA', () => {
  const a = i.buildAttribution({
    search: '?utm_source=news&gclid=abc&ScCid=s1&rdt_cid=r1&twclid=t1&keyword={keyword}&campaignid=123&network=',
    href: 'https://www.example.com/w',
    referrer: '',
    cookie: '_ga=GA1.1.1.2; _fbp=fb.1.1690000000.123; _fbc=fb.1.1690000000.IwAR; _gcl_aw=GCL.1690000000.Cj0; _scid=abc-123; _rdt_uuid=1690000000.uuid; _twclid=%7B%22twclid%22%7D',
  }, { analytics: false, advertising: true });
  assert.deepEqual(a.parameters, { gclid: 'abc', twclid: 't1', ScCid: 's1', rdt_cid: 'r1', campaignid: '123' });
  assert.deepEqual(a.identifiers, {
    gclAw: 'GCL.1690000000.Cj0',
    fbp: 'fb.1.1690000000.123',
    fbc: 'fb.1.1690000000.IwAR',
    twclid: '%7B%22twclid%22%7D',
    scid: 'abc-123',
    rdtUuid: '1690000000.uuid',
  });
  assert.equal(a.pageUrl, undefined);
  assert.equal(a.referrer, undefined);
});

test('unsubstituted ValueTrack and oversized/blank values are dropped', () => {
  assert.equal(i.isPopulated('{keyword}'), false);
  assert.equal(i.isPopulated('{{campaign}}'), false);
  assert.equal(i.isPopulated('   '), false);
  assert.equal(i.isPopulated('x'.repeat(501)), false);
  assert.equal(i.isPopulated('brand shoes'), true);
  assert.deepEqual(i.collectParameters('?matchtype={matchtype}&device=m', ALL), { device: 'm' });
});

test('malformed or missing cookies are skipped', () => {
  assert.deepEqual(i.collectIdentifiers('_ga=junk; _fbp=<script>; _scid=', ALL), {});
  assert.deepEqual(i.collectIdentifiers('', ALL), {});
  assert.equal(i.readCookie('a_ga=1; _ga=GA1.1.2.3', '_ga'), 'GA1.1.2.3');
});

test('session-stored first-touch parameters win and are re-filtered by consent', () => {
  const a = i.buildAttribution({ search: '?utm_source=later&gclid=g2', storedParameters: { utm_source: 'first', gclid: 'g1', evil: 'x' } }, ANALYTICS);
  assert.deepEqual(a.parameters, { utm_source: 'first' });
});

test('buildPayload: canonical fields, aliases, blanks omitted, disabled skipped', () => {
  const { payload, errors } = i.buildPayload([
    control('email', '  ada@example.com '),
    control('first_name', 'Ada'),
    control('lastName', ''),
    control('company', 'Hidden Co', { disabled: true }),
    control('jobTitle', 'Engineer'),
    control('custom.favouriteTopic', 'AI'),
    control('custom.bad key', 'x'),
    control('unknownField', 'ignored'),
    control('organization.externalId', 'acct-1', { attrs: { 'data-namespace': 'crm' } }),
    control('organization.externalId', 'acct-2', { attrs: { 'data-namespace': 'BAD NS' } }),
    control('consents.sms', 'yes', { type: 'checkbox', checked: true, attrs: { 'data-disclosure-version': 'sms-2026-01' } }),
  ], { occurrenceId: 'occ_1', formVersion: 'v3', idempotencyKey: 'key-1', consent: ANALYTICS, attribution: { parameters: {}, identifiers: {} } });
  assert.deepEqual(errors, []);
  assert.deepEqual(payload, {
    schemaVersion: '1',
    occurrenceId: 'occ_1',
    formVersion: 'v3',
    idempotencyKey: 'key-1',
    profile: { email: 'ada@example.com', firstName: 'Ada', jobTitle: 'Engineer' },
    organization: { externalIds: [{ namespace: 'crm', value: 'acct-1' }] },
    consents: {
      analytics: { granted: true },
      advertising: { granted: false },
      sms: { granted: true, disclosureVersion: 'sms-2026-01' },
    },
    attribution: { parameters: {}, identifiers: {} },
    customFields: { favouriteTopic: 'AI' },
  });
  // Client consent is a claim only; no verification flags are ever sent.
  assert.equal(JSON.stringify(payload.consents).includes('verified'), false);
});

test('buildPayload: conflicting aliases rejected, equal ones accepted; never splits names', () => {
  const conflict = i.buildPayload([control('firstName', 'Ada'), control('first_name', 'Grace')], { consent: NONE });
  assert.equal(conflict.errors.length, 1);
  assert.equal(conflict.errors[0].code, 'conflict');
  const same = i.buildPayload([control('firstName', 'Ada'), control('first_name', 'Ada')], { consent: NONE });
  assert.deepEqual(same.errors, []);
  const full = i.buildPayload([control('name', 'Ada Lovelace')], { consent: NONE });
  assert.deepEqual(full.payload.profile, { name: 'Ada Lovelace' });
});

test('buildPayload: SMS checkbox without disclosure version is an error; unchecked records withdrawal claim', () => {
  const missing = i.buildPayload([control('consents.sms', 'yes', { type: 'checkbox', checked: true })], { consent: NONE });
  assert.equal(missing.errors[0].code, 'missing_disclosure_version');
  const unchecked = i.buildPayload([control('consents.sms', 'yes', { type: 'checkbox', checked: false, attrs: { 'data-disclosure-version': 'd1' } })], { consent: NONE });
  assert.deepEqual(unchecked.payload.consents.sms, { granted: false, disclosureVersion: 'd1' });
});

test('explicit per-form field map, restricted to canonical targets', () => {
  const map = i.parseFieldMap('{"org":"company","evil":"__proto__","size":"companySize"}');
  assert.deepEqual(map, { org: 'company', size: 'companySize' });
  assert.deepEqual(i.parseFieldMap('not json'), {});
  const { payload } = i.buildPayload([control('org', 'Acme')], { fieldMap: map, consent: NONE });
  assert.deepEqual(payload.profile, { company: 'Acme' });
});

test('planFields: anonymous or failed lookup shows the full form', () => {
  const keys = ['email', 'firstName', 'company'];
  assert.deepEqual(i.planFields(null, keys), { hide: [], confirmOnly: false, recognized: false });
  assert.deepEqual(i.planFields({ recognized: false, missingFields: ['email'] }, keys), { hide: [], confirmOnly: false, recognized: false });
  assert.deepEqual(i.planFields({ recognized: true }, keys), { hide: [], confirmOnly: false, recognized: false });
});

test('planFields: recognized shows missing fields only, or Confirm my RSVP', () => {
  const keys = ['email', 'firstName', 'company', 'custom.topic'];
  assert.deepEqual(i.planFields({ recognized: true, missingFields: ['company'], formVersion: 'v1' }, keys),
    { hide: ['email', 'firstName', 'custom.topic'], confirmOnly: false, recognized: true });
  const none = i.planFields({ recognized: true, missingFields: [], formVersion: 'v1' }, keys);
  assert.equal(none.confirmOnly, true);
  assert.deepEqual(none.hide, keys);
  assert.equal(i.CONFIRM_LABEL, 'Confirm my RSVP');
});

test('confirm label: data-confirm-label overrides the default, as plain text', () => {
  const form = (v) => ({ getAttribute: (k) => (k === 'data-confirm-label' ? v : null) });
  assert.equal(i.confirmLabel(form(null)), 'Confirm my RSVP');
  assert.equal(i.confirmLabel(form('   ')), 'Confirm my RSVP');
  assert.equal(i.confirmLabel(form('  Save  my\nseat ')), 'Save my seat');
  assert.equal(i.confirmLabel(form('<img src=x onerror=alert(1)>')), '<img src=x onerror=alert(1)>');
  assert.equal(i.confirmLabel(form('x'.repeat(80))).length, 60);
  // Applied via textContent (button) or value (input), never innerHTML.
  const btn = { tagName: 'BUTTON', textContent: 'Register' };
  const ctl = Object.create(i.FormController.prototype);
  Object.assign(ctl, {
    form: {}, submitButton: btn, originalLabel: 'Register', confirmLabel: '<b>Yes</b>',
    fieldMap: {}, messages: { welcomeBackComplete: 'wb' },
    resetButton: { hidden: true }, statusEl: { textContent: '' },
    controls: () => [], _fieldControls: () => [],
  });
  ctl.applyProfile({ recognized: true, missingFields: [] });
  assert.equal(btn.textContent, '<b>Yes</b>');
  assert.equal(btn.innerHTML, undefined);
  ctl.showAll();
  assert.equal(btn.textContent, 'Register');
});

test('idempotency: same intent reuses key, changed intent rotates, reset after completion', () => {
  let n = 0;
  const m = new i.IdempotencyManager(() => `k${++n}`);
  const a = i.intentOf({ profile: { email: 'a@example.com' }, idempotencyKey: 'ignored' });
  const a2 = i.intentOf({ profile: { email: 'a@example.com' }, idempotencyKey: 'other' });
  const b = i.intentOf({ profile: { email: 'b@example.com' } });
  assert.equal(m.keyFor(a), 'k1');
  assert.equal(m.keyFor(a2), 'k1');
  assert.equal(m.keyFor(b), 'k2');
  m.reset();
  assert.equal(m.keyFor(b), 'k3');
});

test('randomKey uses a CSPRNG and fails closed without one', () => {
  const webcrypto = require('node:crypto').webcrypto;
  assert.match(i.randomKey(webcrypto), /^[0-9a-f-]{36}$/);
  assert.match(i.randomKey({ getRandomValues: (b) => webcrypto.getRandomValues(b) }), /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.throws(() => i.randomKey({}), /secure random/);
});

test('safeUrl allows https and same-origin only', () => {
  const base = 'https://www.example.com/webinars/a';
  assert.equal(i.safeUrl('/thanks', base), 'https://www.example.com/thanks');
  assert.equal(i.safeUrl('https://cal.example.org/e.ics', base), 'https://cal.example.org/e.ics');
  assert.equal(i.safeUrl('http://evil.example/', base), null);
  assert.equal(i.safeUrl('javascript:alert(1)', base), null);
  assert.equal(i.safeUrl('data:text/html,x', base), null);
  assert.equal(i.safeUrl(undefined, base), null);
});

const GOOGLE = 'https://calendar.google.com/calendar/render?action=TEMPLATE&text=Demo';
const OUTLOOK = 'https://outlook.live.com/calendar/0/deeplink/compose?rru=addevent&subject=Demo';
const OFFICE365 = 'https://outlook.office.com/calendar/0/deeplink/compose?rru=addevent&subject=Demo';
const MALICIOUS_LINKS = [
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
  '',
  42,
  null,
  { href: GOOGLE },
];

function fakeDoc() {
  return {
    createElement(tag) {
      const attrs = {};
      return {
        tagName: tag.toUpperCase(), attrs, textContent: '',
        setAttribute(k, v) { attrs[k] = String(v); },
        getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
      };
    },
    createTextNode(text) { return { nodeType: 3, text }; },
  };
}

function successController(messages = {}) {
  const children = [];
  const ctl = Object.create(i.FormController.prototype);
  Object.assign(ctl, {
    doc: fakeDoc(),
    messages: Object.assign({}, i.DEFAULT_MESSAGES, messages),
    form: { setAttribute() {} }, submitButton: null, resetButton: { hidden: false },
    statusEl: { textContent: '', appendChild(c) { children.push(c); return c; } },
    _fieldControls: () => [],
  });
  return { ctl, links: () => children.filter((c) => c.tagName === 'A') };
}

test('calendarWebUrl: exact https service path only', () => {
  const [g, o, m] = i.CALENDAR_WEB_LINKS;
  assert.equal(i.calendarWebUrl(GOOGLE, g.base), GOOGLE);
  assert.equal(i.calendarWebUrl(OUTLOOK, o.base), OUTLOOK);
  assert.equal(i.calendarWebUrl(OFFICE365, m.base), OFFICE365);
  assert.equal(i.calendarWebUrl(GOOGLE, o.base), null);
  for (const bad of MALICIOUS_LINKS) {
    assert.equal(i.calendarWebUrl(bad, g.base), null, String(bad));
  }
  assert.deepEqual(i.calendarWebLinks(null), []);
  assert.deepEqual(i.calendarWebLinks({ googleUrl: null, outlookUrl: null, office365Url: null }), []);
});

test('thank-you UI: ICS link unchanged, web calendar links rendered safely and accessibly', () => {
  const { ctl, links } = successController();
  ctl._onSuccess({
    status: 'registered',
    thankYou: { mode: 'message', message: 'See you there' },
    calendar: {
      icsUrl: '/webinar-api/calendar.ics?occurrenceId=7f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f',
      googleUrl: GOOGLE, outlookUrl: OUTLOOK, office365Url: OFFICE365,
      joinUrl: 'https://zoom.example/j/1',
    },
  });
  assert.equal(ctl.statusEl.textContent, 'See you there');
  const all = links();
  assert.equal(all.length, 4);
  // Existing ICS link: first, same attribute and text, no new-tab behaviour.
  const ics = all[0];
  assert.equal(ics.getAttribute('data-webinar-calendar'), '');
  assert.match(ics.href, /\/webinar-api\/calendar\.ics\?occurrenceId=7f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f$/);
  assert.equal(ics.textContent, 'Add to calendar');
  assert.equal(ics.getAttribute('target'), null);
  const web = all.slice(1);
  assert.deepEqual(web.map((a) => a.getAttribute('data-webinar-calendar-link')), ['google', 'outlook', 'office365']);
  assert.deepEqual(web.map((a) => a.href), [GOOGLE, OUTLOOK, OFFICE365]);
  assert.deepEqual(web.map((a) => a.textContent), ['Add to Google Calendar', 'Add to Outlook.com', 'Add to Outlook (Microsoft 365)']);
  for (const a of web) {
    assert.equal(a.getAttribute('data-webinar-calendar'), null);
    assert.equal(a.getAttribute('target'), '_blank');
    assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
    // Accessible name starts with the visible text (WCAG 2.5.3) and announces the new tab.
    assert.equal(a.getAttribute('aria-label'), `${a.textContent} (opens in a new tab)`);
    assert.equal(a.innerHTML, undefined);
  }
  assert.ok(!all.some((a) => /zoom\.example/.test(a.href)));
});

test('thank-you UI: malicious web calendar links are never rendered; ICS still shown', () => {
  for (const bad of MALICIOUS_LINKS) {
    const { ctl, links } = successController();
    ctl._onSuccess({ calendar: { icsUrl: '/webinar-api/calendar.ics?occurrenceId=x', googleUrl: bad, outlookUrl: bad, office365Url: bad } });
    const all = links();
    assert.equal(all.length, 1, String(bad));
    assert.equal(all[0].getAttribute('data-webinar-calendar'), '');
  }
  // Swapped services and null links (cancelled or ended session) render nothing extra.
  const { ctl, links } = successController();
  ctl._onSuccess({ calendar: { googleUrl: OUTLOOK, outlookUrl: OFFICE365, office365Url: null } });
  assert.equal(links().length, 0);
});

test('thank-you UI: link labels are plain text and overridable; blank override falls back', () => {
  const { ctl, links } = successController({ addToGoogleCalendar: '<img src=x onerror=alert(1)>', addToOutlookCalendar: '', opensInNewTab: '(new tab)' });
  ctl._onSuccess({ calendar: { googleUrl: GOOGLE, outlookUrl: OUTLOOK } });
  const [g, o] = links();
  assert.equal(g.textContent, '<img src=x onerror=alert(1)>');
  assert.equal(g.innerHTML, undefined);
  assert.equal(g.getAttribute('aria-label'), '<img src=x onerror=alert(1)> (new tab)');
  assert.equal(o.textContent, 'Add to Outlook.com');
  const keys = Object.keys(i.readOptions().messages);
  for (const k of ['addToGoogleCalendar', 'addToOutlookCalendar', 'addToOffice365Calendar', 'opensInNewTab']) assert.ok(keys.includes(k), k);
});

test('error handling never echoes server text and distinguishes network failure', () => {
  const msgs = { network: 'N', invalid: 'I', conflict: 'C', rateLimited: 'R', server: 'S' };
  assert.equal(i.messageForStatus(0, msgs), 'N');
  assert.equal(i.messageForStatus(422, msgs), 'I');
  assert.equal(i.messageForStatus(409, msgs), 'C');
  assert.equal(i.messageForStatus(429, msgs), 'R');
  assert.equal(i.messageForStatus(502, msgs), 'S');
  assert.deepEqual(i.errorFieldKeys({ errors: { 'profile.email': ['bad'], 'customFields.topic': ['x'] } }), ['email', 'custom.topic']);
  assert.deepEqual(i.errorFieldKeys({ errors: [{ field: 'phone', message: 'Existing user Ada' }] }), ['phone']);
  assert.deepEqual(i.errorFieldKeys(null), []);
});

// Regression (B2): actual backend body from webinars/registration.py error_body().
test('errorFieldKeys reads the backend and proxy error shapes', () => {
  const backend = {
    error: { code: 'validation_error', message: 'Some required fields are missing.', fields: { email: 'Required.', 'custom.topic': 'Required.' } },
    fieldErrors: { email: 'Required.', 'custom.topic': 'Required.' },
  };
  assert.deepEqual(i.errorFieldKeys(backend), ['email', 'custom.topic']);
  assert.deepEqual(i.errorFieldKeys({ error: { code: 'validation_error', fields: { phone: 'x' } } }), ['phone']);
  assert.deepEqual(i.errorFieldKeys({ error: { code: 'validation_error' }, fieldErrors: { 'consents.sms.disclosureVersion': 'invalid' } }), ['consents.sms']);
  assert.deepEqual(i.errorFieldKeys({ error: { code: 'idempotency_conflict', message: 'x' } }), []);
});

// Regression (B6): "/\evil.com" is parsed by URL/fetch as the cross-origin "//evil.com".
test('proxyBase must be a plain first-party path', () => {
  for (const bad of ['/\\evil.com', '/\\/evil.com', '//evil.com', 'https://evil.com', 'webinar-api', '/a/../b', '/a b', '', null, 42]) {
    assert.equal(i.safeProxyBase(bad), '/webinar-api', String(bad));
  }
  assert.equal(i.safeProxyBase('/webinar-api'), '/webinar-api');
  assert.equal(i.safeProxyBase('/api/webinar-proxy/'), '/api/webinar-proxy/');
  const prev = globalThis.WebinarFormConfig;
  try {
    globalThis.WebinarFormConfig = { proxyBase: '/\\evil.com' };
    assert.equal(i.readOptions().proxyBase, '/webinar-api');
  } finally {
    globalThis.WebinarFormConfig = prev;
  }
});

// Regression (B7): every collected parameter/identifier must be accepted by webinars/attribution.py.
test('collected parameters are a subset of the server allowlist', () => {
  const server = new Set([
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'utm_id',
    'campaignid', 'adgroupid', 'keyword', 'matchtype', 'network', 'device', 'creative', 'placement', 'targetid', 'loc_physical_ms',
    'gclid', 'gbraid', 'wbraid', 'fbclid', 'msclkid', 'ttclid', 'twclid', 'ScCid', 'rdt_cid',
  ]);
  for (const p of i.ANALYTICS_PARAMS.concat(i.ADVERTISING_PARAMS)) assert.ok(server.has(p), p);
  const serverIds = new Set(['fbp', 'fbc', 'gaClientId', 'gclAw', 'gclAu', 'scid', 'rdtUuid', 'twclid']);
  for (const d of i.IDENTIFIER_COOKIES) assert.ok(serverIds.has(d.key), d.key);
  const a = i.buildAttribution({ search: '?gad_source=1&utm_source_platform=x&utm_source=ok', href: '', referrer: '', cookie: '' }, { analytics: true, advertising: true });
  assert.deepEqual(a.parameters, { utm_source: 'ok' });
  assert.equal(i.isPopulated('x'.repeat(257)), false);
});

test('resolveFieldName', () => {
  assert.deepEqual(i.resolveFieldName('job_title'), { kind: 'profile', key: 'jobTitle' });
  assert.deepEqual(i.resolveFieldName('custom.topic'), { kind: 'custom', key: 'topic' });
  assert.equal(i.resolveFieldName('fname'), null);
  assert.equal(i.fieldKey(i.resolveFieldName('custom.topic')), 'custom.topic');
  assert.equal(i.fieldKey(i.resolveFieldName('consents.sms')), null);
});

// Paid sessions (docs/tickets.md): 202 payment_required is not a confirmation.
test('checkoutUrl: https on the Stripe Checkout host only', () => {
  assert.equal(i.checkoutUrl('https://checkout.stripe.com/c/pay/cs_test_1'), 'https://checkout.stripe.com/c/pay/cs_test_1');
  for (const bad of ['http://checkout.stripe.com/c', 'https://checkout.stripe.com.evil.example/c', 'https://evil.example/c',
    'https://user@checkout.stripe.com/c', 'https://checkout.stripe.com:8443/c', 'javascript:alert(1)', '', null, 42]) {
    assert.equal(i.checkoutUrl(bad), null, String(bad));
  }
});

function paymentController() {
  const ctl = Object.create(i.FormController.prototype);
  const state = {};
  Object.assign(ctl, {
    messages: Object.assign({}, i.DEFAULT_MESSAGES),
    form: { setAttribute(k, v) { state[k] = v; } },
    _status(text) { state.status = text; },
    _onSuccess() { state.success = true; },
  });
  return { ctl, state };
}

test('payment_required redirects to checkout and never shows a confirmation', () => {
  const { ctl, state } = paymentController();
  const assigned = [];
  const prev = globalThis.location;
  globalThis.location = { href: 'https://www.example.com/w', assign: (u) => assigned.push(u) };
  try {
    const out = ctl._onPaymentRequired({ status: 'payment_required', order: { id: 'o', checkoutUrl: 'https://checkout.stripe.com/c/pay/cs_1' } });
    assert.equal(out, 'payment_required');
    assert.deepEqual(assigned, ['https://checkout.stripe.com/c/pay/cs_1']);
    assert.equal(state['data-webinar-state'], 'payment-required');
    assert.notEqual(state.status, i.DEFAULT_MESSAGES.success);
    assert.equal(state.success, undefined);
    // Unsafe or missing checkout link: no navigation, "preparing" message, still no confirmation.
    assigned.length = 0;
    ctl._onPaymentRequired({ status: 'payment_required', order: { checkoutUrl: 'https://evil.example/pay' } });
    assert.deepEqual(assigned, []);
    assert.equal(state.status, i.DEFAULT_MESSAGES.paymentPreparing);
  } finally {
    globalThis.location = prev;
  }
});

test('paid error codes map to specific messages; unknown codes keep status mapping', () => {
  const msgs = Object.assign({}, i.DEFAULT_MESSAGES);
  assert.equal(i.messageForStatus(409, msgs, { error: { code: 'sold_out' } }), msgs.soldOut);
  assert.equal(i.messageForStatus(409, msgs, { error: { code: 'checkout_in_progress' } }), msgs.checkoutInProgress);
  assert.equal(i.messageForStatus(503, msgs, { error: { code: 'payments_unavailable' } }), msgs.paymentsUnavailable);
  assert.equal(i.messageForStatus(502, msgs, { error: { code: 'checkout_failed' } }), msgs.checkoutFailed);
  assert.equal(i.messageForStatus(409, msgs, { error: { code: 'something_else' } }), msgs.conflict);
  assert.equal(i.messageForStatus(409, msgs, { error: { code: 'sold_out', message: 'Server text' } }), msgs.soldOut);
});

test('checkout_failed and order_closed rotate the idempotency key; other errors keep it', () => {
  assert.equal(i.closesOrder({ error: { code: 'checkout_failed' } }), true);
  assert.equal(i.closesOrder({ error: { code: 'order_closed' } }), true);
  for (const body of [{ error: { code: 'sold_out' } }, { error: { code: 'checkout_in_progress' } }, { error: 'x' }, null, {}]) {
    assert.equal(i.closesOrder(body), false, JSON.stringify(body));
  }
});

test('verifyUrl: https waiting-room path only', () => {
  assert.equal(i.verifyUrl('https://webinars.example.com/w/abc_12-x/'), 'https://webinars.example.com/w/abc_12-x/');
  for (const bad of ['http://webinars.example.com/w/abc/', 'https://evil.example/pay', 'https://x.example/w/abc/?next=1',
    'https://u@x.example/w/abc/', 'https://x.example:8443/w/abc/', 'https://x.example/w/../admin/', 'javascript:alert(1)', '', null]) {
    assert.equal(i.verifyUrl(bad), null, String(bad));
  }
});

test('verification_required redirects to the email check and never confirms or checks out', () => {
  const { ctl, state } = paymentController();
  const assigned = [];
  const prev = globalThis.location;
  globalThis.location = { href: 'https://www.example.com/w', assign: (u) => assigned.push(u) };
  try {
    const url = 'https://webinars.example.com/w/room1/';
    const out = ctl._onVerificationRequired({ status: 'verification_required', verification: { available: true, verifyUrl: url } });
    assert.equal(out, 'verification_required');
    assert.deepEqual(assigned, [url]);
    assert.equal(state['data-webinar-state'], 'verification-required');
    assert.equal(state.success, undefined);
    assigned.length = 0;
    ctl._onVerificationRequired({ status: 'verification_required', verification: { available: false, verifyUrl: null } });
    assert.deepEqual(assigned, []);
    assert.equal(state.status, i.DEFAULT_MESSAGES.verificationUnavailable);
    ctl._onVerificationRequired({ status: 'verification_required', verification: { available: true, verifyUrl: 'https://evil.example/x' } });
    assert.deepEqual(assigned, []);
  } finally {
    globalThis.location = prev;
  }
});
