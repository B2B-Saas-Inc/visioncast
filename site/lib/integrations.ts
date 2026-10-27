/**
 * The public integrations catalogue. Static data, no network calls.
 *
 * Status is deliberately conservative: nothing here is "connected" or "live". Built adapters are
 * tested against static mocked vendor APIs only, never a real vendor account. Framework and CMS kits
 * are reference code (sdk/kits/manifest.json), not turnkey tested apps. Change a status only when
 * the work it claims exists.
 */

/** Back-end test run the statuses below rely on. Static mocks only; no live vendor calls. */
export const BACKEND_TESTS = {
  passed: 1945,
  database: 'PostgreSQL',
  note: 'All vendor calls in the test suite use static mocks. No test talks to a live vendor account.',
} as const;

export const CATEGORIES = [
  { slug: 'hosting', label: 'Video & hosting' },
  { slug: 'frameworks', label: 'Framework & CMS kits' },
  { slug: 'analytics', label: 'Analytics & CDP' },
  { slug: 'crm', label: 'CRM' },
  { slug: 'marketing', label: 'Marketing automation' },
  { slug: 'email', label: 'Operational email' },
  { slug: 'automation', label: 'Workflow automation' },
] as const;

export type CategorySlug = (typeof CATEGORIES)[number]['slug'];

export type Status = 'built' | 'limited' | 'plugin-untested' | 'reference' | 'roadmap';

/** Badge text and a one-line explanation per status. No label may claim a live connection. */
export const STATUS: Record<Status, { label: string; explain: string }> = {
  built: {
    label: 'Built · mock-tested',
    explain:
      'Adapter exists and is tested against static mocked vendor APIs. Unverified with real accounts; not connected to anything in this demo.',
  },
  limited: {
    label: 'Limited',
    explain: 'Works in a narrow way today. See the detail below for exactly what is supported and what is unverified.',
  },
  'plugin-untested': {
    label: 'Plugin · runtime untested',
    explain: 'Plugin source is published but has not been run on a real WordPress install. Treat it as a starting point.',
  },
  reference: {
    label: 'Reference kit',
    explain: 'Copyable reference code or setup instructions to adapt into your own app. Not a turnkey, tested app.',
  },
  roadmap: {
    label: 'Roadmap',
    explain: 'On the roadmap. Nothing is built yet.',
  },
};

export interface Integration {
  id: string;
  name: string;
  category: CategorySlug;
  status: Status;
  summary: string;
  /** What exists today (built / in development / limited) or what is intended (planned / roadmap). */
  details: string[];
}

const kit = (id: string, name: string, summary: string, details: string[]): Integration => ({
  id,
  name,
  category: 'frameworks',
  status: 'reference',
  summary,
  details: [...details, 'Not production-tested.'],
});

const roadmap = (id: string, name: string, category: CategorySlug, summary: string): Integration => ({
  id,
  name,
  category,
  status: 'roadmap',
  summary,
  details: [],
});

export const INTEGRATIONS: Integration[] = [
  // Hosting -----------------------------------------------------------------
  {
    id: 'zoom',
    name: 'Zoom',
    category: 'hosting',
    status: 'built',
    summary: 'Create, update or attach a Zoom webinar for a scheduled session.',
    details: [
      'Create, update and attach existing.',
      'No per-attendee join links.',
      'Preview then confirm; never automatic.',
      'Unverified with a real Zoom account.',
    ],
  },
  {
    id: 'wistia',
    name: 'Wistia',
    category: 'hosting',
    status: 'built',
    summary: 'Create a Wistia webinar for a scheduled session.',
    details: ['Create only; no update or attach.', 'Unverified with a real Wistia account.'],
  },
  {
    id: 'sequel',
    name: 'Sequel',
    category: 'hosting',
    status: 'built',
    summary: 'Create a Sequel event for a scheduled session.',
    details: ['Create only; no update or attach.', 'Unverified with a real Sequel account.'],
  },
  {
    id: 'riverside',
    name: 'Riverside',
    category: 'hosting',
    status: 'limited',
    summary: 'Attach a session to an event that already exists in Riverside.',
    details: [
      'Attach existing events only; does not create Riverside events.',
      'Per-registrant join links are built but the API paths and fields are unverified.',
    ],
  },
  {
    id: 'streamyard',
    name: 'StreamYard',
    category: 'hosting',
    status: 'limited',
    summary: 'Paste a StreamYard link into a session.',
    details: ['Manual link only.', 'No public API, so no automatic creation or sync and no outbound calls.'],
  },

  // Built outbound adapters ---------------------------------------------------
  {
    id: 'posthog',
    name: 'PostHog',
    category: 'analytics',
    status: 'built',
    summary: 'Send registration and attendance events to a PostHog project.',
    details: [
      'US or EU Cloud region.',
      'Optional organisation groups from verified external IDs.',
      'Requires analytics consent before anything is sent.',
    ],
  },
  {
    id: 'hubspot',
    name: 'HubSpot',
    category: 'crm',
    status: 'built',
    summary: 'Upsert contacts and webinar participation into HubSpot CRM.',
    details: [
      'Upserts keyed by stable IDs, so retries target the same records.',
      'Optional company association from verified external IDs.',
    ],
  },
  {
    id: 'attio',
    name: 'Attio',
    category: 'crm',
    status: 'built',
    summary: 'Assert people and add a participation entry to an Attio list.',
    details: [
      'One list entry per person with webinar attributes.',
      'Optional company matching from verified external IDs.',
    ],
  },
  {
    id: 'salesforce',
    name: 'Salesforce',
    category: 'crm',
    status: 'built',
    summary: 'Upsert a Lead or Contact, an optional Account and a custom participation record in one atomic Composite request.',
    details: [
      'Sends only to your own My Domain host, which your operator must allowlist exactly; OAuth client-credentials token kept in memory only.',
      'External ID upserts keyed by our person, organisation and registration IDs, so retries target the same records; no email matching.',
      'Registrations without a last name (or company, for Leads) are skipped rather than written with placeholders.',
      'OAuth token request shape unverified against Salesforce Help; API v62.0 assumed.',
    ],
  },
  {
    id: 'pipedrive',
    name: 'Pipedrive',
    category: 'crm',
    status: 'built',
    summary: 'Find or create the person (and organisation) in Pipedrive and add one activity per webinar event.',
    details: [
      'API v2 search-then-write by our person ID in a custom field, or by email with confirmation.',
      'Activities carry a registration marker; a retry finds it on the first page and does not duplicate.',
      'Never changes marketing status, labels, deals or contact-sync fields.',
    ],
  },
  {
    id: 'customerio',
    name: 'Customer.io',
    category: 'marketing',
    status: 'built',
    summary: 'Identify people and track webinar events in Customer.io.',
    details: ['US or EU region.', 'Events may repeat on a delivery retry.'],
  },
  {
    id: 'segment',
    name: 'Segment',
    category: 'analytics',
    status: 'built',
    summary: 'Identify people and track registration and attendance events in Segment.',
    details: [
      'Profile traits are sent only when the Profile data group is allowed.',
      'Which consents gate delivery is your choice, as for any routing CDP.',
    ],
  },
  {
    id: 'amplitude',
    name: 'Amplitude',
    category: 'analytics',
    status: 'built',
    summary: 'Send registration and attendance events to an Amplitude project.',
    details: ['Requires analytics consent before anything is sent.'],
  },
  {
    id: 'ga4',
    name: 'Google Analytics 4',
    category: 'analytics',
    status: 'built',
    summary: 'Server-side registration and attendance events into GA4.',
    details: ['Requires analytics consent before anything is sent.'],
  },
  {
    id: 'mixpanel',
    name: 'Mixpanel',
    category: 'analytics',
    status: 'built',
    summary: 'Import registration and attendance events into a Mixpanel project.',
    details: [
      'US, EU or India data residency.',
      'Only IDs and webinar properties are sent; no profile data.',
      'Requires analytics consent before anything is sent.',
    ],
  },
  {
    id: 'rudderstack',
    name: 'RudderStack',
    category: 'analytics',
    status: 'built',
    summary: 'Identify people and track registration and attendance events on your RudderStack data plane.',
    details: [
      'Sends only to your own data plane host, which your operator must allowlist exactly.',
      'Requires analytics consent before anything is sent.',
    ],
  },
  {
    id: 'brevo',
    name: 'Brevo',
    category: 'marketing',
    status: 'built',
    summary: 'Send webinar events to Brevo contacts by email, optionally creating or updating the contact.',
    details: [
      'Never changes lists, subscriptions or blocklist flags.',
      'Only explicitly mapped name, company and job title fields; no URLs are sent.',
      'Events may repeat on a delivery retry.',
    ],
  },

  // Framework & CMS kits (sdk/kits/manifest.json) ------------------------------
  {
    id: 'wordpress',
    name: 'WordPress',
    category: 'frameworks',
    status: 'plugin-untested',
    summary: 'A plugin with a registration block and a same-origin proxy for an existing WordPress site.',
    details: ['Plugin source exists; runtime untested on a real WordPress install.', 'Not production-tested.'],
  },
  kit('nextjs', 'Next.js / React', 'Server-rendered event pages and forms, like this demo site.', [
    'Reference components and server routes to copy into your app.',
  ]),
  kit('astro', 'Astro', 'A registration form component for Astro pages.', ['Reference component to copy into your app.']),
  kit('nuxt', 'Nuxt / Vue', 'A registration form component for Vue and Nuxt.', [
    'Reference component to copy into your app.',
  ]),
  kit('sveltekit', 'SvelteKit', 'A registration form component and server route for SvelteKit.', [
    'Reference component and server route to copy into your app.',
  ]),
  kit('webflow', 'Webflow', 'Embed a registration form in a Webflow site.', [
    'Setup instructions and an embed snippet only.',
    'Needs a first-party reverse proxy; Webflow specifics unverified.',
  ]),
  kit('framer', 'Framer', 'Embed a registration form in a Framer site.', [
    'Setup instructions and an embed snippet only.',
    'Needs a first-party reverse proxy; Framer specifics unverified.',
  ]),
  kit('sanity', 'Sanity', 'Reference VisionCast events from Sanity content.', ['Schema definition plus a shared resolver.']),
  kit('payload', 'Payload', 'Reference VisionCast events from Payload content.', [
    'Field definition plus a shared resolver.',
  ]),
  kit('prismic', 'Prismic', 'Reference VisionCast events from Prismic slices.', ['Slice model plus a shared resolver.']),
  kit('contentful', 'Contentful', 'Reference VisionCast events from Contentful entries.', [
    'Content type import plus a shared resolver.',
  ]),

  // Roadmap -------------------------------------------------------------------
  {
    id: 'activecampaign',
    name: 'ActiveCampaign',
    category: 'marketing',
    status: 'built',
    summary: 'Create or update the contact by email on your own ActiveCampaign API host, then apply lifecycle tags.',
    details: [
      'Never adds contacts to lists or automations and never changes their status; only existing tag IDs are applied.',
      'Only explicitly mapped name, company, job title and webinar fields; never phone, and no URLs are sent.',
      'Your account API host must be on the operator allowlist exactly; the key is sent nowhere else.',
      'Site event tracking (trackcmp.net) is not implemented.',
    ],
  },
  {
    id: 'mailchimp',
    name: 'Mailchimp',
    category: 'marketing',
    status: 'built',
    summary: 'Add or update the audience member by email, apply lifecycle tags and record a member event.',
    details: [
      'New members are transactional (not marketable) unless you explicitly choose pending double opt-in; never subscribed.',
      'Never changes an existing member’s status, interests or marketing permissions.',
      'Only explicitly mapped name, company and job title merge fields; no URLs are sent.',
      'Member events may repeat on a delivery retry; each carries the delivery ID for dedupe.',
    ],
  },
  roadmap('marketo', 'Marketo', 'marketing', 'Leads and program membership in Marketo.'),
  roadmap(
    'hubspot-marketing',
    'HubSpot Marketing Hub',
    'marketing',
    'Marketing-side features beyond the built HubSpot CRM adapter.',
  ),
  roadmap('loops', 'Loops', 'marketing', 'Contacts and events in Loops.'),
  roadmap('intercom', 'Intercom', 'marketing', 'Users and events in Intercom.'),
  roadmap('resend', 'Resend', 'email', 'Send confirmations and reminders through your Resend account.'),
  roadmap('postmark', 'Postmark', 'email', 'Send confirmations and reminders through your Postmark account.'),
  roadmap('ses', 'Amazon SES', 'email', 'Send confirmations and reminders through your own SES.'),
  roadmap('n8n', 'n8n', 'automation', 'Trigger n8n workflows from VisionCast events.'),
  roadmap('zapier', 'Zapier', 'automation', 'Trigger Zaps from VisionCast events.'),
  roadmap('make', 'Make', 'automation', 'Trigger Make scenarios from VisionCast events.'),
];

export function isCategory(value: string): value is CategorySlug {
  return CATEGORIES.some((c) => c.slug === value);
}

/** Validated `?category=`; anything else means "all". */
export function parseCategory(value: string | string[] | undefined): CategorySlug | undefined {
  return typeof value === 'string' && isCategory(value) ? value : undefined;
}

export function categoryLabel(slug: CategorySlug): string {
  return CATEGORIES.find((c) => c.slug === slug)!.label;
}

const STATUS_ORDER: Status[] = ['built', 'limited', 'plugin-untested', 'reference', 'roadmap'];

/** Filtered by category, most-real first, then by name. */
export function listIntegrations(category?: CategorySlug): Integration[] {
  return INTEGRATIONS.filter((i) => !category || i.category === category).sort(
    (a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status) || a.name.localeCompare(b.name),
  );
}

export function countByCategory(): Record<CategorySlug, number> {
  const out = Object.fromEntries(CATEGORIES.map((c) => [c.slug, 0])) as Record<CategorySlug, number>;
  for (const i of INTEGRATIONS) out[i.category] += 1;
  return out;
}
