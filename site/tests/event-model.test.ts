import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  artPath,
  collectTopics,
  demoRecordingPath,
  eventState,
  formatWhen,
  isLocalPath,
  parseEvent,
  parseEventPage,
  parseFormSchema,
  sortForDisplay,
} from '../lib/event-model.ts';

const NOW = Date.parse('2026-10-24T12:00:00Z');
const OCC = '0f8b2d4e-6c1a-4b7e-9f3d-2a5c8e1b7d90';
const OCC2 = '1f8b2d4e-6c1a-4b7e-9f3d-2a5c8e1b7d91';

function rawEvent(overrides: Record<string, unknown> = {}) {
  return {
    slug: 'data-101',
    title: 'Data 101',
    summary: 'A summary.',
    heroImageUrl: 'https://visioncast-ten.vercel.app/demo-art/data-101.svg',
    aida: { attention: 'Look', interest: 'One.\n\nTwo.', decision: 'For you', action: 'Register' },
    learningOutcomes: ['A', '', 3],
    hosts: [{ name: 'Ada Lovelace', slug: 'ada', title: 'Analyst', imageUrl: 'https://cdn.example/ada.png' }],
    topics: [{ name: 'Data', slug: 'data' }],
    formVersion: '2025-default',
    cta: { register: 'Save my seat', confirm: 'Confirm my RSVP' },
    occurrences: [
      {
        id: OCC,
        startsAt: '2026-11-04T15:00:00Z',
        endsAt: '2026-11-04T16:00:00Z',
        timezone: 'Europe/London',
        status: 'scheduled',
        registrationOpen: true,
        recordingUrl: null,
        calendarUrl: `https://api.example.com/api/v1/occurrences/${OCC}/calendar.ics`,
        calendarLinks: {
          ics: `https://api.example.com/api/v1/occurrences/${OCC}/calendar.ics`,
          google: 'https://calendar.google.com/calendar/render?action=TEMPLATE',
          outlook: 'https://evil.example/calendar/0/deeplink/compose',
          office365: null,
        },
        joinUrl: 'https://provider.example/join/123',
      },
    ],
    ...overrides,
  };
}

test('parseEvent keeps documented public fields only', () => {
  const e = parseEvent(rawEvent());
  assert.ok(e);
  assert.deepEqual(e.learningOutcomes, ['A']);
  assert.deepEqual(e.hosts, [{ name: 'Ada Lovelace', slug: 'ada', title: 'Analyst' }]);
  const occ = e.occurrences[0];
  assert.equal(occ.icsUrl, `/webinar-api/calendar.ics?occurrenceId=${OCC}`);
  assert.ok(occ.googleUrl?.startsWith('https://calendar.google.com/'));
  assert.equal(occ.outlookUrl, null);
  assert.ok(!JSON.stringify(e).includes('provider.example'));
  assert.ok(!JSON.stringify(e).includes('api.example.com'));
});

test('parseEvent rejects bad slugs and drops bad occurrences', () => {
  assert.equal(parseEvent(rawEvent({ slug: '../x' })), null);
  const e = parseEvent(rawEvent({ occurrences: [{ id: 'nope', startsAt: 'x', endsAt: 'y' }] }));
  assert.equal(e?.occurrences.length, 0);
});

test('recording urls must be https', () => {
  const e = parseEvent(
    rawEvent({
      occurrences: [
        { id: OCC, startsAt: '2026-01-01T10:00:00Z', endsAt: '2026-01-01T11:00:00Z', timezone: 'UTC', status: 'completed', recordingUrl: 'javascript:alert(1)' },
        { id: OCC2, startsAt: '2026-02-01T10:00:00Z', endsAt: '2026-02-01T11:00:00Z', timezone: 'UTC', status: 'completed', recordingUrl: 'https://visioncast-ten.vercel.app/recordings/demo' },
      ],
    }),
  );
  assert.equal(e?.occurrences[0].recordingUrl, null);
  assert.equal(e?.occurrences[1].recordingUrl, 'https://visioncast-ten.vercel.app/recordings/demo');
  assert.equal(eventState(e!, NOW), 'recording');
});

test('seeded demo recording links become the local demo page', () => {
  const occ = (id: string, recordingUrl: string) => ({
    id,
    startsAt: '2026-01-01T10:00:00Z',
    endsAt: '2026-01-01T11:00:00Z',
    timezone: 'UTC',
    status: 'completed',
    recordingUrl,
  });
  const e = parseEvent(
    rawEvent({
      occurrences: [
        occ(OCC, 'https://visioncast-ten.vercel.app/demo-recording/demo-event-ops-interview'),
        occ(OCC2, 'https://visioncast-ten.vercel.app/demo-recording/../admin'),
      ],
    }),
  );
  assert.equal(e?.occurrences[0].recordingUrl, '/demo-recording/demo-event-ops-interview');
  // Not the seed's exact shape: treated as an ordinary external https link.
  assert.equal(e?.occurrences[1].recordingUrl, 'https://visioncast-ten.vercel.app/admin');
  assert.equal(demoRecordingPath('https://evil.example/demo-recording/x'), null);
  assert.equal(demoRecordingPath('/demo-recording/x?y=1'), null);
  assert.ok(isLocalPath('/demo-recording/x'));
  assert.ok(!isLocalPath('//evil.example/x'));
  assert.ok(!isLocalPath('https://visioncast-ten.vercel.app/x'));
});

test('event state and ordering', () => {
  const upcoming = parseEvent(rawEvent())!;
  const past = parseEvent(
    rawEvent({
      slug: 'old',
      occurrences: [{ id: OCC2, startsAt: '2026-01-01T10:00:00Z', endsAt: '2026-01-01T11:00:00Z', timezone: 'UTC', status: 'completed' }],
    }),
  )!;
  assert.equal(eventState(upcoming, NOW), 'upcoming');
  assert.equal(eventState(past, NOW), 'past');
  assert.deepEqual(sortForDisplay([past, upcoming], NOW).map((e) => e.slug), ['data-101', 'old']);
});

test('art path is always local', () => {
  assert.equal(artPath({ slug: 'data-101', heroImageUrl: 'https://visioncast-ten.vercel.app/demo-art/data-101.svg' }), '/demo-art/data-101.svg');
  assert.equal(artPath({ slug: 'data-101', heroImageUrl: 'https://cdn.example/hero.png' }), '/demo-art/data-101.svg');
  assert.equal(artPath({ slug: 'data-101', heroImageUrl: null }), '/demo-art/data-101.svg');
});

test('event page and topics', () => {
  const page = parseEventPage({ count: 2, limit: 12, offset: 0, results: [rawEvent(), { slug: 'x' }] });
  assert.equal(page?.results.length, 1);
  assert.deepEqual(collectTopics(page!.results), [{ name: 'Data', slug: 'data' }]);
  assert.equal(parseEventPage({ results: 'no' }), null);
});

test('form schema parsing', () => {
  const s = parseFormSchema({
    schemaVersion: '1',
    formVersion: '2025-default',
    fields: [
      { key: 'email', type: 'email', label: 'Work email', required: true, autocomplete: 'email' },
      { key: 'custom.role_focus', type: 'select', label: 'Focus', options: ['Eng', 'Ops'], custom: true },
      { key: 'bad key!', type: 'text' },
      { key: 'company', type: 'weird', label: 'Company' },
    ],
    sms: { disclosureVersion: 'sms-1', disclosureText: 'Texts' },
  });
  assert.ok(s);
  assert.deepEqual(s.fields.map((f) => f.key), ['email', 'custom.role_focus', 'company']);
  assert.equal(s.fields[2].type, 'text');
  assert.equal(s.sms?.disclosureVersion, 'sms-1');
});

test('formatWhen uses the occurrence time zone', () => {
  const w = formatWhen({ startsAt: '2026-11-04T15:00:00Z', endsAt: '2026-11-04T16:30:00Z', timezone: 'America/New_York' });
  assert.equal(w.minutes, 90);
  assert.match(w.time, /^10:00/);
});
