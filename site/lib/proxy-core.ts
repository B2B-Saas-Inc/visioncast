/*
 * Pure helpers for the same-origin /webinar-api/* proxy (no Next.js imports, so the
 * Node test runner can load this file directly). Mirrors sdk/proxy/server.js: exact
 * Origin allowlist, double-submit CSRF, fixed route allowlist, bounded bodies, and
 * projection of every upstream body to its documented public keys.
 */

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const FORM_VERSION_RE = /^[A-Za-z0-9._:-]{1,64}$/;
const TOKEN_RE = /^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$/; // RFC 7230 token (cookie names)
const COOKIE_VALUE_RE = /^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]{1,4096}$/; // RFC 6265 cookie-octet
export const CSRF_VALUE_RE = /^[A-Za-z0-9_-]{43}$/;
const BACKEND_ICS_RE = /^\/api\/v1\/occurrences\/([0-9a-fA-F-]{36})\/calendar\.ics$/;

export const CSRF_HEADER = 'x-webinar-csrf';
export const DEFAULT_ALLOWED_ORIGINS = ['https://visioncast-ten.vercel.app'];
/** The capability cookie is only ever needed by the proxy routes. */
export const SESSION_COOKIE_PATH = '/webinar-api';
export const CALENDAR_PATH = '/webinar-api/calendar.ics';
export const MAX_UPSTREAM_BODY = 256 * 1024;
export const MAX_ICS_BODY = 64 * 1024;

export interface ProxyConfig {
  apiBase: string;
  proxyKey: string;
  allowedOrigins: readonly string[];
  sessionCookieName: string;
  csrfCookieName: string;
  bodyLimit: number;
  timeoutMs: number;
}

export type ConfigResult = { ok: true; config: ProxyConfig } | { ok: false; errors: string[] };

type Env = Record<string, string | undefined>;

function intEnv(value: string | undefined, fallback: number): number {
  if (value === undefined || value === '') return fallback;
  return /^\d+$/.test(value) ? Number(value) : NaN;
}

/** The backend origin from VISIONCAST_API_BASE, or an error message. Messages never include values. */
export function parseApiBase(env: Env): { ok: true; origin: string } | { ok: false; error: string } {
  const raw = env.VISIONCAST_API_BASE || '';
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return { ok: false, error: 'VISIONCAST_API_BASE is required and must be an absolute URL' };
  }
  const insecureOk = env.VISIONCAST_ALLOW_INSECURE_BACKEND === '1' && env.NODE_ENV !== 'production';
  if (url.protocol !== 'https:' && !(insecureOk && url.protocol === 'http:')) {
    return { ok: false, error: 'VISIONCAST_API_BASE must use https' };
  }
  if (url.username || url.password || url.search || url.hash || (url.pathname && url.pathname !== '/')) {
    return { ok: false, error: 'VISIONCAST_API_BASE must be an origin only (scheme://host[:port])' };
  }
  return { ok: true, origin: url.origin };
}

export function parseProxyConfig(env: Env): ConfigResult {
  const errors: string[] = [];
  const base = parseApiBase(env);
  if (!base.ok) errors.push(base.error);
  const proxyKey = env.VISIONCAST_PROXY_KEY || '';
  if (proxyKey.length < 32 || proxyKey.length > 512 || !/^[\x21-\x7E]+$/.test(proxyKey)) {
    errors.push('VISIONCAST_PROXY_KEY is required (32-512 printable characters, no whitespace)');
  }
  const originsRaw = env.VISIONCAST_ALLOWED_ORIGINS;
  const allowedOrigins = originsRaw
    ? originsRaw.split(',').map((s) => s.trim()).filter(Boolean)
    : [...DEFAULT_ALLOWED_ORIGINS];
  if (!allowedOrigins.length) errors.push('VISIONCAST_ALLOWED_ORIGINS must list at least one origin');
  for (const o of allowedOrigins) {
    let ok = false;
    try {
      const u = new URL(o);
      ok = u.origin === o && (u.protocol === 'https:' || u.hostname === 'localhost' || u.hostname === '127.0.0.1');
    } catch {
      ok = false;
    }
    if (!ok) errors.push('VISIONCAST_ALLOWED_ORIGINS entries must be exact https origins');
  }
  const sessionCookieName = env.VISIONCAST_SESSION_COOKIE || 'webinar_session';
  if (!TOKEN_RE.test(sessionCookieName)) errors.push('VISIONCAST_SESSION_COOKIE is not a valid cookie name');
  const bodyLimit = intEnv(env.VISIONCAST_PROXY_BODY_LIMIT, 32 * 1024);
  if (!(bodyLimit >= 1024 && bodyLimit <= 64 * 1024)) errors.push('VISIONCAST_PROXY_BODY_LIMIT must be 1024-65536');
  const timeoutMs = intEnv(env.VISIONCAST_PROXY_TIMEOUT_MS, 10000);
  if (!(timeoutMs >= 100 && timeoutMs <= 30000)) errors.push('VISIONCAST_PROXY_TIMEOUT_MS must be 100-30000');
  if (errors.length || !base.ok) return { ok: false, errors: [...new Set(errors)] };
  return {
    ok: true,
    config: Object.freeze({
      apiBase: base.origin,
      proxyKey,
      allowedOrigins: Object.freeze(allowedOrigins),
      sessionCookieName,
      csrfCookieName: '__Host-webinar_csrf',
      bodyLimit,
      timeoutMs,
    }),
  };
}

export function parseCookies(header: string | null): Map<string, string> {
  const out = new Map<string, string>();
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

export function isValidSessionValue(value: string | undefined): value is string {
  return typeof value === 'string' && COOKIE_VALUE_RE.test(value);
}

/** Constant-time comparison of two short ASCII strings. */
export function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Exact Origin match (when present; required for POST) and Sec-Fetch-Site same-origin
 * (GET also accepts "none", i.e. a typed URL or a download link opened directly).
 */
export function checkOrigin(origin: string | null, secFetchSite: string | null, method: string, allowed: readonly string[]): boolean {
  if (secFetchSite !== null && secFetchSite !== 'same-origin' && !(method === 'GET' && secFetchSite === 'none')) return false;
  if (origin === null) return method === 'GET';
  return allowed.includes(origin);
}

export function checkCsrf(header: string | null, cookie: string | undefined): boolean {
  if (typeof header !== 'string' || !cookie) return false;
  if (!CSRF_VALUE_RE.test(header) || !CSRF_VALUE_RE.test(cookie)) return false;
  return safeEqual(header, cookie);
}

/** Readable by first-party JS (double-submit); __Host- prefix pins it to this origin, Path=/. */
export function csrfSetCookie(name: string, token: string): string {
  return `${name}=${token}; Path=/; Secure; SameSite=Strict; Max-Age=86400`;
}

/**
 * Re-issues the backend capability cookie with fixed attributes and drops anything else
 * (other cookies, Domain, a wider Path). Only Max-Age/Expires are carried over.
 */
export function rewriteSetCookie(raw: string, sessionCookieName: string): string | null {
  const parts = String(raw).split(';').map((s) => s.trim()).filter(Boolean);
  const first = parts.shift();
  if (!first) return null;
  const eq = first.indexOf('=');
  if (eq === -1) return null;
  const name = first.slice(0, eq).trim();
  const value = first.slice(eq + 1).trim().replace(/^"(.*)"$/, '$1');
  if (name !== sessionCookieName) return null;
  if (value !== '' && !COOKIE_VALUE_RE.test(value)) return null;
  const kept: string[] = [];
  for (const p of parts) {
    const [k, ...rest] = p.split('=');
    const key = k.trim().toLowerCase();
    const v = rest.join('=').trim();
    if (key === 'max-age' && /^-?\d{1,10}$/.test(v)) kept.push(`Max-Age=${v}`);
    if (key === 'expires' && /^[A-Za-z0-9 ,:-]{1,64}$/.test(v)) kept.push(`Expires=${v}`);
  }
  return [`${name}=${value}`, `Path=${SESSION_COOKIE_PATH}`, ...kept, 'HttpOnly', 'Secure', 'SameSite=Lax'].join('; ');
}

export function isJsonContentType(value: string | null): boolean {
  if (typeof value !== 'string') return false;
  const [type, ...params] = value.split(';').map((s) => s.trim().toLowerCase());
  if (type !== 'application/json') return false;
  return params.every((p) => p === '' || p === 'charset=utf-8');
}

/** The single occurrenceId UUID query parameter (lower case), or null. */
export function singleOccurrenceId(params: URLSearchParams): string | null {
  const keys = [...params.keys()];
  const id = params.get('occurrenceId') || '';
  if (keys.length !== 1 || keys[0] !== 'occurrenceId' || !UUID_RE.test(id)) return null;
  return id.toLowerCase();
}

/** occurrenceId + formVersion, exactly once each, for /profile; or null. */
export function profileQuery(params: URLSearchParams): { occurrenceId: string; formVersion: string } | null {
  const keys = [...params.keys()].sort();
  if (keys.length !== 2 || keys[0] !== 'formVersion' || keys[1] !== 'occurrenceId') return null;
  const occurrenceId = params.get('occurrenceId') || '';
  const formVersion = params.get('formVersion') || '';
  if (!UUID_RE.test(occurrenceId) || !FORM_VERSION_RE.test(formVersion)) return null;
  return { occurrenceId: occurrenceId.toLowerCase(), formVersion };
}

/** First X-Forwarded-For hop (set by the Vercel edge), when it looks like an IP address. */
export function clientIp(forwardedFor: string | null): string | null {
  if (typeof forwardedFor !== 'string') return null;
  const first = forwardedFor.split(',')[0].trim();
  return /^[0-9A-Fa-f:.]{2,45}$/.test(first) ? first : null;
}

type Json = Record<string, unknown>;

export function isPlainObject(value: unknown): value is Json {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function boundedString(value: unknown, max: number): string | undefined {
  return typeof value === 'string' && value.length <= max ? value : undefined;
}

export interface ProjectedError {
  error?: { code: string };
  fieldErrors?: Record<string, 'invalid'>;
}

/** Keeps only a machine code and offending field keys; server text never reaches the browser. */
export function projectError(body: unknown): ProjectedError {
  const out: ProjectedError = {};
  if (!isPlainObject(body)) return out;
  const err = body.error;
  const code = isPlainObject(err) ? err.code : err;
  if (typeof code === 'string' && /^[a-z0-9_]{1,64}$/.test(code)) out.error = { code };
  const fields: Record<string, 'invalid'> = {};
  const add = (k: unknown) => {
    if (typeof k === 'string' && /^[A-Za-z0-9_.]{1,128}$/.test(k) && Object.keys(fields).length < 64) fields[k] = 'invalid';
  };
  for (const src of [body.fieldErrors, isPlainObject(err) ? err.fields : null]) {
    if (isPlainObject(src)) for (const k of Object.keys(src)) add(k);
  }
  if (Object.keys(fields).length) out.fieldErrors = fields;
  return out;
}

export function projectProfile(body: Json) {
  return {
    recognized: body.recognized === true,
    missingFields: Array.isArray(body.missingFields)
      ? body.missingFields.filter((f): f is string => typeof f === 'string' && f.length <= 128).slice(0, 100)
      : [],
    formVersion: typeof body.formVersion === 'string' ? body.formVersion : '',
  };
}

/** Exact https origin + path of each web calendar link (webinars/calendar_links.py). */
export const CALENDAR_WEB_LINKS = Object.freeze({
  googleUrl: 'https://calendar.google.com/calendar/render',
  outlookUrl: 'https://outlook.live.com/calendar/0/deeplink/compose',
  office365Url: 'https://outlook.office.com/calendar/0/deeplink/compose',
});

/** The link when it is https on the expected calendar service path, else null. */
export function webCalendarLink(value: unknown, expected: string): string | null {
  if (typeof value !== 'string' || value.length > 8192) return null;
  let u: URL;
  try {
    u = new URL(value);
  } catch {
    return null;
  }
  if (u.protocol !== 'https:' || u.username || u.password || u.port || u.hash) return null;
  return u.origin + u.pathname === expected ? u.href : null;
}

/** Backend ICS path (relative or absolute) → this site's fixed calendar route, else undefined. */
export function rewriteIcsUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || value.length > 2048) return undefined;
  let path = value;
  if (/^https?:\/\//i.test(value)) {
    try {
      path = new URL(value).pathname;
    } catch {
      return undefined;
    }
  }
  const m = BACKEND_ICS_RE.exec(path);
  if (m && UUID_RE.test(m[1])) return `${CALENDAR_PATH}?${new URLSearchParams({ occurrenceId: m[1].toLowerCase() })}`;
  // Already rewritten by a direct-mode backend.
  const direct = /^\/webinar-api\/calendar\.ics\?occurrenceId=([0-9a-fA-F-]{36})$/.exec(value);
  if (direct && UUID_RE.test(direct[1])) return `${CALENDAR_PATH}?${new URLSearchParams({ occurrenceId: direct[1].toLowerCase() })}`;
  return undefined;
}

export interface ThankYou {
  mode: 'message' | 'redirect';
  message?: string;
  redirectUrl?: string;
}

export interface CalendarBlock {
  title?: string;
  startsAt?: string;
  endsAt?: string;
  timezone?: string;
  icsUrl?: string;
  googleUrl: string | null;
  outlookUrl: string | null;
  office365Url: string | null;
  uid?: string;
  sequence?: number;
}

export function projectThankYou(src: Json): ThankYou {
  const out: ThankYou = { mode: src.mode === 'redirect' ? 'redirect' : 'message' };
  const message = boundedString(src.message, 4000);
  if (message !== undefined) out.message = message;
  const redirect = boundedString(src.redirectUrl, 2048);
  if (redirect !== undefined && /^https:\/\//i.test(redirect)) out.redirectUrl = redirect;
  return out;
}

export function projectCalendar(src: Json): CalendarBlock {
  const out: CalendarBlock = { googleUrl: null, outlookUrl: null, office365Url: null };
  const limits = { title: 1000, startsAt: 64, endsAt: 64, timezone: 64, uid: 255 } as const;
  for (const [key, max] of Object.entries(limits) as [keyof typeof limits, number][]) {
    const value = boundedString(src[key], max);
    if (value !== undefined) out[key] = value;
  }
  const ics = rewriteIcsUrl(src.icsUrl);
  if (ics) out.icsUrl = ics;
  out.googleUrl = webCalendarLink(src.googleUrl, CALENDAR_WEB_LINKS.googleUrl);
  out.outlookUrl = webCalendarLink(src.outlookUrl, CALENDAR_WEB_LINKS.outlookUrl);
  out.office365Url = webCalendarLink(src.office365Url, CALENDAR_WEB_LINKS.office365Url);
  if (Number.isSafeInteger(src.sequence) && (src.sequence as number) >= 0) out.sequence = src.sequence as number;
  return out;
}

/** Registration success body: status, occurrence, thank-you and calendar only. */
export function projectRegistration(body: Json) {
  return {
    status: typeof body.status === 'string' && /^[a-z_]{1,32}$/.test(body.status) ? body.status : 'registered',
    occurrenceId: typeof body.occurrenceId === 'string' && UUID_RE.test(body.occurrenceId) ? body.occurrenceId : '',
    thankYou: isPlainObject(body.thankYou) ? projectThankYou(body.thankYou) : { mode: 'message' as const },
    calendar: isPlainObject(body.calendar) ? projectCalendar(body.calendar) : null,
  };
}

export function projectMyRegistration(body: Json) {
  const registered = body.registered === true;
  return {
    occurrenceId: typeof body.occurrenceId === 'string' && UUID_RE.test(body.occurrenceId) ? body.occurrenceId : '',
    recognized: body.recognized === true,
    registered,
    status: typeof body.status === 'string' && /^[a-z_]{1,32}$/.test(body.status) ? body.status : null,
    ...(registered && isPlainObject(body.thankYou) ? { thankYou: projectThankYou(body.thankYou) } : {}),
    ...(registered && isPlainObject(body.calendar) ? { calendar: projectCalendar(body.calendar) } : {}),
  };
}

/** Reads a stream up to `limit` bytes; returns null when the limit is exceeded. */
export async function readBounded(stream: ReadableStream<Uint8Array> | null, limit: number): Promise<Uint8Array | null> {
  if (!stream) return new Uint8Array(0);
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      await reader.cancel().catch(() => undefined);
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(size);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.byteLength;
  }
  return out;
}
