/*
 * Generic headless seam: CMS reference → live webinar data → WebinarForm props.
 * Licensed under the Apache License, Version 2.0.
 *
 * Each CMS stores only { eventSlug, occurrenceId? } (field names vary per CMS; see
 * toReference). This resolves it server-side against GET /api/v1/events/{slug}, so
 * titles, times, hosts, capacity and prices are never copied into the CMS.
 * Vendor SDK calls that fetch the CMS entry itself are out of scope: pass the
 * already-fetched field value in.
 */
import { formProps, getWebinarEvent, pickOccurrence, SLUG_RE, UUID_RE } from '../headless/webinar-events.mjs';

/** Normalises the stored value from Sanity, Payload, Prismic or Contentful into { eventSlug, occurrenceId }. */
export function toReference(value) {
  if (!value || typeof value !== 'object') return null;
  // Contentful delivery entries nest values under .fields; Prismic slices under .primary.
  const src = value.fields && typeof value.fields === 'object' ? value.fields : value.primary && typeof value.primary === 'object' ? value.primary : value;
  const slug = src.eventSlug ?? src.event_slug;
  const occ = src.occurrenceId ?? src.occurrence_id;
  if (typeof slug !== 'string' || !SLUG_RE.test(slug.trim())) return null;
  return {
    eventSlug: slug.trim(),
    occurrenceId: typeof occ === 'string' && UUID_RE.test(occ.trim()) ? occ.trim().toLowerCase() : null,
  };
}

/**
 * { event, occurrence, form } for a stored reference; null when the reference is invalid
 * or the event is gone (render nothing or a "no longer available" note). form is null
 * when registration is closed. Throws on backend failure so the caller can choose a fallback.
 */
export async function resolveWebinarReference(value, options) {
  const ref = toReference(value);
  if (!ref) return null;
  const event = await getWebinarEvent(ref.eventSlug, options);
  if (!event) return null;
  const occurrence = pickOccurrence(event, ref.occurrenceId);
  return { event, occurrence, form: formProps(event, occurrence) };
}
