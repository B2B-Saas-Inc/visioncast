import 'server-only';

import {
  CSRF_HEADER,
  MAX_ICS_BODY,
  MAX_UPSTREAM_BODY,
  checkCsrf,
  checkOrigin,
  clientIp,
  csrfSetCookie,
  isJsonContentType,
  isPlainObject,
  isValidSessionValue,
  parseCookies,
  parseProxyConfig,
  profileQuery,
  projectError,
  projectMyRegistration,
  projectProfile,
  projectRegistration,
  readBounded,
  rewriteSetCookie,
  singleOccurrenceId,
  CSRF_VALUE_RE,
  type ProxyConfig,
} from './proxy-core.ts';

/** The fixed allowlist: each /webinar-api/<route> maps to exactly one backend path and method. */
export type ProxyRoute = 'profile' | 'registrations' | 'reset' | 'my-registration' | 'calendar';

const SPECS: Record<ProxyRoute, { method: 'GET' | 'POST' }> = {
  profile: { method: 'GET' },
  registrations: { method: 'POST' },
  reset: { method: 'POST' },
  'my-registration': { method: 'GET' },
  calendar: { method: 'GET' },
};

class ProxyError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

const BASE_HEADERS: Record<string, string> = {
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  Vary: 'Origin, Cookie',
};

let warned = false;

function loadConfig(): ProxyConfig {
  const result = parseProxyConfig(process.env);
  if (!result.ok) {
    if (!warned) {
      warned = true;
      // Messages name variables only, never their values.
      console.error(`visioncast proxy: not configured:\n- ${result.errors.join('\n- ')}`);
    }
    throw new ProxyError(503, 'proxy_not_configured');
  }
  return result.config;
}

function json(status: number, payload: unknown, cookies: string[] = []): Response {
  const headers = new Headers(BASE_HEADERS);
  headers.set('Content-Type', 'application/json; charset=utf-8');
  for (const c of cookies) headers.append('Set-Cookie', c);
  return new Response(JSON.stringify(payload), { status, headers });
}

function randomToken(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Buffer.from(bytes).toString('base64url');
}

async function upstream(config: ProxyConfig, path: string, init: { method: string; headers: Record<string, string>; body?: string }) {
  let res: Response;
  try {
    res = await fetch(new URL(path, config.apiBase), {
      method: init.method,
      headers: init.headers,
      body: init.body,
      redirect: 'manual',
      cache: 'no-store',
      signal: AbortSignal.timeout(config.timeoutMs),
    });
  } catch (err) {
    const timedOut = err instanceof Error && (err.name === 'TimeoutError' || err.name === 'AbortError');
    throw new ProxyError(timedOut ? 504 : 502, timedOut ? 'gateway_timeout' : 'bad_gateway');
  }
  const declared = Number(res.headers.get('content-length') || '0');
  if (declared > MAX_UPSTREAM_BODY) throw new ProxyError(502, 'bad_gateway');
  const body = await readBounded(res.body, MAX_UPSTREAM_BODY);
  if (body === null) throw new ProxyError(502, 'bad_gateway');
  return { status: res.status, headers: res.headers, body };
}

async function handleCalendar(req: Request, config: ProxyConfig): Promise<Response> {
  const id = singleOccurrenceId(new URL(req.url).searchParams);
  if (!id) throw new ProxyError(400, 'invalid_query');
  // Public file: no key, no cookie forwarded.
  const res = await upstream(config, `/api/v1/occurrences/${id}/calendar.ics`, {
    method: 'GET',
    headers: { accept: 'text/calendar', 'x-request-id': crypto.randomUUID() },
  });
  if (res.status === 404) throw new ProxyError(404, 'not_found');
  if (res.status !== 200) throw new ProxyError(502, 'bad_gateway');
  const type = (res.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
  if (type !== 'text/calendar' || res.body.byteLength > MAX_ICS_BODY) throw new ProxyError(502, 'bad_gateway');
  const headers = new Headers(BASE_HEADERS);
  headers.set('Content-Type', 'text/calendar; charset=utf-8');
  headers.set('Content-Disposition', 'attachment; filename="visioncast-event.ics"');
  return new Response(new TextDecoder().decode(res.body), { status: 200, headers });
}

async function handle(route: ProxyRoute, req: Request): Promise<Response> {
  const spec = SPECS[route];
  if (req.method !== spec.method) {
    const res = json(405, { error: { code: 'method_not_allowed' } });
    res.headers.set('Allow', spec.method);
    return res;
  }
  const config = loadConfig();
  if (!checkOrigin(req.headers.get('origin'), req.headers.get('sec-fetch-site'), spec.method, config.allowedOrigins)) {
    throw new ProxyError(403, 'origin_rejected');
  }
  if (route === 'calendar') return handleCalendar(req, config);

  const url = new URL(req.url);
  const cookies = parseCookies(req.headers.get('cookie'));
  const headers: Record<string, string> = {
    accept: 'application/json',
    authorization: `Bearer ${config.proxyKey}`,
    'x-request-id': crypto.randomUUID(),
    'x-forwarded-proto': 'https',
  };
  const ip = clientIp(req.headers.get('x-forwarded-for'));
  if (ip) headers['x-forwarded-for'] = ip;
  // Only the capability cookie is forwarded; the CSRF cookie and anything else stay here.
  const session = cookies.get(config.sessionCookieName);
  if (isValidSessionValue(session)) headers.cookie = `${config.sessionCookieName}=${session}`;

  const setCookies: string[] = [];
  let path: string;
  let body: string | undefined;

  if (route === 'profile') {
    const q = profileQuery(url.searchParams);
    if (!q) throw new ProxyError(400, 'invalid_query');
    path = `/api/v1/profile?${new URLSearchParams(q)}`;
    const existing = cookies.get(config.csrfCookieName);
    if (!existing || !CSRF_VALUE_RE.test(existing)) setCookies.push(csrfSetCookie(config.csrfCookieName, randomToken()));
  } else if (route === 'my-registration') {
    // Reads the capability, so it needs the double-submit token like a POST.
    if (!checkCsrf(req.headers.get(CSRF_HEADER), cookies.get(config.csrfCookieName))) throw new ProxyError(403, 'csrf_failed');
    const id = singleOccurrenceId(url.searchParams);
    if (!id) throw new ProxyError(400, 'invalid_query');
    path = `/api/v1/my-registration?${new URLSearchParams({ occurrenceId: id })}`;
  } else {
    if (req.headers.get('origin') === null) throw new ProxyError(403, 'origin_rejected');
    if (!checkCsrf(req.headers.get(CSRF_HEADER), cookies.get(config.csrfCookieName))) throw new ProxyError(403, 'csrf_failed');
    if ([...url.searchParams.keys()].length) throw new ProxyError(400, 'invalid_query');
    if (!isJsonContentType(req.headers.get('content-type'))) throw new ProxyError(415, 'unsupported_media_type');
    const declared = req.headers.get('content-length');
    if (declared !== null && !(/^\d+$/.test(declared) && Number(declared) <= config.bodyLimit)) {
      throw new ProxyError(413, 'payload_too_large');
    }
    const raw = await readBounded(req.body, config.bodyLimit);
    if (raw === null) throw new ProxyError(413, 'payload_too_large');
    let parsed: unknown = {};
    try {
      // Invalid UTF-8 is rejected (as the backend does), never silently replaced.
      const text = new TextDecoder('utf-8', { fatal: true }).decode(raw);
      if (text.trim() || route === 'registrations') parsed = JSON.parse(text);
    } catch {
      throw new ProxyError(400, 'invalid_json');
    }
    if (!isPlainObject(parsed)) throw new ProxyError(400, 'invalid_json');
    if (route === 'registrations' && parsed.schemaVersion !== '1') throw new ProxyError(400, 'unsupported_schema_version');
    path = route === 'registrations' ? '/api/v1/registrations' : '/api/v1/reset';
    body = JSON.stringify(route === 'reset' ? {} : parsed);
    headers['content-type'] = 'application/json';
  }

  const res = await upstream(config, path, { method: spec.method, headers, body });
  for (const raw of res.headers.getSetCookie()) {
    const rewritten = rewriteSetCookie(raw, config.sessionCookieName);
    if (rewritten) setCookies.push(rewritten);
  }
  if (res.status >= 500 || res.status < 200 || (res.status >= 300 && res.status < 400)) {
    throw new ProxyError(502, 'bad_gateway');
  }
  const contentType = (res.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
  if (contentType !== 'application/json') throw new ProxyError(502, 'bad_gateway');
  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder().decode(res.body));
  } catch {
    throw new ProxyError(502, 'bad_gateway');
  }
  if (res.status >= 400) return json(res.status, projectError(payload), setCookies);
  if (!isPlainObject(payload)) throw new ProxyError(502, 'bad_gateway');
  let out: unknown;
  if (route === 'profile') out = projectProfile(payload);
  else if (route === 'registrations') out = projectRegistration(payload);
  else if (route === 'my-registration') out = projectMyRegistration(payload);
  else out = { reset: payload.reset === true };
  return json(res.status, out, setCookies);
}

/** Route handler for one allowlisted proxy route. */
export function proxyHandler(route: ProxyRoute) {
  return async function handler(req: Request): Promise<Response> {
    try {
      return await handle(route, req);
    } catch (err) {
      if (err instanceof ProxyError) return json(err.status, { error: { code: err.code } });
      console.error('visioncast proxy: unexpected error', err instanceof Error ? err.name : 'unknown');
      return json(500, { error: { code: 'internal_error' } });
    }
  };
}
