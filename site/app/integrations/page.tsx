import type { Metadata } from 'next';
import Link from 'next/link';

import {
  BACKEND_TESTS,
  CATEGORIES,
  STATUS,
  categoryLabel,
  countByCategory,
  listIntegrations,
  parseCategory,
  type CategorySlug,
  type Integration,
  type Status,
} from '@/lib/integrations';

export const metadata: Metadata = {
  title: 'Integrations',
  description:
    'What VisionCast connects to today, what is being built, and what is planned. Nothing in this demo is connected to a live account.',
};

type Search = Record<string, string | string[] | undefined>;

const PRINCIPLES = [
  {
    title: 'One source of truth',
    body: 'Events, sessions, forms and registrations live in VisionCast. Every integration reads the same records through the same outbox, so each tool receives the same facts.',
  },
  {
    title: 'Your infrastructure, no lock-in',
    body: 'VisionCast runs on servers you control. Integrations are adapters over open APIs and signed webhooks, so you can change tools without moving your event data.',
  },
  {
    title: 'Credentials stay private',
    body: 'Provider secrets are stored encrypted on your back end, read only when a message is sent, and never logged or returned to a browser. Only verified registrations with the required consent are sent.',
  },
  {
    title: 'No live keys in this demo',
    body: 'This site holds no provider API keys and the demo seed creates no integration destinations. Nothing listed here is connected to a live account.',
  },
];

const STATUSES = Object.keys(STATUS) as Status[];

function hrefFor(category?: CategorySlug) {
  return category ? `/integrations?category=${category}` : '/integrations';
}

function IntegrationCard({ item, headingLevel }: { item: Integration; headingLevel: 2 | 3 }) {
  const H = headingLevel === 2 ? 'h2' : 'h3';
  const headingId = `integration-${item.id}`;
  return (
    <article className="integration-card" aria-labelledby={headingId}>
      <div className="integration-card-head">
        <H id={headingId}>{item.name}</H>
        <span className={`pill status-${item.status}`}>
          <span className="visually-hidden">Status: </span>
          {STATUS[item.status].label}
        </span>
      </div>
      <p className="card-meta">{categoryLabel(item.category)}</p>
      <p>{item.summary}</p>
      {item.details.length > 0 && (
        <ul className="integration-details">
          {item.details.map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
      )}
    </article>
  );
}

function CardList({ items, headingLevel }: { items: Integration[]; headingLevel: 2 | 3 }) {
  return (
    <ul className="integration-grid">
      {items.map((item) => (
        <li key={item.id}>
          <IntegrationCard item={item} headingLevel={headingLevel} />
        </li>
      ))}
    </ul>
  );
}

export default async function IntegrationsCatalogue({ searchParams }: { searchParams: Promise<Search> }) {
  const category = parseCategory((await searchParams).category);
  const counts = countByCategory();
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const tabs = [{ slug: undefined, label: 'All', count: total }, ...CATEGORIES.map((c) => ({ ...c, count: counts[c.slug] }))];

  return (
    <>
      <section className="page-head dark" aria-labelledby="integrations-title">
        <div className="wrap">
          <p className="eyebrow">Integrations catalogue</p>
          <h1 id="integrations-title">{category ? categoryLabel(category) : 'Integrations'}</h1>
          <p className="lead">
            What VisionCast connects to today, what is being built, and what is planned. Every status below is stated
            conservatively: nothing on this page is connected to a live account.
          </p>
          <p>
            The back-end suite passes {BACKEND_TESTS.passed.toLocaleString('en-GB')} tests on {BACKEND_TESTS.database}.{' '}
            {BACKEND_TESTS.note} This demo site still runs against an older back-end version. See{' '}
            <Link href="/features#hosting">what each hosting platform supports</Link>.
          </p>

          <nav className="category-tabs" aria-label="Filter integrations by category">
            <ul>
              {tabs.map((t) => (
                <li key={t.slug ?? 'all'}>
                  <Link href={hrefFor(t.slug)} aria-current={t.slug === category ? 'page' : undefined}>
                    {t.label} <span className="count">{t.count}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </section>

      <div className="wrap">
        <section className="section" aria-labelledby="status-title">
          <h2 id="status-title" className="visually-hidden">
            What each status means
          </h2>
          <dl className="status-legend">
            {STATUSES.map((s) => (
              <div key={s}>
                <dt>
                  <span className={`pill status-${s}`}>{STATUS[s].label}</span>
                </dt>
                <dd>{STATUS[s].explain}</dd>
              </div>
            ))}
          </dl>
        </section>

        {category ? (
          <div className="section">
            <div className="results-bar">
              <p style={{ margin: 0 }}>
                {counts[category] === 1 ? '1 integration' : `${counts[category]} integrations`} in this category
              </p>
              <Link href="/integrations">Show all categories</Link>
            </div>
            <CardList items={listIntegrations(category)} headingLevel={2} />
          </div>
        ) : (
          CATEGORIES.map((c) => (
            <section className="section" key={c.slug} aria-labelledby={`category-${c.slug}`}>
              <div className="section-head">
                <h2 id={`category-${c.slug}`}>{c.label}</h2>
                <Link href={hrefFor(c.slug)}>Only {c.label.toLowerCase()} →</Link>
              </div>
              <CardList items={listIntegrations(c.slug)} headingLevel={3} />
            </section>
          ))
        )}

        <section className="section" id="principles" aria-labelledby="principles-title">
          <div className="section-head">
            <h2 id="principles-title">How integrations work</h2>
          </div>
          <ul className="feature-grid">
            {PRINCIPLES.map((p, i) => (
              <li key={p.title}>
                <span className="num" aria-hidden="true">
                  {String(i + 1).padStart(2, '0')}
                </span>
                <h3>{p.title}</h3>
                <p>{p.body}</p>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </>
  );
}
