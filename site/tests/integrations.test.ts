import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  BACKEND_TESTS,
  CATEGORIES,
  INTEGRATIONS,
  STATUS,
  countByCategory,
  listIntegrations,
  parseCategory,
} from '../lib/integrations.ts';
import { HOSTING_CAPABILITIES, HOSTING_LIMITATIONS } from '../lib/features.ts';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const read = (...p: string[]) => readFileSync(join(ROOT, ...p), 'utf8');

const idsWith = (status: string) =>
  INTEGRATIONS.filter((i) => i.status === status)
    .map((i) => i.id)
    .sort();

test('no status claims a live connection', () => {
  for (const { label } of Object.values(STATUS)) {
    assert.doesNotMatch(label, /connected|live|verified|active|available|ready|integrated/i, label);
  }
  assert.match(STATUS.built.explain, /mock/i);
  assert.match(STATUS.built.explain, /unverified with real accounts/i);
  assert.match(STATUS.built.explain, /not connected/i);
  assert.equal(STATUS.reference.label, 'Reference kit');
  assert.match(STATUS.reference.explain, /not a turnkey, tested app/i);
  assert.match(STATUS['plugin-untested'].label, /untested/i);
});

test('backend test claim is static-mock only', () => {
  assert.equal(BACKEND_TESTS.passed, 2001);
  assert.equal(BACKEND_TESTS.database, 'PostgreSQL');
  assert.match(BACKEND_TESTS.note, /static mocks/i);
  assert.match(BACKEND_TESTS.note, /no test talks to a live vendor/i);
});

test('the page and cards render no connected badge or live-key wording', () => {
  const page = read('app', 'integrations', 'page.tsx');
  assert.doesNotMatch(page, />\s*(Connected|Live|Verified|Active)\s*</);
  assert.doesNotMatch(page, /checkout|stripe|price/i);
  // Badge text only ever comes from STATUS labels.
  assert.match(page, /\{STATUS\[item\.status\]\.label\}/);
  for (const i of INTEGRATIONS) {
    assert.doesNotMatch(`${i.summary} ${i.details.join(' ')}`, /\b(is|are) (live|connected)\b/i, i.id);
  }
});

const byId = Object.fromEntries(INTEGRATIONS.map((i) => [i.id, i]));

test('only mock-tested outbound, hosting and automation-preset integrations are marked built', () => {
  assert.deepEqual(idsWith('built'), [
    'activecampaign', 'amplitude', 'attio', 'beehiiv', 'brevo', 'customerio', 'ga4', 'hubspot', 'mailchimp', 'make', 'mixpanel',
    'n8n', 'pipedrive', 'posthog', 'rudderstack', 'salesforce', 'segment', 'sequel', 'wistia', 'zapier', 'zoom',
  ]);
});

// Mirrors PROVIDER_CHOICES in webinars/native_integrations.py plus the module registry in
// webinars/native_providers/ (generic webhook excluded), in registration order.
const NATIVE_PROVIDERS = [
  'posthog', 'hubspot', 'customerio', 'attio', 'segment', 'amplitude', 'ga4', 'mixpanel', 'rudderstack', 'brevo',
  'activecampaign', 'mailchimp', 'salesforce', 'pipedrive', 'beehiiv',
];

test('every native adapter in the back-end registry is listed as built, never live', () => {
  assert.equal(NATIVE_PROVIDERS.length, 15);
  for (const id of NATIVE_PROVIDERS) {
    const item = byId[id];
    assert.ok(item, id);
    assert.equal(item.status, 'built', id);
    assert.equal(STATUS[item.status].label, 'Built · mock-tested', id);
    const text = `${item.summary} ${item.details.join(' ')}`;
    assert.doesNotMatch(text, /\b(connected|live|production[- ]?(ready|tested|validated)|verified with)\b/i, id);
  }
  const nonHosting = INTEGRATIONS.filter((i) => i.status === 'built' && !['hosting', 'automation'].includes(i.category)).map((i) => i.id);
  assert.deepEqual(nonHosting.sort(), [...NATIVE_PROVIDERS].sort());
});

// Mirrors PRESETS in webinars/webhook_presets.py and the recipes in sdk/kits/automation/.
test('workflow-automation presets are built (preset + recipe), simulation-only, never live', () => {
  const automation = INTEGRATIONS.filter((i) => i.category === 'automation').map((i) => i.id).sort();
  assert.deepEqual(automation, ['make', 'n8n', 'zapier']);
  for (const id of automation) {
    assert.equal(byId[id].status, 'built', id);
    assert.ok(byId[id].details.some((d) => /destination preset/i.test(d)), `${id}: preset`);
    assert.ok(byId[id].details.some((d) => /verification recipe/i.test(d)), `${id}: recipe`);
    assert.ok(byId[id].details.some((d) => /node simulation.*unverified with a real/i.test(d)), `${id}: honest limit`);
    assert.ok(byId[id].details.some((d) => /delivery id/i.test(d)), `${id}: dedupe`);
  }
  assert.ok(byId.n8n.details.some((d) => /never \/webhook-test\//.test(d)));
  assert.ok(byId.make.details.some((d) => /eu1, eu2, us1, us2/.test(d)));
  assert.ok(byId.zapier.details.some((d) => /hooks\.zapier\.com/.test(d)));
  const manifest = JSON.parse(read('..', 'sdk', 'kits', 'manifest.json'));
  for (const id of automation) {
    const kit = manifest.kits.find((k: { id: string }) => k.id === id);
    assert.ok(kit && kit.productionTested === false, `${id}: manifest kit`);
  }
});

test('built adapters state their privacy limits', () => {
  // ANALYTICS_PROVIDERS in webinars/native_integrations.py; Segment's consents are the operator's choice.
  for (const id of ['posthog', 'amplitude', 'ga4', 'mixpanel', 'rudderstack']) {
    assert.ok(byId[id].details.some((d) => /requires analytics consent/i.test(d)), id);
  }
  assert.ok(!byId.segment.details.some((d) => /requires analytics consent/i.test(d)));
  assert.ok(byId.mixpanel.details.some((d) => /no profile data/i.test(d)));
  assert.ok(byId.rudderstack.details.some((d) => /own data plane host.*allowlist/i.test(d)));
  assert.ok(byId.brevo.details.some((d) => /never changes lists, subscriptions or blocklist/i.test(d)));
  assert.ok(byId.brevo.details.some((d) => /no urls are sent/i.test(d)));
  assert.ok(byId.activecampaign.details.some((d) => /never adds contacts to lists or automations/i.test(d)));
  assert.ok(byId.activecampaign.details.some((d) => /never phone.*no urls are sent/i.test(d)));
  assert.ok(byId.activecampaign.details.some((d) => /operator allowlist exactly/i.test(d)));
  assert.ok(byId.mailchimp.details.some((d) => /transactional \(not marketable\).*never subscribed/i.test(d)));
  assert.ok(byId.mailchimp.details.some((d) => /never changes an existing member/i.test(d)));
  assert.ok(byId.mailchimp.details.some((d) => /may repeat on a delivery retry/i.test(d)));
  assert.ok(byId.salesforce.details.some((d) => /own my domain host.*allowlist/i.test(d)));
  assert.ok(byId.salesforce.details.some((d) => /unverified/i.test(d)));
  assert.ok(byId.pipedrive.details.some((d) => /never changes marketing status/i.test(d)));
  assert.ok(byId.beehiiv.details.some((d) => /this one subscribes people.*subscription acknowledgement/i.test(d)));
  assert.ok(byId.beehiiv.details.some((d) => /never reactivates someone who unsubscribed/i.test(d)));
  assert.ok(byId.beehiiv.details.some((d) => /existing-email handling is unverified/i.test(d)));
});

test('hosting statuses match what exists', () => {
  assert.deepEqual(idsWith('limited'), ['riverside', 'streamyard']);
  assert.ok(byId.riverside.details.some((d) => /attach existing events only/i.test(d)));
  assert.ok(byId.riverside.details.some((d) => /per-registrant.*unverified/i.test(d)));
  assert.ok(byId.streamyard.details.some((d) => /manual link only/i.test(d)));
  for (const id of ['zoom', 'wistia', 'sequel']) {
    assert.ok(byId[id].details.some((d) => /unverified with a real/i.test(d)), id);
  }
});

test('framework and CMS kits are reference code, never built', () => {
  const kits = INTEGRATIONS.filter((i) => i.category === 'frameworks');
  assert.equal(kits.length, 11);
  assert.equal(byId.wordpress.status, 'plugin-untested');
  assert.ok(byId.wordpress.details.some((d) => /runtime untested/i.test(d)));
  for (const k of kits) {
    if (k.id !== 'wordpress') assert.equal(k.status, 'reference', k.id);
    assert.ok(k.details.includes('Not production-tested.'), k.id);
  }
});

test('roadmap providers are listed and not built', () => {
  for (const id of ['marketo', 'hubspot-marketing', 'loops', 'intercom', 'resend', 'postmark', 'ses']) {
    assert.equal(INTEGRATIONS.find((i) => i.id === id)?.status, 'roadmap', id);
  }
  assert.deepEqual(idsWith('roadmap'), ['hubspot-marketing', 'intercom', 'loops', 'marketo', 'postmark', 'resend', 'ses']);
});

test('ids are unique and every category is valid and non-empty', () => {
  assert.equal(new Set(INTEGRATIONS.map((i) => i.id)).size, INTEGRATIONS.length);
  const counts = countByCategory();
  for (const c of CATEGORIES) assert.ok(counts[c.slug] > 0, c.slug);
  assert.equal(
    Object.values(counts).reduce((a, b) => a + b, 0),
    INTEGRATIONS.length,
  );
});

test('category filter accepts only known slugs', () => {
  assert.equal(parseCategory('crm'), 'crm');
  assert.equal(parseCategory('CRM'), undefined);
  assert.equal(parseCategory('<script>'), undefined);
  assert.equal(parseCategory(['crm', 'hosting']), undefined);
  assert.equal(parseCategory(undefined), undefined);
  assert.ok(listIntegrations('crm').every((i) => i.category === 'crm'));
  assert.equal(listIntegrations().length, INTEGRATIONS.length);
  assert.equal(listIntegrations('crm')[0].status, 'built');
});

test('catalogue markup is accessible', () => {
  const page = read('app', 'integrations', 'page.tsx');
  assert.match(page, /<nav className="category-tabs" aria-label="[^"]+"/);
  assert.match(page, /aria-current=\{t\.slug === category \? 'page' : undefined\}/);
  assert.match(page, /<article className="integration-card" aria-labelledby=\{headingId\}>/);
  assert.match(page, /<H id=\{headingId\}>/);
  assert.match(page, /className="visually-hidden">Status: </);
  assert.match(page, /<dl className="status-legend">/);
  assert.match(page, /<h1 id="integrations-title">/);
  // Plain links, no client JS needed to filter.
  assert.doesNotMatch(page, /['"]use client['"]/);
});

test('integrations and features are linked from the header and footer', () => {
  assert.match(read('components', 'SiteHeader.tsx'), /<NavLink href="\/integrations">Integrations<\/NavLink>/);
  assert.match(read('components', 'SiteFooter.tsx'), /<Link href="\/integrations">Integrations<\/Link>/);
  assert.match(read('components', 'SiteHeader.tsx'), /<NavLink href="\/features">Features<\/NavLink>/);
  assert.match(read('components', 'SiteFooter.tsx'), /<Link href="\/features">/);
});

test('features page explains tickets and provisioning without going live', () => {
  const page = read('app', 'features', 'page.tsx');
  const data = read('lib', 'features.ts');
  for (const phrase of ['Stripe hosted checkout', 'Verified email before a paid hold', 'Atomic headcount limits', 'Show or hide openings']) {
    assert.ok(data.includes(phrase), phrase);
  }
  assert.match(page, /Not live on this demo/);
  assert.match(page, /No checkout is configured/);
  assert.match(page, /older version/);
  // Static explanation only: no fetches, no client JS, no backend or proxy use, no keys.
  for (const src of [page, data]) {
    assert.doesNotMatch(src, /fetch\(|['"]use client['"]|process\.env|@\/lib\/(backend|proxy)|<form\b/);
    assert.doesNotMatch(src, /checkout\.stripe\.com|sk_(live|test)|pk_(live|test)/);
  }
});

test('hosting capability table is honest per platform', () => {
  const rows = Object.fromEntries(HOSTING_CAPABILITIES.map((r) => [r.provider, r]));
  assert.deepEqual(Object.keys(rows).sort(), ['Riverside', 'Sequel', 'StreamYard', 'Wistia', 'Zoom']);
  assert.equal(rows.Zoom.update, 'yes');
  assert.equal(rows.Wistia.update, 'no');
  assert.equal(rows.Riverside.create, 'no');
  assert.equal(rows.Riverside.perRegistrantLinks, 'unverified');
  for (const k of ['create', 'update', 'attach', 'perRegistrantLinks'] as const) assert.equal(rows.StreamYard[k], 'no');
  assert.equal(rows.StreamYard.manualLink, 'yes');
  for (const r of HOSTING_CAPABILITIES) assert.doesNotMatch(r.note, /\b(connected|live)\b/i, r.provider);
  assert.ok(HOSTING_LIMITATIONS.some((l) => /never|confirm/i.test(l)));
});

test('demo event views gain no paid, sold-out or countdown UI', () => {
  for (const rel of [['components', 'EventCard.tsx'], ['components', 'RsvpPanel.tsx'], ['app', 'events', '[slug]', 'page.tsx']]) {
    assert.doesNotMatch(read(...rel), /sold.?out|countdown|seats? left|checkout|stripe/i, rel.join('/'));
  }
});

test('the integrations page explains principles without changing the hero', () => {
  const page = read('app', 'integrations', 'page.tsx');
  for (const phrase of ['One source of truth', 'no lock-in', 'Credentials stay private', 'No live keys in this demo']) {
    assert.ok(page.includes(phrase), phrase);
  }
  const home = read('app', 'page.tsx');
  assert.ok(home.includes('Your webinars. Your infrastructure. <em>No vendor lock-in.</em>'));
  assert.ok(home.includes('VisionCast is a headless CMS for deploying your virtual events and webinars using your own infrastructure'));
});
