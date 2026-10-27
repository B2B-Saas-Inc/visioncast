import 'server-only';

import { cache } from 'react';

import { parseEvent, parseEventPage, parseFormSchema, type EventItem, type EventPage, type FormSchema } from './event-model.ts';
import { parseApiBase } from './proxy-core.ts';

/*
 * Server-side reads of the public catalogue from VISIONCAST_API_BASE. These endpoints
 * need no key, so the proxy key is never sent from here. Failures are returned as
 * explicit results so pages show an "unavailable" state; there is no static fallback.
 */

export type Result<T> = { ok: true; data: T } | { ok: false; reason: 'not_configured' | 'unavailable' | 'invalid_response' };
export type Found<T> = Result<T> | { ok: false; reason: 'not_found' };

const TIMEOUT_MS = 8000;
const MAX_BYTES = 2 * 1024 * 1024;
const REVALIDATE_SECONDS = 30;

let warned = false;

async function getJson(path: string, revalidate = REVALIDATE_SECONDS): Promise<{ status: number; body: unknown } | { error: 'not_configured' | 'unavailable' | 'invalid_response' }> {
  const base = parseApiBase(process.env);
  if (!base.ok) {
    if (!warned) {
      warned = true;
      console.error(`visioncast: ${base.error}`);
    }
    return { error: 'not_configured' };
  }
  let res: Response;
  try {
    res = await fetch(new URL(path, base.origin), {
      headers: { accept: 'application/json' },
      redirect: 'manual',
      signal: AbortSignal.timeout(TIMEOUT_MS),
      next: { revalidate },
    });
  } catch {
    return { error: 'unavailable' };
  }
  if (res.status >= 500 || (res.status >= 300 && res.status < 400)) return { error: 'unavailable' };
  const type = (res.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
  if (type !== 'application/json') return { error: 'invalid_response' };
  const text = await res.text();
  if (text.length > MAX_BYTES) return { error: 'invalid_response' };
  try {
    return { status: res.status, body: JSON.parse(text) };
  } catch {
    return { error: 'invalid_response' };
  }
}

export interface ListParams {
  q?: string;
  topic?: string;
  when?: 'upcoming' | 'recorded';
  limit?: number;
  offset?: number;
}

export const listEvents = cache(async (params: ListParams = {}): Promise<Result<EventPage>> => {
  const qs = new URLSearchParams();
  if (params.q) qs.set('q', params.q);
  if (params.topic) qs.set('topic', params.topic);
  if (params.when) qs.set('when', params.when);
  qs.set('limit', String(params.limit ?? 12));
  qs.set('offset', String(params.offset ?? 0));
  const res = await getJson(`/api/v1/events?${qs}`);
  if ('error' in res) return { ok: false, reason: res.error };
  if (res.status !== 200) return { ok: false, reason: 'invalid_response' };
  const page = parseEventPage(res.body);
  return page ? { ok: true, data: page } : { ok: false, reason: 'invalid_response' };
});

export const getEvent = cache(async (slug: string): Promise<Found<EventItem>> => {
  if (!/^[A-Za-z0-9_-]{1,200}$/.test(slug)) return { ok: false, reason: 'not_found' };
  const res = await getJson(`/api/v1/events/${encodeURIComponent(slug)}`);
  if ('error' in res) return { ok: false, reason: res.error };
  if (res.status === 404) return { ok: false, reason: 'not_found' };
  if (res.status !== 200) return { ok: false, reason: 'invalid_response' };
  const event = parseEvent(res.body);
  return event ? { ok: true, data: event } : { ok: false, reason: 'invalid_response' };
});

export const getFormSchema = cache(async (version: string): Promise<Found<FormSchema>> => {
  if (!/^[A-Za-z0-9._:-]{1,64}$/.test(version)) return { ok: false, reason: 'not_found' };
  const res = await getJson(`/api/v1/forms/${encodeURIComponent(version)}`, 300);
  if ('error' in res) return { ok: false, reason: res.error };
  if (res.status === 404) return { ok: false, reason: 'not_found' };
  if (res.status !== 200) return { ok: false, reason: 'invalid_response' };
  const schema = parseFormSchema(res.body);
  return schema ? { ok: true, data: schema } : { ok: false, reason: 'invalid_response' };
});
