// Payload CMS 3 field: a reference to a platform webinar, not a copy of it.
// Add to a collection: fields: [webinarReference(), ...]. Resolve at render with
// cms/resolve-reference.mjs. (Payload 2 imports Field from 'payload/types'.)
import type { Field } from 'payload';

const SLUG_RE = /^[-a-zA-Z0-9_]{1,200}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const webinarReference = (name = 'webinar'): Field => ({
  name,
  type: 'group',
  label: 'Webinar',
  fields: [
    {
      name: 'eventSlug',
      type: 'text',
      required: true,
      admin: { description: 'Event slug from Studio. Title, times and hosts are loaded live from the platform.' },
      validate: (value: unknown) =>
        (typeof value === 'string' && SLUG_RE.test(value)) || 'Use the event slug (letters, digits, - and _).',
    },
    {
      name: 'occurrenceId',
      type: 'text',
      admin: { description: 'Optional session UUID. Blank shows the next open session.' },
      validate: (value: unknown) =>
        value == null || value === '' || (typeof value === 'string' && UUID_RE.test(value)) || 'Must be a session UUID.',
    },
  ],
});
