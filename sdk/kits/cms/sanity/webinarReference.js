// Sanity Studio v3 schema type: a reference to a platform webinar, not a copy of it.
// Register in sanity.config: schema: { types: [webinarReference, ...] }, then use
// defineField({ name: 'webinar', type: 'webinarReference' }) on any document.
// Resolve at render with cms/resolve-reference.mjs.
import { defineField, defineType } from 'sanity';

const SLUG_RE = /^[-a-zA-Z0-9_]{1,200}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const webinarReference = defineType({
  name: 'webinarReference',
  title: 'Webinar',
  type: 'object',
  fields: [
    defineField({
      name: 'eventSlug',
      title: 'Event slug',
      type: 'string',
      description: 'The event slug from Studio, e.g. "q3-product-update". Title, times and hosts are loaded live from the platform.',
      validation: (rule) => rule.required().regex(SLUG_RE, { name: 'event slug' }),
    }),
    defineField({
      name: 'occurrenceId',
      title: 'Pinned session (optional)',
      type: 'string',
      description: 'A session UUID. Leave blank to show the next session open for registration.',
      validation: (rule) => rule.regex(UUID_RE, { name: 'UUID' }),
    }),
  ],
  preview: { select: { title: 'eventSlug', subtitle: 'occurrenceId' } },
});
