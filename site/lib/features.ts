/**
 * Product features explained on /features. Static data, no network calls.
 *
 * These describe the current back end, which is newer than the one this demo talks to. Nothing here
 * is configured on the demo: no checkout, no hosting provider, no seat limits on the demo events.
 */

export const TICKET_POINTS = [
  {
    title: 'Stripe hosted checkout',
    body: 'Paid sessions send the buyer to Stripe’s own hosted checkout page. Card details never touch your site or the VisionCast back end, and a seat is fulfilled only after Stripe confirms payment.',
  },
  {
    title: 'Verified email before a paid hold',
    body: 'A seat is held only after the buyer proves they own the email address with a code or link sent to it. Throwaway addresses cannot tie up seats, and every unverified caller gets the same answer.',
  },
  {
    title: 'Atomic headcount limits',
    body: 'Seat limits are enforced under a database lock, so two buyers cannot take the last seat. Registrations and open checkouts both count. Expired or abandoned checkouts release their seat.',
  },
  {
    title: 'Show or hide openings',
    body: 'Organisers choose whether a session publishes how many seats remain, or only whether it is full.',
  },
] as const;

export type Support = 'yes' | 'no' | 'unverified';

export interface HostingCapability {
  provider: string;
  create: Support;
  update: Support;
  attach: Support;
  perRegistrantLinks: Support;
  manualLink: Support;
  note: string;
}

/** Mirrors the capability matrix in docs/hosting.md. Not every platform supports the same things. */
export const HOSTING_CAPABILITIES: HostingCapability[] = [
  {
    provider: 'Zoom',
    create: 'yes',
    update: 'yes',
    attach: 'yes',
    perRegistrantLinks: 'no',
    manualLink: 'no',
    note: 'Built and mock-tested; unverified with a real account.',
  },
  {
    provider: 'Wistia',
    create: 'yes',
    update: 'no',
    attach: 'no',
    perRegistrantLinks: 'no',
    manualLink: 'no',
    note: 'Built and mock-tested; unverified with a real account.',
  },
  {
    provider: 'Sequel',
    create: 'yes',
    update: 'no',
    attach: 'no',
    perRegistrantLinks: 'no',
    manualLink: 'no',
    note: 'Built and mock-tested; unverified with a real account.',
  },
  {
    provider: 'Riverside',
    create: 'no',
    update: 'no',
    attach: 'yes',
    perRegistrantLinks: 'unverified',
    manualLink: 'no',
    note: 'Limited: attach existing events only. Registrant API paths and fields unverified.',
  },
  {
    provider: 'StreamYard',
    create: 'no',
    update: 'no',
    attach: 'no',
    perRegistrantLinks: 'no',
    manualLink: 'yes',
    note: 'Manual link only. No public API, so no outbound calls.',
  },
];

export const SUPPORT_LABEL: Record<Support, string> = {
  yes: 'Yes',
  no: 'No',
  unverified: 'Built, unverified',
};

export const HOSTING_LIMITATIONS = [
  'Provisioning always shows a preview and waits for an organiser to confirm. Nothing is created automatically.',
  'Every adapter is tested against static mocked vendor APIs only, not against real vendor accounts.',
  'Features differ by platform: only Zoom can update a session it created, and only Riverside offers per-registrant links.',
] as const;
