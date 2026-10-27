/*!
 * webinar-form.js — dependency-free progressive profiling for webinar RSVP forms.
 * Licensed under the Apache License, Version 2.0.
 *
 * Install via a GTM Custom HTML tag or a plain <script> tag. Talks only to a
 * first-party proxy (default "/webinar-api"); holds no secrets. See
 * docs/browser-integration.md.
 */
(function (root, factory) {
  'use strict';
  var api = factory(root);
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  }
  if (root && root.document && !root.WebinarForm) {
    root.WebinarForm = api;
    api._boot();
  }
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this), function (root) {
  'use strict';

  var SCHEMA_VERSION = '1';
  var CANONICAL_FIELDS = ['email', 'phone', 'firstName', 'lastName', 'name', 'company', 'jobTitle', 'companySize'];
  // Common snake_case aliases only. No fuzzy guessing; anything else needs data-field-map.
  var ALIASES = {
    first_name: 'firstName',
    last_name: 'lastName',
    full_name: 'name',
    job_title: 'jobTitle',
    company_name: 'company',
    company_size: 'companySize',
    email_address: 'email',
    phone_number: 'phone'
  };
  var CUSTOM_PREFIX = 'custom.';

  // URL parameters collected only with analytics consent. Must stay a subset of the
  // server allowlist (webinars/attribution.py ALLOWED_PARAMETERS); anything else is dropped.
  var ANALYTICS_PARAMS = [
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'utm_id'
  ];
  // Ad click IDs and Google Ads ValueTrack parameters, collected only with advertising consent.
  var ADVERTISING_PARAMS = [
    'gclid', 'gbraid', 'wbraid', // Google Ads
    'fbclid', // Meta
    'twclid', // X
    'ScCid', // Snap
    'rdt_cid', // Reddit
    'campaignid', 'adgroupid', 'keyword', 'matchtype', 'network', 'device', 'creative', 'placement', 'targetid',
    'loc_physical_ms'
  ];
  // First-party cookies read individually by name. Availability is never guaranteed.
  // Keys are the server's canonical identifier names.
  var IDENTIFIER_COOKIES = [
    { cookie: '_ga', key: 'gaClientId', purpose: 'analytics', parse: parseGaCookie },
    { cookie: '_gcl_aw', key: 'gclAw', purpose: 'advertising' },
    { cookie: '_fbp', key: 'fbp', purpose: 'advertising' },
    { cookie: '_fbc', key: 'fbc', purpose: 'advertising' },
    { cookie: '_twclid', key: 'twclid', purpose: 'advertising' },
    { cookie: '_scid', key: 'scid', purpose: 'advertising' },
    { cookie: '_rdt_uuid', key: 'rdtUuid', purpose: 'advertising' }
  ];
  var MAX_PARAM_LENGTH = 256; // server MAX_VALUE_LENGTH
  var IDENTIFIER_RE = /^[A-Za-z0-9._~:|%\-]{1,256}$/;
  var CSRF_COOKIE = '__Host-webinar_csrf';
  var CSRF_HEADER = 'X-Webinar-CSRF';
  var STORAGE_KEY = 'webinarForm.attribution.v1';
  var REQUEST_TIMEOUT_MS = 15000;
  var CONFIRM_LABEL = 'Confirm my RSVP';
  // Web "add to calendar" links: response key, message key and the exact https origin + path
  // the backend emits (webinars/calendar_links.py). Anything else is not rendered.
  var CALENDAR_WEB_LINKS = [
    { key: 'googleUrl', name: 'google', message: 'addToGoogleCalendar', base: 'https://calendar.google.com/calendar/render' },
    { key: 'outlookUrl', name: 'outlook', message: 'addToOutlookCalendar', base: 'https://outlook.live.com/calendar/0/deeplink/compose' },
    { key: 'office365Url', name: 'office365', message: 'addToOffice365Calendar', base: 'https://outlook.office.com/calendar/0/deeplink/compose' }
  ];
  var MAX_WEB_LINK = 8192;

  var DEFAULT_MESSAGES = {
    welcomeBack: 'Welcome back. We only need the details below.',
    welcomeBackComplete: 'Welcome back. Confirm your RSVP below.',
    notYou: 'Not you? Start over',
    resetDone: 'Form cleared. Please enter your details.',
    resetFailed: 'We could not clear the form. Please try again.',
    submitting: 'Sending your RSVP…',
    success: 'Thanks — your RSVP is confirmed.',
    addToCalendar: 'Add to calendar',
    addToGoogleCalendar: 'Add to Google Calendar',
    addToOutlookCalendar: 'Add to Outlook.com',
    addToOffice365Calendar: 'Add to Outlook (Microsoft 365)',
    opensInNewTab: '(opens in a new tab)',
    network: 'We could not reach the server, so your RSVP was not saved. Please check your connection and try again.',
    invalid: 'Some details need attention. Please review the highlighted fields.',
    conflict: 'This RSVP request conflicts with an earlier one. Please reload the page and try again.',
    rateLimited: 'Too many attempts. Please wait a moment and try again.',
    server: 'Something went wrong on our side, so your RSVP was not saved. Please try again.',
    fieldRequired: 'This field is required.',
    fieldInvalid: 'Please check this field.',
    fieldConflict: 'This value conflicts with another field.',
    // Paid sessions (docs/tickets.md): nothing is confirmed until payment succeeds.
    paymentRedirecting: 'Taking you to secure checkout…',
    paymentPreparing: 'Your seat is held while we prepare checkout. Please submit again in a moment to continue to payment.',
    soldOut: 'This session is sold out.',
    checkoutInProgress: 'A checkout for this registration is already in progress. Finish it, or try again once it expires.',
    paymentsUnavailable: 'Ticket sales are not available right now, so you have not been registered.',
    orderClosed: 'That checkout is no longer available. Please reload the page and try again.',
    checkoutFailed: 'We could not start checkout and nothing was charged. Please try again.',
    verificationRedirecting: 'To buy a ticket, confirm your email first. Taking you there…',
    verificationUnavailable: 'Online ticket sales need email confirmation, which is not available for this session. Please contact the organiser.'
  };

  var CHECKOUT_HOSTS = ['checkout.stripe.com'];
  var ERROR_CODE_MESSAGES = {
    sold_out: 'soldOut',
    checkout_in_progress: 'checkoutInProgress',
    payments_unavailable: 'paymentsUnavailable',
    order_closed: 'orderClosed',
    checkout_failed: 'checkoutFailed'
  };

  // ---------------------------------------------------------------------------
  // Pure helpers (exported for tests)
  // ---------------------------------------------------------------------------

  function hasOwn(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  function parseGaCookie(value) {
    // GA1.1.1234567890.1690000000 -> 1234567890.1690000000
    var parts = String(value).split('.');
    if (parts.length < 4) return null;
    var id = parts.slice(-2).join('.');
    return /^\d{1,20}\.\d{1,20}$/.test(id) ? id : null;
  }

  /** Returns origin + path only; query and hash are never kept. */
  function stripUrl(raw) {
    if (!raw || typeof raw !== 'string') return '';
    try {
      var u = new URL(raw);
      if (u.protocol !== 'https:' && u.protocol !== 'http:') return '';
      return u.origin + u.pathname;
    } catch (e) {
      return '';
    }
  }

  function isPopulated(value) {
    if (typeof value !== 'string') return false;
    var v = value.trim();
    if (!v || v.length > MAX_PARAM_LENGTH) return false;
    // Unsubstituted ValueTrack / macro placeholders such as {keyword} or {{campaign}}.
    if (v.indexOf('{') !== -1 || v.indexOf('}') !== -1) return false;
    return true;
  }

  /** Allowlisted, populated URL parameters permitted by consent. */
  function collectParameters(search, consent) {
    var out = {};
    if (!consent || (!consent.analytics && !consent.advertising)) return out;
    var params;
    try {
      params = new URLSearchParams(search || '');
    } catch (e) {
      return out;
    }
    var allowed = [];
    if (consent.analytics) allowed = allowed.concat(ANALYTICS_PARAMS);
    if (consent.advertising) allowed = allowed.concat(ADVERTISING_PARAMS);
    for (var i = 0; i < allowed.length; i++) {
      var value = params.get(allowed[i]);
      if (isPopulated(value)) out[allowed[i]] = value.trim();
    }
    return out;
  }

  /** Reads only the named cookies; never returns the whole cookie string. */
  function readCookie(cookieString, name) {
    if (!cookieString) return null;
    var pairs = String(cookieString).split(';');
    for (var i = 0; i < pairs.length; i++) {
      var pair = pairs[i];
      var eq = pair.indexOf('=');
      if (eq === -1) continue;
      if (pair.slice(0, eq).trim() === name) return pair.slice(eq + 1).trim();
    }
    return null;
  }

  function collectIdentifiers(cookieString, consent) {
    var out = {};
    if (!consent) return out;
    for (var i = 0; i < IDENTIFIER_COOKIES.length; i++) {
      var def = IDENTIFIER_COOKIES[i];
      if (!consent[def.purpose]) continue;
      var raw = readCookie(cookieString, def.cookie);
      if (raw === null || !IDENTIFIER_RE.test(raw)) continue;
      var value = def.parse ? def.parse(raw) : raw;
      if (value) out[def.key] = value;
    }
    return out;
  }

  function normalizeConsent(c) {
    return { analytics: !!(c && c.analytics === true), advertising: !!(c && c.advertising === true) };
  }

  /**
   * Builds the attribution block. With no consent the block is empty apart
   * from empty containers; page URL and referrer need analytics consent.
   */
  function buildAttribution(ctx, consent) {
    consent = normalizeConsent(consent);
    var attribution = { parameters: {}, identifiers: {} };
    if (!consent.analytics && !consent.advertising) return attribution;
    var params = collectParameters(ctx.search, consent);
    var stored = ctx.storedParameters || {};
    // First-touch-in-session values (captured after consent) win over later pages.
    var allowedNow = collectParameters(toSearch(stored), consent);
    var merged = {};
    var k;
    for (k in params) if (hasOwn(params, k)) merged[k] = params[k];
    for (k in allowedNow) if (hasOwn(allowedNow, k)) merged[k] = allowedNow[k];
    attribution.parameters = merged;
    attribution.identifiers = collectIdentifiers(ctx.cookie, consent);
    if (consent.analytics) {
      var page = stripUrl(ctx.href);
      var ref = stripUrl(ctx.referrer);
      if (page) attribution.pageUrl = page;
      if (ref) attribution.referrer = ref;
    }
    return attribution;
  }

  function toSearch(obj) {
    var parts = [];
    for (var k in obj) {
      if (hasOwn(obj, k) && typeof obj[k] === 'string') {
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(obj[k]));
      }
    }
    return parts.join('&');
  }

  function parseFieldMap(json) {
    if (!json) return {};
    var map;
    try {
      map = JSON.parse(json);
    } catch (e) {
      return {};
    }
    var out = {};
    if (!map || typeof map !== 'object' || Array.isArray(map)) return out;
    for (var k in map) {
      if (hasOwn(map, k) && CANONICAL_FIELDS.indexOf(map[k]) !== -1) out[k] = map[k];
    }
    return out;
  }

  /** Resolves an HTML name attribute to {kind, key} or null. */
  function resolveFieldName(name, fieldMap) {
    if (!name) return null;
    if (fieldMap && hasOwn(fieldMap, name)) return { kind: 'profile', key: fieldMap[name] };
    if (CANONICAL_FIELDS.indexOf(name) !== -1) return { kind: 'profile', key: name };
    if (hasOwn(ALIASES, name)) return { kind: 'profile', key: ALIASES[name] };
    if (name.indexOf(CUSTOM_PREFIX) === 0 && /^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(name.slice(CUSTOM_PREFIX.length))) {
      return { kind: 'custom', key: name.slice(CUSTOM_PREFIX.length) };
    }
    if (name === 'organization.externalId') return { kind: 'externalId', key: name };
    if (name === 'consents.sms') return { kind: 'sms', key: name };
    return null;
  }

  function attr(el, name) {
    return el && typeof el.getAttribute === 'function' ? el.getAttribute(name) : null;
  }

  /**
   * Collects form controls into the registration payload.
   * Returns {payload, errors} where errors is [{name, code}].
   * Blank values are omitted so they cannot erase stored values.
   */
  function buildPayload(controls, ctx) {
    var fieldMap = ctx.fieldMap || {};
    var profile = {};
    var profileSource = {};
    var customFields = {};
    var externalIds = [];
    var sms = null;
    var errors = [];
    for (var i = 0; i < controls.length; i++) {
      var el = controls[i];
      if (!el || el.disabled) continue;
      var resolved = resolveFieldName(el.name, fieldMap);
      if (!resolved) continue;
      var type = (el.type || '').toLowerCase();
      if (resolved.kind === 'sms') {
        if (type !== 'checkbox') continue;
        var disclosure = attr(el, 'data-disclosure-version');
        if (!disclosure) {
          errors.push({ name: el.name, code: 'missing_disclosure_version' });
          continue;
        }
        sms = { granted: !!el.checked, disclosureVersion: disclosure };
        continue;
      }
      if ((type === 'checkbox' || type === 'radio') && !el.checked) continue;
      var value = typeof el.value === 'string' ? el.value.trim() : '';
      if (!value) continue;
      if (resolved.kind === 'profile') {
        if (hasOwn(profile, resolved.key) && profile[resolved.key] !== value) {
          errors.push({ name: el.name, code: 'conflict', other: profileSource[resolved.key] });
          continue;
        }
        profile[resolved.key] = value;
        profileSource[resolved.key] = el.name;
      } else if (resolved.kind === 'custom') {
        customFields[resolved.key] = value;
      } else if (resolved.kind === 'externalId') {
        var ns = attr(el, 'data-namespace');
        if (ns && /^[a-z0-9][a-z0-9_.-]{0,63}$/.test(ns)) externalIds.push({ namespace: ns, value: value });
      }
    }
    var consent = normalizeConsent(ctx.consent);
    var consents = {
      analytics: { granted: consent.analytics },
      advertising: { granted: consent.advertising }
    };
    if (sms) consents.sms = sms;
    var payload = {
      schemaVersion: SCHEMA_VERSION,
      occurrenceId: ctx.occurrenceId,
      formVersion: ctx.formVersion,
      idempotencyKey: ctx.idempotencyKey,
      profile: profile,
      organization: { externalIds: externalIds },
      consents: consents,
      attribution: ctx.attribution || { parameters: {}, identifiers: {} },
      customFields: customFields
    };
    return { payload: payload, errors: errors };
  }

  /**
   * Decides which fields to show. Unknown/anonymous browsers get the full form.
   * Returns {hide: string[] (field keys), confirmOnly: boolean}.
   */
  function planFields(profileResponse, presentKeys) {
    var res = profileResponse;
    if (!res || res.recognized !== true || !Array.isArray(res.missingFields)) {
      return { hide: [], confirmOnly: false, recognized: false };
    }
    var missing = res.missingFields.filter(function (f) { return typeof f === 'string'; });
    var hide = presentKeys.filter(function (k) { return missing.indexOf(k) === -1; });
    return { hide: hide, confirmOnly: missing.length === 0, recognized: true };
  }

  /** Control key used for missingFields matching: canonical key or "custom.<key>". */
  function fieldKey(resolved) {
    if (!resolved) return null;
    if (resolved.kind === 'profile') return resolved.key;
    if (resolved.kind === 'custom') return CUSTOM_PREFIX + resolved.key;
    return null;
  }

  function randomKey(cryptoObj) {
    var c = cryptoObj || (root && root.crypto);
    if (c && typeof c.randomUUID === 'function') return c.randomUUID();
    if (c && typeof c.getRandomValues === 'function') {
      var b = new Uint8Array(16);
      c.getRandomValues(b);
      b[6] = (b[6] & 0x0f) | 0x40;
      b[8] = (b[8] & 0x3f) | 0x80;
      var hex = '';
      for (var i = 0; i < b.length; i++) hex += (b[i] < 16 ? '0' : '') + b[i].toString(16);
      return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' + hex.slice(16, 20) + '-' + hex.slice(20);
    }
    throw new Error('webinar-form: secure random source unavailable');
  }

  /**
   * In-memory idempotency keys: the same intent (identical payload) reuses its
   * key across retries; a changed intent gets a new key; completion resets.
   * Nothing is persisted, so no form data reaches browser storage.
   */
  function IdempotencyManager(generate) {
    this._generate = generate || randomKey;
    this._key = null;
    this._intent = null;
  }
  IdempotencyManager.prototype.keyFor = function (intent) {
    if (this._key === null || this._intent !== intent) {
      this._key = this._generate();
      this._intent = intent;
    }
    return this._key;
  };
  IdempotencyManager.prototype.reset = function () {
    this._key = null;
    this._intent = null;
  };

  function intentOf(payload) {
    var copy = {};
    for (var k in payload) if (hasOwn(payload, k) && k !== 'idempotencyKey') copy[k] = payload[k];
    return JSON.stringify(copy);
  }

  /** Accepts same-origin relative paths or https URLs only. */
  function safeUrl(raw, base) {
    if (typeof raw !== 'string' || !raw) return null;
    try {
      var u = new URL(raw, base);
      var baseOrigin = new URL(base).origin;
      if (u.protocol === 'https:' || u.origin === baseOrigin) {
        if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
        return u.href;
      }
    } catch (e) {
      return null;
    }
    return null;
  }

  /** The link only when it is https on exactly the expected calendar service path. */
  function calendarWebUrl(raw, expected) {
    if (typeof raw !== 'string' || !raw || raw.length > MAX_WEB_LINK) return null;
    try {
      var u = new URL(raw);
      if (u.protocol !== 'https:' || u.username || u.password || u.port || u.hash) return null;
      return u.origin + u.pathname === expected ? u.href : null;
    } catch (e) {
      return null;
    }
  }

  /** [{name, message, href}] for the valid web calendar links in a response calendar block. */
  function calendarWebLinks(calendar) {
    var out = [];
    if (!calendar || typeof calendar !== 'object') return out;
    for (var i = 0; i < CALENDAR_WEB_LINKS.length; i++) {
      var def = CALENDAR_WEB_LINKS[i];
      var href = calendarWebUrl(calendar[def.key], def.base);
      if (href) out.push({ name: def.name, message: def.message, href: href });
    }
    return out;
  }

  /** Hosted checkout link: https on exactly the Stripe Checkout host, else null. */
  function checkoutUrl(raw) {
    if (typeof raw !== 'string' || !raw || raw.length > 2000) return null;
    try {
      var u = new URL(raw);
      if (u.protocol !== 'https:' || u.username || u.password || u.port) return null;
      return CHECKOUT_HOSTS.indexOf(u.hostname) >= 0 ? u.href : null;
    } catch (e) {
      return null;
    }
  }

  /** Email-verification link from the backend: https, no credentials/port, a /w/ room path. */
  function verifyUrl(raw) {
    if (typeof raw !== 'string' || !raw || raw.length > 2000) return null;
    try {
      var u = new URL(raw);
      if (u.protocol !== 'https:' || u.username || u.password || u.port) return null;
      return /^\/w\/[A-Za-z0-9_-]+\/$/.test(u.pathname) && !u.search && !u.hash ? u.href : null;
    } catch (e) {
      return null;
    }
  }

  /** True for error bodies after which the idempotency key must rotate (order is closed). */
  function closesOrder(body) {
    var code = body && body.error && typeof body.error === 'object' ? body.error.code : '';
    return code === 'checkout_failed' || code === 'order_closed';
  }

  /** ``body`` (optional) lets known server error codes pick a specific message. */
  function messageForStatus(status, messages, body) {
    var code = body && body.error && typeof body.error.code === 'string' ? body.error.code : '';
    if (code && hasOwn(ERROR_CODE_MESSAGES, code)) {
      var name = ERROR_CODE_MESSAGES[code];
      return messages[name] || DEFAULT_MESSAGES[name];
    }
    if (status === 0) return messages.network;
    if (status === 400 || status === 422) return messages.invalid;
    if (status === 409) return messages.conflict;
    if (status === 429) return messages.rateLimited;
    return messages.server;
  }

  /**
   * Extracts field keys from error bodies; never echoes server text. Reads the
   * backend/proxy shape {error:{code,fields?}, fieldErrors:{key:...}} plus the
   * legacy {errors:{...}|[{field}]}. Keys such as "consents.sms.disclosureVersion"
   * map to the control name "consents.sms".
   */
  function errorFieldKeys(body) {
    var keys = [];
    if (!body || typeof body !== 'object') return keys;
    var sources = [body.fieldErrors, body.errors, body.error && typeof body.error === 'object' ? body.error.fields : null];
    sources.forEach(function (errs) {
      if (Array.isArray(errs)) {
        errs.forEach(function (e) {
          if (e && typeof e.field === 'string') keys.push(e.field);
        });
      } else if (errs && typeof errs === 'object') {
        for (var k in errs) if (hasOwn(errs, k)) keys.push(k);
      }
    });
    var out = [];
    keys.forEach(function (k) {
      k = k.replace(/^profile\./, '').replace(/^customFields\./, CUSTOM_PREFIX).replace(/^consents\.sms(\..*)?$/, 'consents.sms');
      if (out.indexOf(k) === -1) out.push(k);
    });
    return out;
  }

  /** First-party proxy base: an absolute path on this origin, no scheme-relative or backslash tricks. */
  function safeProxyBase(raw) {
    if (typeof raw !== 'string' || !/^\/[A-Za-z0-9._~\-\/]*$/.test(raw) || raw.charAt(1) === '/') return '/webinar-api';
    if (/(^|\/)\.\.?(\/|$)/.test(raw)) return '/webinar-api';
    return raw;
  }

  // ---------------------------------------------------------------------------
  // Consent state
  // ---------------------------------------------------------------------------

  var consentState = { analytics: false, advertising: false };
  var consentProvider = null;
  var controllers = [];

  function currentConsent() {
    if (typeof consentProvider === 'function') {
      try {
        return normalizeConsent(consentProvider());
      } catch (e) {
        return { analytics: false, advertising: false };
      }
    }
    return normalizeConsent(consentState);
  }

  function storage() {
    try {
      return root.sessionStorage || null;
    } catch (e) {
      return null;
    }
  }

  function loadStoredParameters() {
    var s = storage();
    if (!s) return {};
    try {
      var v = JSON.parse(s.getItem(STORAGE_KEY) || '{}');
      return v && typeof v === 'object' && !Array.isArray(v) ? v : {};
    } catch (e) {
      return {};
    }
  }

  /** After consent, keeps the first allowlisted landing parameters for this tab session. */
  function captureLanding() {
    var consent = currentConsent();
    var s = storage();
    if (!s) return;
    if (!consent.analytics && !consent.advertising) {
      try { s.removeItem(STORAGE_KEY); } catch (e) { /* ignore */ }
      return;
    }
    var stored = collectParameters(toSearch(loadStoredParameters()), consent);
    var fresh = collectParameters(root.location ? root.location.search : '', consent);
    for (var k in fresh) if (hasOwn(fresh, k) && !hasOwn(stored, k)) stored[k] = fresh[k];
    try { s.setItem(STORAGE_KEY, JSON.stringify(stored)); } catch (e) { /* ignore */ }
  }

  function setConsent(next) {
    consentState = normalizeConsent(next);
    captureLanding();
  }

  // ---------------------------------------------------------------------------
  // DOM controller
  // ---------------------------------------------------------------------------

  function FormController(form, options) {
    this.form = form;
    this.doc = form.ownerDocument;
    this.options = options;
    this.messages = options.messages;
    this.proxyBase = safeProxyBase(options.proxyBase).replace(/\/+$/, '');
    this.occurrenceId = form.getAttribute('data-occurrence-id') || '';
    this.formVersion = form.getAttribute('data-form-version') || '';
    this.fieldMap = parseFieldMap(form.getAttribute('data-field-map'));
    this.idempotency = new IdempotencyManager();
    this.busy = false;
    this.submitButton = form.querySelector('[type="submit"]');
    this.originalLabel = this.submitButton ? labelOf(this.submitButton) : '';
    this.confirmLabel = confirmLabel(form);
    this._ensureRegions();
  }

  /** Optional data-confirm-label (plain text, applied via textContent/value), else the default. */
  function confirmLabel(form) {
    var raw = attr(form, 'data-confirm-label');
    var text = typeof raw === 'string' ? raw.replace(/\s+/g, ' ').trim().slice(0, 60) : '';
    return text || CONFIRM_LABEL;
  }

  function labelOf(btn) {
    return btn.tagName === 'INPUT' ? btn.value : btn.textContent;
  }

  function setLabel(btn, text) {
    if (btn.tagName === 'INPUT') btn.value = text;
    else btn.textContent = text;
  }

  FormController.prototype._ensureRegions = function () {
    var form = this.form;
    var doc = this.doc;
    form.setAttribute('novalidate', '');
    this.statusEl = form.querySelector('[data-webinar-status]');
    if (!this.statusEl) {
      this.statusEl = doc.createElement('div');
      this.statusEl.setAttribute('data-webinar-status', '');
      form.appendChild(this.statusEl);
    }
    this.statusEl.setAttribute('role', 'status');
    this.statusEl.setAttribute('aria-live', 'polite');
    this.errorEl = form.querySelector('[data-webinar-error]');
    if (!this.errorEl) {
      this.errorEl = doc.createElement('div');
      this.errorEl.setAttribute('data-webinar-error', '');
      form.insertBefore(this.errorEl, form.firstChild);
    }
    this.errorEl.setAttribute('role', 'alert');
    this.errorEl.setAttribute('tabindex', '-1');
    this.resetButton = form.querySelector('[data-webinar-reset]');
    if (!this.resetButton) {
      this.resetButton = doc.createElement('button');
      this.resetButton.type = 'button';
      this.resetButton.setAttribute('data-webinar-reset', '');
      this.resetButton.textContent = this.messages.notYou;
      form.appendChild(this.resetButton);
    }
    this.resetButton.hidden = true;
  };

  FormController.prototype.controls = function () {
    return Array.prototype.slice.call(this.form.elements || []);
  };

  FormController.prototype._fieldControls = function () {
    var self = this;
    return this.controls().filter(function (el) {
      return fieldKey(resolveFieldName(el.name, self.fieldMap)) !== null;
    });
  };

  FormController.prototype._wrappers = function (el) {
    var wrapper = el.closest ? el.closest('[data-webinar-field]') : null;
    if (wrapper && this.form.contains(wrapper)) return [wrapper];
    var list = [el];
    if (el.labels) for (var i = 0; i < el.labels.length; i++) list.push(el.labels[i]);
    return list;
  };

  FormController.prototype._setVisible = function (el, visible) {
    var wrappers = this._wrappers(el);
    for (var i = 0; i < wrappers.length; i++) wrappers[i].hidden = !visible;
    if (visible) {
      el.disabled = false;
      if (el.getAttribute('data-webinar-required') === 'true') el.required = true;
      el.removeAttribute('data-webinar-required');
    } else {
      if (el.required) el.setAttribute('data-webinar-required', 'true');
      el.required = false;
      el.disabled = true;
    }
  };

  FormController.prototype.showAll = function () {
    var self = this;
    this._fieldControls().forEach(function (el) { self._setVisible(el, true); });
    if (this.submitButton) setLabel(this.submitButton, this.originalLabel);
    this.resetButton.hidden = true;
  };

  FormController.prototype.applyProfile = function (res) {
    var self = this;
    var controls = this._fieldControls();
    var keys = controls.map(function (el) { return fieldKey(resolveFieldName(el.name, self.fieldMap)); });
    var plan = planFields(res, keys);
    this.showAll();
    if (!plan.recognized) return plan;
    controls.forEach(function (el, i) {
      if (plan.hide.indexOf(keys[i]) !== -1) self._setVisible(el, false);
    });
    if (plan.confirmOnly && this.submitButton) setLabel(this.submitButton, this.confirmLabel);
    this.resetButton.hidden = false;
    this._status(plan.confirmOnly ? this.messages.welcomeBackComplete : this.messages.welcomeBack);
    return plan;
  };

  FormController.prototype._status = function (text) {
    this.statusEl.textContent = text || '';
  };

  FormController.prototype._error = function (text) {
    this.errorEl.textContent = text || '';
    if (text && typeof this.errorEl.focus === 'function') this.errorEl.focus();
  };

  FormController.prototype._clearFieldErrors = function () {
    var nodes = this.form.querySelectorAll('[data-webinar-field-error]');
    for (var i = 0; i < nodes.length; i++) nodes[i].parentNode.removeChild(nodes[i]);
    this.controls().forEach(function (el) {
      if (el.getAttribute && el.getAttribute('aria-invalid') === 'true') {
        el.removeAttribute('aria-invalid');
        var describedBy = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(function (id) {
          return id && id.indexOf('webinar-err-') !== 0;
        });
        if (describedBy.length) el.setAttribute('aria-describedby', describedBy.join(' '));
        else el.removeAttribute('aria-describedby');
      }
    });
  };

  FormController.prototype._markField = function (el, text) {
    var id = 'webinar-err-' + Math.random().toString(36).slice(2, 10);
    var msg = this.doc.createElement('span');
    msg.id = id;
    msg.setAttribute('data-webinar-field-error', '');
    msg.textContent = text;
    el.setAttribute('aria-invalid', 'true');
    var existing = el.getAttribute('aria-describedby');
    el.setAttribute('aria-describedby', existing ? existing + ' ' + id : id);
    if (el.parentNode) el.parentNode.insertBefore(msg, el.nextSibling);
  };

  FormController.prototype._validateLocal = function () {
    var invalid = [];
    this.controls().forEach(function (el) {
      if (el.disabled || typeof el.checkValidity !== 'function') return;
      if (!el.checkValidity()) invalid.push(el);
    });
    return invalid;
  };

  FormController.prototype._fetch = function (path, init) {
    var fetchFn = root.fetch;
    var controller = typeof root.AbortController === 'function' ? new root.AbortController() : null;
    var timer = null;
    if (controller) {
      init.signal = controller.signal;
      timer = root.setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS);
    }
    init.credentials = 'same-origin';
    init.headers = init.headers || {};
    init.headers.Accept = 'application/json';
    if (typeof fetchFn !== 'function') return Promise.resolve({ status: 0, ok: false, body: null });
    // Called as a method: an unbound window.fetch throws "Illegal invocation".
    return root.fetch(this.proxyBase + path, init).then(function (res) {
      return res.text().then(function (text) {
        var body = null;
        try { body = text ? JSON.parse(text) : null; } catch (e) { body = null; }
        return { status: res.status, ok: res.ok, body: body };
      });
    }).then(null, function () {
      // Network error, abort/timeout or unreadable body: never treated as success.
      return { status: 0, ok: false, body: null };
    }).then(function (r) {
      if (timer) root.clearTimeout(timer);
      return r;
    });
  };

  FormController.prototype.loadProfile = function () {
    var self = this;
    if (!this.occurrenceId || !this.formVersion) return Promise.resolve(this.applyProfile(null));
    var qs = '?occurrenceId=' + encodeURIComponent(this.occurrenceId) + '&formVersion=' + encodeURIComponent(this.formVersion);
    return this._fetch('/profile' + qs, { method: 'GET' }).then(function (r) {
      // Any failure falls back to the full form; nothing is hidden.
      return self.applyProfile(r.ok ? r.body : null);
    });
  };

  FormController.prototype._csrf = function () {
    return readCookie(this.doc.cookie, CSRF_COOKIE);
  };

  FormController.prototype._post = function (path, body) {
    var self = this;
    var ready = this._csrf() ? Promise.resolve() : this._fetch('/profile?occurrenceId=' + encodeURIComponent(this.occurrenceId) +
      '&formVersion=' + encodeURIComponent(this.formVersion), { method: 'GET' });
    return ready.then(function () {
      var headers = { 'Content-Type': 'application/json' };
      var token = self._csrf();
      if (token) headers[CSRF_HEADER] = token;
      return self._fetch(path, { method: 'POST', headers: headers, body: JSON.stringify(body) });
    });
  };

  FormController.prototype._setBusy = function (busy) {
    this.busy = busy;
    this.form.setAttribute('aria-busy', busy ? 'true' : 'false');
    if (this.submitButton) this.submitButton.disabled = busy;
    this.resetButton.disabled = busy;
  };

  FormController.prototype.reset = function () {
    var self = this;
    if (this.busy) return Promise.resolve(false);
    this._setBusy(true);
    this._error('');
    return this._post('/reset', {}).then(function (r) {
      self._setBusy(false);
      if (!r.ok) {
        self._error(self.messages.resetFailed);
        return false;
      }
      self.idempotency.reset();
      if (typeof self.form.reset === 'function') self.form.reset();
      self._clearFieldErrors();
      self.showAll();
      self._status(self.messages.resetDone);
      var first = self._fieldControls()[0];
      if (first && typeof first.focus === 'function') first.focus();
      return true;
    });
  };

  FormController.prototype.submit = function () {
    var self = this;
    if (this.busy) return Promise.resolve(false);
    this._error('');
    this._status('');
    this._clearFieldErrors();
    var invalid = this._validateLocal();
    if (invalid.length) {
      invalid.forEach(function (el) {
        self._markField(el, el.validity && el.validity.valueMissing ? self.messages.fieldRequired : self.messages.fieldInvalid);
      });
      this._error(this.messages.invalid);
      return Promise.resolve(false);
    }
    var consent = currentConsent();
    var loc = root.location || {};
    var attribution = buildAttribution({
      search: loc.search,
      href: loc.href,
      referrer: this.doc.referrer,
      cookie: this.doc.cookie,
      storedParameters: loadStoredParameters()
    }, consent);
    var built = buildPayload(this.controls(), {
      fieldMap: this.fieldMap,
      occurrenceId: this.occurrenceId,
      formVersion: this.formVersion,
      consent: consent,
      attribution: attribution,
      idempotencyKey: ''
    });
    if (built.errors.length) {
      built.errors.forEach(function (e) {
        var el = self.form.elements.namedItem(e.name);
        if (el && el.setAttribute) self._markField(el, e.code === 'conflict' ? self.messages.fieldConflict : self.messages.fieldInvalid);
      });
      this._error(this.messages.invalid);
      return Promise.resolve(false);
    }
    var payload = built.payload;
    payload.idempotencyKey = this.idempotency.keyFor(intentOf(payload));
    this._setBusy(true);
    this._status(this.messages.submitting);
    return this._post('/registrations', payload).then(function (r) {
      self._setBusy(false);
      if (!r.ok) {
        // The order behind this key is closed for good: a retry needs a fresh key, or it
        // would only replay the closed order (409 order_closed).
        if (closesOrder(r.body)) self.idempotency.reset();
        self._status('');
        self._showServerErrors(r);
        return false;
      }
      var body = r.body || {};
      if (body.status === 'verification_required') {
        // Paid session, browser not yet email-verified: nothing was held or charged. The
        // response is the same for every address (no account oracle); a new key is used
        // after verifying, from the verified browser.
        self.idempotency.reset();
        return self._onVerificationRequired(body);
      }
      if (r.status === 202 || body.status === 'payment_required') {
        // Paid session: a seat is held, nothing is registered yet. The idempotency key is
        // kept, so submitting again replays the same order (never a second hold).
        return self._onPaymentRequired(body);
      }
      self.idempotency.reset();
      self._onSuccess(body);
      return true;
    });
  };

  /** 202 payment_required: go to hosted checkout; never show a confirmation here. */
  FormController.prototype._onPaymentRequired = function (body) {
    var order = body.order || {};
    var url = checkoutUrl(order.checkoutUrl);
    this.form.setAttribute('data-webinar-state', 'payment-required');
    if (url && root.location && typeof root.location.assign === 'function') {
      this._status(this.messages.paymentRedirecting || DEFAULT_MESSAGES.paymentRedirecting);
      root.location.assign(url);
      return 'payment_required';
    }
    this._status(this.messages.paymentPreparing || DEFAULT_MESSAGES.paymentPreparing);
    return 'payment_required';
  };

  /** 202 verification_required: send the buyer to the hosted email check (waiting room). */
  FormController.prototype._onVerificationRequired = function (body) {
    var v = body.verification || {};
    var url = v.available ? verifyUrl(v.verifyUrl) : null;
    this.form.setAttribute('data-webinar-state', 'verification-required');
    if (url && root.location && typeof root.location.assign === 'function') {
      this._status(this.messages.verificationRedirecting || DEFAULT_MESSAGES.verificationRedirecting);
      root.location.assign(url);
      return 'verification_required';
    }
    this._status(this.messages.verificationUnavailable || DEFAULT_MESSAGES.verificationUnavailable);
    return 'verification_required';
  };

  FormController.prototype._showServerErrors = function (r) {
    var self = this;
    var keys = errorFieldKeys(r.body);
    keys.forEach(function (key) {
      self.controls().forEach(function (el) {
        if (el.disabled || (el.getAttribute && el.getAttribute('aria-invalid') === 'true')) return;
        var resolved = resolveFieldName(el.name, self.fieldMap);
        if (fieldKey(resolved) === key || (resolved && resolved.kind === 'sms' && key === 'consents.sms')) {
          self._markField(el, self.messages.fieldInvalid);
        }
      });
    });
    this._error(messageForStatus(r.status, this.messages, r.body));
  };

  FormController.prototype._onSuccess = function (body) {
    var base = root.location ? root.location.href : 'https://invalid.invalid/';
    var thankYou = body.thankYou || {};
    var redirect = safeUrl(thankYou.redirectUrl, base);
    this._pushEvent();
    if (redirect && root.location && typeof root.location.assign === 'function') {
      root.location.assign(redirect);
      return;
    }
    this.form.setAttribute('data-webinar-state', 'complete');
    this._fieldControls().forEach(function (el) { el.disabled = true; });
    if (this.submitButton) this.submitButton.disabled = true;
    this.resetButton.hidden = true;
    var text = typeof thankYou.message === 'string' && thankYou.message ? thankYou.message : this.messages.success;
    this._status(text);
    this._renderCalendar(body.calendar || {}, base);
  };

  /** ICS link plus validated Google/Outlook/Microsoft 365 links, as text-only anchors. */
  FormController.prototype._renderCalendar = function (calendar, base) {
    var self = this;
    var ics = safeUrl(calendar.icsUrl, base);
    if (ics) {
      var a = this.doc.createElement('a');
      a.href = ics;
      a.textContent = this.messages.addToCalendar;
      a.setAttribute('data-webinar-calendar', '');
      this.statusEl.appendChild(this.doc.createTextNode(' '));
      this.statusEl.appendChild(a);
    }
    calendarWebLinks(calendar).forEach(function (link) {
      var w = self.doc.createElement('a');
      var label = self.messages[link.message] || DEFAULT_MESSAGES[link.message];
      w.href = link.href;
      w.textContent = label;
      // New tab without opener or referrer; the accessible name starts with the visible text.
      w.setAttribute('target', '_blank');
      w.setAttribute('rel', 'noopener noreferrer');
      w.setAttribute('aria-label', label + ' ' + self.messages.opensInNewTab);
      w.setAttribute('data-webinar-calendar-link', link.name);
      self.statusEl.appendChild(self.doc.createTextNode(' '));
      self.statusEl.appendChild(w);
    });
  };

  /** dataLayer event without PII, only with analytics consent. */
  FormController.prototype._pushEvent = function () {
    if (!currentConsent().analytics) return;
    if (!root.dataLayer || typeof root.dataLayer.push !== 'function') return;
    root.dataLayer.push({ event: 'webinar_registration', webinarOccurrenceId: this.occurrenceId, webinarFormVersion: this.formVersion });
  };

  FormController.prototype.bind = function () {
    var self = this;
    this.form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      self.submit();
    });
    this.resetButton.addEventListener('click', function () { self.reset(); });
    return this.loadProfile();
  };

  // ---------------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------------

  function readOptions() {
    var cfg = (root && root.WebinarFormConfig) || {};
    var script = root && root.document ? root.document.currentScript : null;
    var proxyBase = cfg.proxyBase || (script && script.getAttribute('data-proxy-base')) || '/webinar-api';
    // First-party only: a plain absolute path on this origin ("/\evil.com" is cross-origin in URL parsing).
    proxyBase = safeProxyBase(proxyBase);
    var messages = {};
    var k;
    for (k in DEFAULT_MESSAGES) if (hasOwn(DEFAULT_MESSAGES, k)) messages[k] = DEFAULT_MESSAGES[k];
    if (cfg.messages) for (k in cfg.messages) if (hasOwn(messages, k) && typeof cfg.messages[k] === 'string') messages[k] = cfg.messages[k];
    return { proxyBase: proxyBase, messages: messages, autoInit: cfg.autoInit !== false, consent: cfg.consent, consentProvider: cfg.consentProvider };
  }

  var bootOptions = null;

  function init(scope) {
    if (!root || !root.document) return [];
    bootOptions = bootOptions || readOptions();
    var forms = (scope || root.document).querySelectorAll('form[data-webinar-form]');
    var created = [];
    for (var i = 0; i < forms.length; i++) {
      if (forms[i].__webinarForm) continue;
      var c = new FormController(forms[i], bootOptions);
      forms[i].__webinarForm = c;
      controllers.push(c);
      created.push(c);
      c.bind();
    }
    return created;
  }

  function boot() {
    bootOptions = readOptions();
    if (bootOptions.consent) consentState = normalizeConsent(bootOptions.consent);
    if (typeof bootOptions.consentProvider === 'function') consentProvider = bootOptions.consentProvider;
    captureLanding();
    if (!bootOptions.autoInit) return;
    if (root.document.readyState === 'loading') {
      root.document.addEventListener('DOMContentLoaded', function () { init(); });
    } else {
      init();
    }
  }

  return {
    version: '1.0.0',
    init: init,
    setConsent: setConsent,
    setConsentProvider: function (fn) {
      consentProvider = typeof fn === 'function' ? fn : null;
      captureLanding();
    },
    getConsent: currentConsent,
    _boot: boot,
    _internal: {
      CANONICAL_FIELDS: CANONICAL_FIELDS,
      ALIASES: ALIASES,
      ANALYTICS_PARAMS: ANALYTICS_PARAMS,
      ADVERTISING_PARAMS: ADVERTISING_PARAMS,
      IDENTIFIER_COOKIES: IDENTIFIER_COOKIES,
      CONFIRM_LABEL: CONFIRM_LABEL,
      confirmLabel: confirmLabel,
      CSRF_COOKIE: CSRF_COOKIE,
      CSRF_HEADER: CSRF_HEADER,
      stripUrl: stripUrl,
      isPopulated: isPopulated,
      collectParameters: collectParameters,
      collectIdentifiers: collectIdentifiers,
      readCookie: readCookie,
      parseGaCookie: parseGaCookie,
      buildAttribution: buildAttribution,
      buildPayload: buildPayload,
      resolveFieldName: resolveFieldName,
      parseFieldMap: parseFieldMap,
      planFields: planFields,
      fieldKey: fieldKey,
      IdempotencyManager: IdempotencyManager,
      verifyUrl: verifyUrl,
      closesOrder: closesOrder,
      intentOf: intentOf,
      randomKey: randomKey,
      safeUrl: safeUrl,
      CALENDAR_WEB_LINKS: CALENDAR_WEB_LINKS,
      calendarWebUrl: calendarWebUrl,
      calendarWebLinks: calendarWebLinks,
      DEFAULT_MESSAGES: DEFAULT_MESSAGES,
      messageForStatus: messageForStatus,
      checkoutUrl: checkoutUrl,
      errorFieldKeys: errorFieldKeys,
      safeProxyBase: safeProxyBase,
      readOptions: readOptions,
      FormController: FormController
    }
  };
});
