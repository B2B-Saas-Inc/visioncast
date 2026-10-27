/*
 * Types and defensive parsers for the public catalogue (webinars/api/views.py
 * serialize_event / serialize_occurrence, webinars/forms_schema.py public_schema).
 * Pure: no Next.js imports, so tests load it directly.
 */
import { CALENDAR_WEB_LINKS, isPlainObject, rewriteIcsUrl, webCalendarLink } from './proxy-core.ts';

export interface Host {
  name: string;
  slug: string;
  title: string;
}

export interface Topic {
  name: string;
  slug: string;
}

export interface Occurrence {
  id: string;
  startsAt: string;
  endsAt: string;
  timezone: string;
  status: 'scheduled' | 'cancelled' | 'completed' | string;
  registrationOpen: boolean;
  /** /demo-recording/<slug> (seeded demo page) or an external https URL. */
  recordingUrl: string | null;
  /** Same-origin ICS route (/webinar-api/calendar.ics?occurrenceId=…). */
  icsUrl: string | null;
  googleUrl: string | null;
  outlookUrl: string | null;
  office365Url: string | null;
}

export interface EventItem {
  slug: string;
  title: string;
  summary: string;
  heroImageUrl: string | null;
  aida: { attention: string; interest: string; decision: string; action: string };
  learningOutcomes: string[];
  hosts: Host[];
  topics: Topic[];
  formVersion: string;
  cta: { register: string; confirm: string };
  occurrences: Occurrence[];
}

export interface EventPage {
  count: number;
  limit: number;
  offset: number;
  results: EventItem[];
}

export interface FormField {
  key: string;
  type: 'email' | 'tel' | 'text' | 'textarea' | 'select' | 'checkbox';
  label: string;
  help: string | null;
  required: boolean;
  options: string[] | null;
  autocomplete: string | null;
  custom: boolean;
}

export interface FormSchema {
  schemaVersion: string;
  formVersion: string;
  fields: FormField[];
  sms: { disclosureVersion: string; disclosureText: string } | null;
}

const SLUG_RE = /^[A-Za-z0-9_-]{1,200}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const FIELD_TYPES = new Set(['email', 'tel', 'text', 'textarea', 'select', 'checkbox']);

function str(value: unknown, max = 20000): string {
  return typeof value === 'string' ? value.slice(0, max) : '';
}

function isInstant(value: unknown): value is string {
  return typeof value === 'string' && value.length <= 64 && !Number.isNaN(Date.parse(value));
}

function isTimeZone(value: unknown): value is string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_+\-/]{1,64}$/.test(value)) return false;
  try {
    new Intl.DateTimeFormat('en', { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

/** An https URL with no credentials, else null. Used for recording links. */
export function safeHttpsUrl(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 2048) return null;
  try {
    const u = new URL(value);
    return u.protocol === 'https:' && !u.username && !u.password ? u.href : null;
  } catch {
    return null;
  }
}

const DEMO_RECORDING_RE = /^(?:https:\/\/visioncast-ten\.vercel\.app)?\/demo-recording\/([A-Za-z0-9_-]{1,200})$/;

/**
 * The seed's recording link (RECORDING_PAGE_BASE + slug in seed_public_demo.py) as a
 * same-origin path, else null. That page explains there is no recording; it never plays one.
 */
export function demoRecordingPath(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const m = DEMO_RECORDING_RE.exec(value);
  return m ? `/demo-recording/${m[1]}` : null;
}

/** Recording link: the local demo page (same tab) or an external https URL (new tab). */
export function isLocalPath(url: string): boolean {
  return url.startsWith('/') && !url.startsWith('//');
}

export function parseOccurrence(raw: unknown): Occurrence | null {
  if (!isPlainObject(raw)) return null;
  if (typeof raw.id !== 'string' || !UUID_RE.test(raw.id)) return null;
  if (!isInstant(raw.startsAt) || !isInstant(raw.endsAt)) return null;
  const links = isPlainObject(raw.calendarLinks) ? raw.calendarLinks : {};
  const id = raw.id.toLowerCase();
  return {
    id,
    startsAt: raw.startsAt,
    endsAt: raw.endsAt,
    timezone: isTimeZone(raw.timezone) ? raw.timezone : 'UTC',
    status: typeof raw.status === 'string' ? raw.status.slice(0, 32) : 'scheduled',
    registrationOpen: raw.registrationOpen === true,
    recordingUrl: demoRecordingPath(raw.recordingUrl) ?? safeHttpsUrl(raw.recordingUrl),
    // The backend ICS link is absolute on its own host; always serve it through this origin.
    icsUrl: rewriteIcsUrl(`/api/v1/occurrences/${id}/calendar.ics`) ?? null,
    googleUrl: webCalendarLink(links.google, CALENDAR_WEB_LINKS.googleUrl),
    outlookUrl: webCalendarLink(links.outlook, CALENDAR_WEB_LINKS.outlookUrl),
    office365Url: webCalendarLink(links.office365, CALENDAR_WEB_LINKS.office365Url),
  };
}

export function parseEvent(raw: unknown): EventItem | null {
  if (!isPlainObject(raw)) return null;
  if (typeof raw.slug !== 'string' || !SLUG_RE.test(raw.slug)) return null;
  const title = str(raw.title, 500).trim();
  if (!title) return null;
  const aida = isPlainObject(raw.aida) ? raw.aida : {};
  const cta = isPlainObject(raw.cta) ? raw.cta : {};
  const hosts = Array.isArray(raw.hosts) ? raw.hosts : [];
  const topics = Array.isArray(raw.topics) ? raw.topics : [];
  const occurrences = Array.isArray(raw.occurrences) ? raw.occurrences : [];
  return {
    slug: raw.slug,
    title,
    summary: str(raw.summary, 4000),
    heroImageUrl: typeof raw.heroImageUrl === 'string' ? raw.heroImageUrl.slice(0, 2048) : null,
    aida: {
      attention: str(aida.attention),
      interest: str(aida.interest),
      decision: str(aida.decision),
      action: str(aida.action),
    },
    learningOutcomes: (Array.isArray(raw.learningOutcomes) ? raw.learningOutcomes : [])
      .filter((o): o is string => typeof o === 'string' && o.trim() !== '')
      .slice(0, 30)
      .map((o) => o.slice(0, 1000)),
    hosts: hosts
      .filter(isPlainObject)
      .map((h) => ({ name: str(h.name, 300), slug: str(h.slug, 200), title: str(h.title, 300) }))
      .filter((h) => h.name),
    topics: topics
      .filter(isPlainObject)
      .map((t) => ({ name: str(t.name, 200), slug: str(t.slug, 200) }))
      .filter((t) => t.name && SLUG_RE.test(t.slug)),
    formVersion: str(raw.formVersion, 64),
    cta: {
      register: str(cta.register, 120) || 'Save my seat',
      confirm: str(cta.confirm, 120) || 'Confirm my RSVP',
    },
    occurrences: occurrences
      .map(parseOccurrence)
      .filter((o): o is Occurrence => o !== null)
      .sort((a, b) => Date.parse(a.startsAt) - Date.parse(b.startsAt)),
  };
}

export function parseEventPage(raw: unknown): EventPage | null {
  if (!isPlainObject(raw) || !Array.isArray(raw.results)) return null;
  const num = (v: unknown, d: number) => (Number.isSafeInteger(v) && (v as number) >= 0 ? (v as number) : d);
  return {
    count: num(raw.count, 0),
    limit: num(raw.limit, 20),
    offset: num(raw.offset, 0),
    results: raw.results.map(parseEvent).filter((e): e is EventItem => e !== null),
  };
}

export function parseFormSchema(raw: unknown): FormSchema | null {
  if (!isPlainObject(raw) || !Array.isArray(raw.fields)) return null;
  const fields: FormField[] = [];
  for (const f of raw.fields) {
    if (!isPlainObject(f) || typeof f.key !== 'string' || !/^[A-Za-z0-9_.]{1,80}$/.test(f.key)) continue;
    const options = Array.isArray(f.options) ? f.options.filter((o): o is string => typeof o === 'string').slice(0, 50) : null;
    fields.push({
      key: f.key,
      type: (FIELD_TYPES.has(f.type as string) ? f.type : 'text') as FormField['type'],
      label: str(f.label, 120) || f.key,
      help: typeof f.help === 'string' && f.help ? f.help.slice(0, 300) : null,
      required: f.required === true,
      options: options && options.length ? options : null,
      autocomplete: typeof f.autocomplete === 'string' && /^[a-z -]{1,40}$/.test(f.autocomplete) ? f.autocomplete : null,
      custom: f.custom === true || f.key.startsWith('custom.'),
    });
  }
  const sms = isPlainObject(raw.sms) && typeof raw.sms.disclosureVersion === 'string'
    ? { disclosureVersion: raw.sms.disclosureVersion.slice(0, 64), disclosureText: str(raw.sms.disclosureText, 4000) }
    : null;
  return { schemaVersion: str(raw.schemaVersion, 8), formVersion: str(raw.formVersion, 64), fields, sms };
}

// ---------------------------------------------------------------------------
// Presentation helpers
// ---------------------------------------------------------------------------

export function isUpcoming(o: Occurrence, now: number): boolean {
  return o.status === 'scheduled' && Date.parse(o.endsAt) > now;
}

/** The soonest scheduled occurrence that has not ended, else null. */
export function nextOccurrence(event: EventItem, now: number): Occurrence | null {
  return event.occurrences.find((o) => isUpcoming(o, now)) ?? null;
}

export function openOccurrences(event: EventItem): Occurrence[] {
  return event.occurrences.filter((o) => o.registrationOpen);
}

/** The most recent occurrence with a published recording, else null. */
export function latestRecording(event: EventItem): Occurrence | null {
  const withRecording = event.occurrences.filter((o) => o.recordingUrl);
  return withRecording.length ? withRecording[withRecording.length - 1] : null;
}

export type EventState = 'upcoming' | 'recording' | 'past';

export function eventState(event: EventItem, now: number): EventState {
  if (nextOccurrence(event, now)) return 'upcoming';
  if (latestRecording(event)) return 'recording';
  return 'past';
}

/** Sort key: upcoming events by next start, then recordings/past by most recent start. */
export function sortForDisplay(events: EventItem[], now: number): EventItem[] {
  const key = (e: EventItem) => {
    const next = nextOccurrence(e, now);
    if (next) return [0, Date.parse(next.startsAt)];
    const last = e.occurrences[e.occurrences.length - 1];
    return [1, last ? -Date.parse(last.startsAt) : 0];
  };
  return [...events].sort((a, b) => {
    const [ga, ka] = key(a);
    const [gb, kb] = key(b);
    return ga - gb || ka - kb;
  });
}

const ART_RE = /^(?:https:\/\/visioncast-ten\.vercel\.app)?\/demo-art\/([A-Za-z0-9_-]{1,80})\.svg$/;

/**
 * Local event art. A hero URL that points at this site's /demo-art/ is used (as a
 * same-origin path); anything else falls back to art generated from the slug, so no
 * third-party image host is ever loaded.
 */
export function artPath(event: Pick<EventItem, 'slug' | 'heroImageUrl'>): string {
  const m = event.heroImageUrl ? ART_RE.exec(event.heroImageUrl) : null;
  if (m) return `/demo-art/${m[1]}.svg`;
  return `/demo-art/${encodeURIComponent(event.slug.slice(0, 80))}.svg`;
}

/** Blank-line separated paragraphs (plain text from the CMS; never HTML). */
export function paragraphs(text: string): string[] {
  return text
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter(Boolean);
}

export function collectTopics(events: EventItem[]): Topic[] {
  const seen = new Map<string, Topic>();
  for (const e of events) for (const t of e.topics) if (!seen.has(t.slug)) seen.set(t.slug, t);
  return [...seen.values()].sort((a, b) => a.name.localeCompare(b.name));
}

export function formatWhen(o: Pick<Occurrence, 'startsAt' | 'endsAt' | 'timezone'>, locale = 'en-GB') {
  const start = new Date(o.startsAt);
  const end = new Date(o.endsAt);
  const tz = o.timezone;
  const date = new Intl.DateTimeFormat(locale, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone: tz }).format(start);
  const time = new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit', timeZone: tz });
  const zone =
    new Intl.DateTimeFormat(locale, { timeZone: tz, timeZoneName: 'short' })
      .formatToParts(start)
      .find((p) => p.type === 'timeZoneName')?.value ?? tz;
  const minutes = Math.max(0, Math.round((end.getTime() - start.getTime()) / 60000));
  return { date, time: `${time.format(start)}–${time.format(end)}`, zone, minutes };
}
