/*
 * Server-only reader for the public webinar catalogue. Native fetch, no dependencies.
 * Licensed under the Apache License, Version 2.0.
 *
 * Reads GET /api/v1/events/{slug} (public, unauthenticated; no key is sent). CMS
 * entries store only a reference (event slug, optional occurrence UUID); titles,
 * times, hosts and capacity always come from here, so nothing is duplicated.
 *
 * Environment (server-side only; never prefix with NEXT_PUBLIC_/PUBLIC_/VITE_):
 *   WEBINAR_API_BASE               backend origin, https (http only for localhost with
 *                                  WEBINAR_ALLOW_INSECURE_BACKEND=1)
 *   WEBINAR_SDK_URL                optional SDK script override (https URL or site path)
 */

export const SLUG_RE = /^[-a-zA-Z0-9_]{1,200}$/;
export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const FORM_VERSION_RE = /^[A-Za-z0-9._:-]{1,128}$/;
export const SDK_PATH = '/static/webinar-sdk/webinar-form.js';

const MAX_BODY = 512 * 1024;

/** The validated backend origin, or throws: configuration errors should fail the build/boot. */
export function backendOrigin(env = process.env) {
  let u;
  try {
    u = new URL(env.WEBINAR_API_BASE || '');
  } catch {
    throw new Error('WEBINAR_API_BASE must be an absolute URL');
  }
  const local = u.hostname === 'localhost' || u.hostname === '127.0.0.1';
  const insecureOk = env.WEBINAR_ALLOW_INSECURE_BACKEND === '1' && local && u.protocol === 'http:';
  if (u.protocol !== 'https:' && !insecureOk) throw new Error('WEBINAR_API_BASE must use https');
  if (u.username || u.password || u.search || u.hash || (u.pathname && u.pathname !== '/')) {
    throw new Error('WEBINAR_API_BASE must be an origin only (scheme://host[:port])');
  }
  return u.origin;
}

/** The SDK script URL: an override (https URL or same-site path), else the backend's published file. */
export function sdkScriptUrl(env = process.env) {
  const override = env.WEBINAR_SDK_URL || '';
  if (override) {
    if (safeScriptSrc(override)) return override;
    throw new Error('WEBINAR_SDK_URL must be an https URL or a path on this site');
  }
  return backendOrigin(env) + SDK_PATH;
}

/** https URL without credentials, or an absolute path on this origin; else null. */
export function safeScriptSrc(src) {
  if (typeof src !== 'string' || !src || src.length > 2048 || /[\s"'<>\\]/.test(src)) return null;
  if (src[0] === '/') return src[1] === '/' ? null : src;
  try {
    const u = new URL(src);
    return u.protocol === 'https:' && !u.username && !u.password ? u.href : null;
  } catch {
    return null;
  }
}

function str(value, max) {
  return typeof value === 'string' && value.length <= max ? value : null;
}

function httpsUrl(value) {
  const s = str(value, 2048);
  if (!s) return null;
  try {
    return new URL(s).protocol === 'https:' ? s : null;
  } catch {
    return null;
  }
}

/** Keeps the documented public fields only (webinars/api/views.py serialize_event). */
export function normalizeEvent(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const slug = str(raw.slug, 200);
  const formVersion = str(raw.formVersion, 128);
  if (!slug || !SLUG_RE.test(slug) || !formVersion || !FORM_VERSION_RE.test(formVersion)) return null;
  const list = (v) => (Array.isArray(v) ? v : []);
  return {
    slug,
    title: str(raw.title, 500) || '',
    summary: str(raw.summary, 5000) || '',
    heroImageUrl: httpsUrl(raw.heroImageUrl),
    formVersion,
    hosts: list(raw.hosts)
      .filter((h) => h && typeof h === 'object')
      .map((h) => ({ name: str(h.name, 200) || '', title: str(h.title, 200) || '', imageUrl: httpsUrl(h.imageUrl) })),
    topics: list(raw.topics)
      .filter((t) => t && typeof t === 'object')
      .map((t) => ({ name: str(t.name, 200) || '', slug: str(t.slug, 200) || '' })),
    occurrences: list(raw.occurrences)
      .filter((o) => o && typeof o === 'object' && typeof o.id === 'string' && UUID_RE.test(o.id))
      .map((o) => ({
        id: o.id.toLowerCase(),
        startsAt: str(o.startsAt, 64),
        endsAt: str(o.endsAt, 64),
        timezone: str(o.timezone, 64),
        status: str(o.status, 32),
        registrationOpen: o.registrationOpen === true,
        // Present for paid sessions (webinars/payments.py public_ticket); display only.
        ticket: o.ticket && typeof o.ticket === 'object' && !Array.isArray(o.ticket) ? o.ticket : null,
      })),
  };
}

/** Event by slug, null when unknown/invalid; throws on backend or network failure. */
export async function getWebinarEvent(slug, { env = process.env, fetchImpl = globalThis.fetch, timeoutMs = 5000 } = {}) {
  if (typeof slug !== 'string' || !SLUG_RE.test(slug)) return null;
  const url = new URL(`/api/v1/events/${encodeURIComponent(slug)}`, backendOrigin(env));
  const res = await fetchImpl(url, {
    headers: { accept: 'application/json' },
    redirect: 'error',
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`webinar backend responded ${res.status}`);
  const text = await res.text();
  if (text.length > MAX_BODY) throw new Error('webinar backend response too large');
  return normalizeEvent(JSON.parse(text));
}

/** The pinned occurrence when it belongs to the event and is open, else the next open one, else null. */
export function pickOccurrence(event, pinnedId) {
  if (!event) return null;
  const open = event.occurrences.filter((o) => o.registrationOpen);
  if (typeof pinnedId === 'string' && UUID_RE.test(pinnedId)) {
    const pinned = open.find((o) => o.id === pinnedId.toLowerCase());
    if (pinned) return pinned;
  }
  return open[0] || null;
}

/** Props for the WebinarForm components, or null when registration is not open. */
export function formProps(event, occurrence) {
  if (!event || !occurrence) return null;
  return { occurrenceId: occurrence.id, formVersion: event.formVersion };
}
