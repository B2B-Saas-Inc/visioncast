#!/usr/bin/env node
/*
 * First-party webinar proxy. Node.js standard library only.
 * Licensed under the Apache License, Version 2.0.
 *
 * Mount on the marketing site's origin under /webinar-api/. It validates the
 * exact Origin, a double-submit CSRF token, content type and body size, then
 * forwards a fixed allowlist of routes to the webinar backend with a
 * server-held scoped proxy key. The key never reaches the browser.
 */
'use strict';

const http = require('node:http');
const https = require('node:https');
const crypto = require('node:crypto');

const ROUTES = Object.freeze({
  '/webinar-api/profile': Object.freeze({ method: 'GET', upstream: '/api/v1/profile' }),
  '/webinar-api/registrations': Object.freeze({ method: 'POST', upstream: '/api/v1/registrations' }),
  '/webinar-api/reset': Object.freeze({ method: 'POST', upstream: '/api/v1/reset' }),
  // Registration status for this browser's capability; occurrence UUID only, CSRF token required.
  '/webinar-api/my-registration': Object.freeze({ method: 'GET', upstream: '/api/v1/my-registration', kind: 'myRegistration' }),
  // Public ICS download; the occurrence UUID is the only input and is pattern-checked.
  '/webinar-api/calendar.ics': Object.freeze({ method: 'GET', upstream: '/api/v1/occurrences/{id}/calendar.ics', kind: 'calendar' }),
});

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const BACKEND_ICS_RE = /^\/api\/v1\/occurrences\/([0-9a-fA-F-]{36})\/calendar\.ics$/;
const MAX_ICS_BODY = 64 * 1024;

const CSRF_HEADER = 'x-webinar-csrf';
const TOKEN_RE = /^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$/; // RFC 7230 token, used for cookie names
const COOKIE_VALUE_RE = /^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]{1,4096}$/; // RFC 6265 cookie-octet
const QUERY_VALUE_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const CSRF_VALUE_RE = /^[A-Za-z0-9_-]{43}$/;
const MAX_UPSTREAM_BODY = 256 * 1024;

class HttpError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

function loadConfig(env) {
  env = env || process.env;
  const errors = [];
  let backend = null;
  const allowInsecure = env.PROXY_ALLOW_INSECURE_BACKEND === '1';
  try {
    backend = new URL(env.PROXY_BACKEND_URL || '');
    if (backend.protocol !== 'https:' && !(allowInsecure && backend.protocol === 'http:')) {
      errors.push('PROXY_BACKEND_URL must use https (set PROXY_ALLOW_INSECURE_BACKEND=1 only for local development)');
    }
    if (backend.username || backend.password || backend.search || backend.hash || (backend.pathname && backend.pathname !== '/')) {
      errors.push('PROXY_BACKEND_URL must be an origin only (scheme://host[:port])');
    }
  } catch (e) {
    errors.push('PROXY_BACKEND_URL is required and must be an absolute URL');
  }
  const proxyKey = env.PROXY_API_KEY || '';
  if (proxyKey.length < 32 || /[\s\r\n]/.test(proxyKey)) {
    errors.push('PROXY_API_KEY is required (>= 32 characters, no whitespace)');
  }
  const allowedOrigins = String(env.PROXY_ALLOWED_ORIGINS || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  if (!allowedOrigins.length) errors.push('PROXY_ALLOWED_ORIGINS is required (comma-separated exact origins)');
  for (const o of allowedOrigins) {
    let ok = false;
    try {
      const u = new URL(o);
      ok = u.origin === o && (u.protocol === 'https:' || u.hostname === 'localhost' || u.hostname === '127.0.0.1');
    } catch (e) {
      ok = false;
    }
    if (!ok) errors.push(`PROXY_ALLOWED_ORIGINS entry is not an exact https origin: ${JSON.stringify(o)}`);
  }
  const bodyLimit = intEnv(env.PROXY_BODY_LIMIT, 32 * 1024);
  if (!(bodyLimit >= 1024 && bodyLimit <= 1024 * 1024)) errors.push('PROXY_BODY_LIMIT must be between 1024 and 1048576 bytes');
  const timeoutMs = intEnv(env.PROXY_TIMEOUT_MS, 10000);
  if (!(timeoutMs >= 100 && timeoutMs <= 60000)) errors.push('PROXY_TIMEOUT_MS must be between 100 and 60000');
  const sessionCookieName = env.PROXY_SESSION_COOKIE || 'webinar_session';
  const csrfCookieName = env.PROXY_CSRF_COOKIE || '__Host-webinar_csrf';
  if (!TOKEN_RE.test(sessionCookieName)) errors.push('PROXY_SESSION_COOKIE is not a valid cookie name');
  if (!TOKEN_RE.test(csrfCookieName)) errors.push('PROXY_CSRF_COOKIE is not a valid cookie name');
  const port = intEnv(env.PORT, 8787);
  if (!(port >= 1 && port <= 65535)) errors.push('PORT must be between 1 and 65535');
  if (errors.length) {
    const err = new Error(`Invalid proxy configuration:\n- ${errors.join('\n- ')}`);
    err.details = errors;
    throw err;
  }
  return Object.freeze({
    backendOrigin: backend.origin,
    backendProtocol: backend.protocol,
    proxyKey,
    allowedOrigins: Object.freeze(allowedOrigins),
    bodyLimit,
    timeoutMs,
    sessionCookieName,
    csrfCookieName,
    trustForwardedFor: env.PROXY_TRUST_FORWARDED_FOR === '1',
    host: env.HOST || '127.0.0.1',
    port,
  });
}

function intEnv(value, fallback) {
  if (value === undefined || value === '') return fallback;
  return /^\d+$/.test(value) ? Number(value) : NaN;
}

function parseCookies(header) {
  const out = new Map();
  if (typeof header !== 'string') return out;
  for (const part of header.split(';')) {
    const eq = part.indexOf('=');
    if (eq === -1) continue;
    const name = part.slice(0, eq).trim();
    const value = part.slice(eq + 1).trim();
    if (TOKEN_RE.test(name) && !out.has(name)) out.set(name, value);
  }
  return out;
}

function safeEqual(a, b) {
  const ab = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  if (ab.length !== bb.length) return false;
  return crypto.timingSafeEqual(ab, bb);
}

function checkOrigin(req, config, method) {
  const origin = req.headers.origin;
  const site = req.headers['sec-fetch-site'];
  if (site !== undefined && site !== 'same-origin' && !(method === 'GET' && site === 'none')) return false;
  if (origin === undefined) return method === 'GET';
  return config.allowedOrigins.includes(origin);
}

function checkCsrf(req, cookies, config) {
  const header = req.headers[CSRF_HEADER];
  const cookie = cookies.get(config.csrfCookieName);
  if (typeof header !== 'string' || !cookie) return false;
  if (!CSRF_VALUE_RE.test(header) || !CSRF_VALUE_RE.test(cookie)) return false;
  return safeEqual(header, cookie);
}

function csrfCookie(config, token) {
  // Readable by first-party JS (double-submit); Secure + __Host- prefix pins it to this origin.
  return `${config.csrfCookieName}=${token}; Path=/; Secure; SameSite=Strict; Max-Age=86400`;
}

/** Re-issues the backend session cookie with fixed safe attributes; drops anything else. */
function rewriteSetCookie(raw, config) {
  const parts = String(raw).split(';').map((s) => s.trim()).filter(Boolean);
  if (!parts.length) return null;
  const first = parts.shift();
  const eq = first.indexOf('=');
  if (eq === -1) return null;
  const name = first.slice(0, eq).trim();
  let value = first.slice(eq + 1).trim();
  if (name !== config.sessionCookieName) return null;
  // RFC 6265 allows a DQUOTE-wrapped cookie-value; Django's delete_cookie emits `name=""`.
  if (value.length >= 2 && value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
  if (value !== '' && !COOKIE_VALUE_RE.test(value)) return null;
  const kept = [];
  for (const p of parts) {
    const [k, ...rest] = p.split('=');
    const key = k.trim().toLowerCase();
    const v = rest.join('=').trim();
    if (key === 'max-age' && /^-?\d{1,10}$/.test(v)) kept.push(`Max-Age=${v}`);
    if (key === 'expires' && /^[A-Za-z0-9 ,:-]{1,64}$/.test(v)) kept.push(`Expires=${v}`);
  }
  return [`${name}=${value}`, 'Path=/', ...kept, 'HttpOnly', 'Secure', 'SameSite=Lax'].join('; ');
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const declared = req.headers['content-length'];
    if (declared !== undefined && !(/^\d+$/.test(declared) && Number(declared) <= limit)) {
      req.resume();
      reject(new HttpError(413, 'payload_too_large'));
      return;
    }
    const chunks = [];
    let size = 0;
    let done = false;
    req.on('data', (chunk) => {
      if (done) return;
      size += chunk.length;
      if (size > limit) {
        done = true;
        reject(new HttpError(413, 'payload_too_large'));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (done) return;
      done = true;
      resolve(Buffer.concat(chunks));
    });
    req.on('error', () => {
      if (done) return;
      done = true;
      reject(new HttpError(400, 'bad_request'));
    });
  });
}

function parseJsonObject(buf, allowEmpty) {
  const text = buf.toString('utf8');
  if (!text.trim()) {
    if (allowEmpty) return {};
    throw new HttpError(400, 'invalid_json');
  }
  let value;
  try {
    value = JSON.parse(text);
  } catch (e) {
    throw new HttpError(400, 'invalid_json');
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new HttpError(400, 'invalid_json');
  return value;
}

function isJsonContentType(value) {
  if (typeof value !== 'string') return false;
  const [type, ...params] = value.split(';').map((s) => s.trim().toLowerCase());
  if (type !== 'application/json') return false;
  return params.every((p) => p === '' || p === 'charset=utf-8');
}

function clientIp(req, config) {
  if (config.trustForwardedFor) {
    const xff = req.headers['x-forwarded-for'];
    if (typeof xff === 'string' && xff.trim()) return xff.split(',')[0].trim();
  }
  return req.socket.remoteAddress || '';
}

/** Defence in depth: only the documented profile fields ever reach the browser. */
function projectProfile(body) {
  return {
    recognized: body.recognized === true,
    missingFields: Array.isArray(body.missingFields)
      ? body.missingFields.filter((f) => typeof f === 'string' && f.length <= 128)
      : [],
    formVersion: typeof body.formVersion === 'string' ? body.formVersion : '',
  };
}

/**
 * Error bodies keep only a machine code and offending field keys; server text never reaches the browser.
 * Backend shape: {"error":{"code","message","fields"?},"fieldErrors"?:{key:message}}.
 * Projected shape: {"error":{"code"},"fieldErrors"?:{key:"invalid"}}.
 */
function projectError(body) {
  const out = {};
  if (!body || typeof body !== 'object' || Array.isArray(body)) return out;
  const codeRe = /^[a-z0-9_]{1,64}$/;
  const err = body.error;
  const code = err && typeof err === 'object' ? err.code : err;
  if (typeof code === 'string' && codeRe.test(code)) out.error = { code };
  const keyRe = /^[A-Za-z0-9_.]{1,128}$/;
  const fields = {};
  const add = (k) => {
    if (typeof k === 'string' && keyRe.test(k) && Object.keys(fields).length < 64) fields[k] = 'invalid';
  };
  for (const src of [body.fieldErrors, err && typeof err === 'object' ? err.fields : null, body.errors]) {
    if (Array.isArray(src)) {
      for (const e of src) if (e && typeof e === 'object') add(e.field);
    } else if (src && typeof src === 'object') {
      for (const k of Object.keys(src)) add(k);
    }
  }
  if (Object.keys(fields).length) out.fieldErrors = fields;
  return out;
}

/** Points the backend's relative ICS link at this proxy's fixed calendar route. */
function rewriteRegistrationBody(body) {
  if (!body || typeof body !== 'object' || !body.calendar || typeof body.calendar !== 'object') return body;
  const ics = body.calendar.icsUrl;
  if (typeof ics !== 'string') return body;
  let path = ics;
  if (/^https?:\/\//i.test(ics)) {
    try {
      path = new URL(ics).pathname;
    } catch (e) {
      path = '';
    }
  }
  const m = BACKEND_ICS_RE.exec(path);
  const calendar = Object.assign({}, body.calendar);
  if (m && UUID_RE.test(m[1])) {
    calendar.icsUrl = `/webinar-api/calendar.ics?${new URLSearchParams({ occurrenceId: m[1].toLowerCase() })}`;
  } else {
    delete calendar.icsUrl; // unknown shape: no link rather than a broken or foreign one
  }
  return Object.assign({}, body, { calendar });
}

/** Exact https origin + path of each web calendar link (webinars/calendar_links.py). */
const CALENDAR_WEB_LINKS = Object.freeze({
  googleUrl: 'https://calendar.google.com/calendar/render',
  outlookUrl: 'https://outlook.live.com/calendar/0/deeplink/compose',
  office365Url: 'https://outlook.office.com/calendar/0/deeplink/compose',
});
const MAX_WEB_LINK = 8192;

function isPlainObject(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function boundedString(value, max) {
  return typeof value === 'string' && value.length <= max ? value : undefined;
}

/** The link when it is https on the expected calendar service path, else null. */
function webCalendarLink(value, expected) {
  if (typeof value !== 'string' || value.length > MAX_WEB_LINK) return null;
  let u;
  try {
    u = new URL(value);
  } catch (e) {
    return null;
  }
  if (u.protocol !== 'https:' || u.username || u.password || u.port || u.hash) return null;
  return u.origin + u.pathname === expected ? u.href : null;
}

function projectThankYou(src) {
  const out = { mode: src.mode === 'redirect' ? 'redirect' : 'message' };
  const message = boundedString(src.message, 4000);
  if (message !== undefined) out.message = message;
  const redirect = boundedString(src.redirectUrl, 2048);
  if (redirect !== undefined && /^https:\/\//i.test(redirect)) out.redirectUrl = redirect;
  return out;
}

function projectCalendar(src) {
  const out = {};
  const limits = { title: 1000, startsAt: 64, endsAt: 64, timezone: 64, icsUrl: 2048, uid: 255 };
  for (const [key, max] of Object.entries(limits)) {
    const value = boundedString(src[key], max);
    if (value !== undefined) out[key] = value;
  }
  for (const [key, expected] of Object.entries(CALENDAR_WEB_LINKS)) out[key] = webCalendarLink(src[key], expected);
  if (Number.isSafeInteger(src.sequence) && src.sequence >= 0) out.sequence = src.sequence;
  return out;
}

/** Defence in depth: only the documented status, thank-you and calendar keys reach the browser. */
function projectMyRegistration(body) {
  const out = {
    occurrenceId: typeof body.occurrenceId === 'string' && UUID_RE.test(body.occurrenceId) ? body.occurrenceId : '',
    recognized: body.recognized === true,
    registered: body.registered === true,
    status: typeof body.status === 'string' && /^[a-z_]{1,32}$/.test(body.status) ? body.status : null,
  };
  if (out.registered && isPlainObject(body.thankYou)) out.thankYou = projectThankYou(body.thankYou);
  if (out.registered && isPlainObject(body.calendar)) out.calendar = projectCalendar(body.calendar);
  return rewriteRegistrationBody(out);
}

/** The single occurrenceId UUID query parameter (lower case), or a 400. */
function singleOccurrenceId(url) {
  const keys = [...url.searchParams.keys()];
  const occurrenceId = url.searchParams.get('occurrenceId') || '';
  if (keys.length !== 1 || keys[0] !== 'occurrenceId' || !UUID_RE.test(occurrenceId)) {
    throw new HttpError(400, 'invalid_query');
  }
  return occurrenceId.toLowerCase();
}

function upstreamRequest(config, { method, path, headers, body }) {
  const transport = config.backendProtocol === 'https:' ? https : http;
  const target = new URL(path, config.backendOrigin);
  return new Promise((resolve, reject) => {
    const req = transport.request(
      target,
      { method, headers, timeout: config.timeoutMs },
      (res) => {
        const chunks = [];
        let size = 0;
        res.on('data', (chunk) => {
          size += chunk.length;
          if (size > MAX_UPSTREAM_BODY) {
            res.destroy();
            reject(new HttpError(502, 'bad_gateway'));
            return;
          }
          chunks.push(chunk);
        });
        res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
        res.on('error', () => reject(new HttpError(502, 'bad_gateway')));
      }
    );
    req.on('timeout', () => req.destroy(new HttpError(504, 'gateway_timeout')));
    req.on('error', (err) => reject(err instanceof HttpError ? err : new HttpError(502, 'bad_gateway')));
    if (body) req.write(body);
    req.end();
  });
}

function sendJson(res, status, payload, extraCookies) {
  const body = JSON.stringify(payload);
  if (extraCookies && extraCookies.length) {
    const existing = res.getHeader('Set-Cookie');
    res.setHeader('Set-Cookie', [].concat(existing || [], extraCookies));
  }
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Content-Length', Buffer.byteLength(body));
  res.end(body);
}

function createHandler(config) {
  return async function handle(req, res) {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Vary', 'Origin, Cookie');
    try {
      await route(req, res, config);
    } catch (err) {
      const status = err instanceof HttpError ? err.status : 500;
      const code = err instanceof HttpError ? err.code : 'internal_error';
      if (!(err instanceof HttpError)) console.error('webinar-proxy: unexpected error', err && err.name);
      if (!res.headersSent) {
        if (status === 413) res.setHeader('Connection', 'close');
        sendJson(res, status, { error: code });
      } else {
        res.destroy();
      }
    }
  };
}

async function route(req, res, config) {
  let url;
  try {
    url = new URL(req.url, 'http://proxy.invalid');
  } catch (e) {
    throw new HttpError(400, 'bad_request');
  }
  // Exact match against a fixed allowlist; raw URL must equal its normalised form.
  const rawPath = String(req.url).split('?')[0];
  const spec = Object.prototype.hasOwnProperty.call(ROUTES, url.pathname) && rawPath === url.pathname ? ROUTES[url.pathname] : null;
  if (!spec) throw new HttpError(404, 'not_found');
  if (req.method !== spec.method) {
    res.setHeader('Allow', spec.method);
    throw new HttpError(405, 'method_not_allowed');
  }
  if (!checkOrigin(req, config, spec.method)) throw new HttpError(403, 'origin_not_allowed');
  if (spec.kind === 'calendar') {
    await proxyCalendar(url, res, config, spec);
    return;
  }

  const cookies = parseCookies(req.headers.cookie);
  const headers = {
    accept: 'application/json',
    authorization: `Bearer ${config.proxyKey}`,
    'x-request-id': crypto.randomUUID(),
    'x-forwarded-for': clientIp(req, config),
    'x-forwarded-proto': 'https',
  };
  const session = cookies.get(config.sessionCookieName);
  if (session && COOKIE_VALUE_RE.test(session)) headers.cookie = `${config.sessionCookieName}=${session}`;

  const setCookies = [];
  let upstreamPath = spec.upstream;
  let body = null;

  if (spec.kind === 'myRegistration') {
    // Reads the capability, so it needs the double-submit token like POST (issued by /profile).
    if (!checkCsrf(req, cookies, config)) throw new HttpError(403, 'csrf_failed');
    upstreamPath = `${spec.upstream}?${new URLSearchParams({ occurrenceId: singleOccurrenceId(url) })}`;
  } else if (spec.method === 'GET') {
    const occurrenceId = url.searchParams.get('occurrenceId');
    const formVersion = url.searchParams.get('formVersion');
    if (!QUERY_VALUE_RE.test(occurrenceId || '') || !QUERY_VALUE_RE.test(formVersion || '')) {
      throw new HttpError(400, 'invalid_query');
    }
    const qs = new URLSearchParams({ occurrenceId, formVersion });
    upstreamPath = `${spec.upstream}?${qs.toString()}`;
    const existing = cookies.get(config.csrfCookieName);
    if (!existing || !CSRF_VALUE_RE.test(existing)) {
      setCookies.push(csrfCookie(config, crypto.randomBytes(32).toString('base64url')));
    }
  } else {
    if (req.headers.origin === undefined) throw new HttpError(403, 'origin_not_allowed');
    if (!checkCsrf(req, cookies, config)) throw new HttpError(403, 'csrf_failed');
    const isReset = spec.upstream === '/api/v1/reset';
    if (!isJsonContentType(req.headers['content-type'])) {
      if (!(isReset && req.headers['content-length'] === '0')) throw new HttpError(415, 'unsupported_media_type');
    }
    const raw = await readBody(req, config.bodyLimit);
    const parsed = parseJsonObject(raw, isReset);
    if (!isReset && parsed.schemaVersion !== '1') throw new HttpError(400, 'unsupported_schema_version');
    body = Buffer.from(JSON.stringify(parsed));
    headers['content-type'] = 'application/json';
    headers['content-length'] = String(body.length);
  }

  const upstream = await upstreamRequest(config, { method: spec.method, path: upstreamPath, headers, body });

  const upstreamCookies = [].concat(upstream.headers['set-cookie'] || []);
  for (const raw of upstreamCookies) {
    const rewritten = rewriteSetCookie(raw, config);
    if (rewritten) setCookies.push(rewritten);
  }

  if (upstream.status >= 500 || upstream.status < 200) throw new HttpError(502, 'bad_gateway');
  if (upstream.status === 204 || upstream.body.length === 0) {
    if (setCookies.length) res.setHeader('Set-Cookie', setCookies);
    res.statusCode = upstream.status === 204 ? 204 : upstream.status;
    res.end();
    return;
  }
  if (!isJsonContentType(String(upstream.headers['content-type'] || '').replace(/;\s*charset=utf-8/i, ''))) {
    throw new HttpError(502, 'bad_gateway');
  }
  let payload;
  try {
    payload = JSON.parse(upstream.body.toString('utf8'));
  } catch (e) {
    throw new HttpError(502, 'bad_gateway');
  }
  if (upstream.status >= 400) {
    payload = projectError(payload);
  } else if (spec.upstream === '/api/v1/profile') {
    if (!payload || typeof payload !== 'object') throw new HttpError(502, 'bad_gateway');
    payload = projectProfile(payload);
  } else if (spec.upstream === '/api/v1/registrations') {
    payload = rewriteRegistrationBody(payload);
  } else if (spec.kind === 'myRegistration') {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new HttpError(502, 'bad_gateway');
    payload = projectMyRegistration(payload);
  }
  sendJson(res, upstream.status, payload, setCookies);
}

/** GET /webinar-api/calendar.ics?occurrenceId=<uuid>: public ICS, no cookies or key forwarded. */
async function proxyCalendar(url, res, config, spec) {
  const path = spec.upstream.replace('{id}', singleOccurrenceId(url));
  const upstream = await upstreamRequest(config, {
    method: 'GET',
    path,
    headers: { accept: 'text/calendar', 'x-request-id': crypto.randomUUID() },
    body: null,
  });
  if (upstream.status === 404) throw new HttpError(404, 'not_found');
  if (upstream.status !== 200) throw new HttpError(502, 'bad_gateway');
  const type = String(upstream.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
  if (type !== 'text/calendar' || upstream.body.length > MAX_ICS_BODY) throw new HttpError(502, 'bad_gateway');
  res.statusCode = 200;
  res.setHeader('Content-Type', 'text/calendar; charset=utf-8');
  res.setHeader('Content-Disposition', 'attachment; filename="webinar.ics"');
  res.setHeader('Content-Length', upstream.body.length);
  res.end(upstream.body);
}

function createProxyServer(config) {
  const server = http.createServer(createHandler(config));
  server.headersTimeout = 15000;
  server.requestTimeout = 30000;
  return server;
}

module.exports = {
  ROUTES,
  HttpError,
  loadConfig,
  createHandler,
  createProxyServer,
  parseCookies,
  rewriteSetCookie,
  projectProfile,
  projectError,
  rewriteRegistrationBody,
  projectMyRegistration,
  webCalendarLink,
  CALENDAR_WEB_LINKS,
  isJsonContentType,
  checkOrigin,
};

if (require.main === module) {
  let config;
  try {
    config = loadConfig(process.env);
  } catch (err) {
    console.error(err.message);
    process.exit(1);
  }
  const server = createProxyServer(config);
  server.listen(config.port, config.host, () => {
    console.log(`webinar-proxy listening on http://${config.host}:${config.port} -> ${config.backendOrigin}`);
  });
}
