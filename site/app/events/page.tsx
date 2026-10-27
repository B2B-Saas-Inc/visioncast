import type { Metadata } from 'next';
import Link from 'next/link';

import { BackendUnavailable } from '@/components/BackendUnavailable';
import { EventCard } from '@/components/EventCard';
import { listEvents, type ListParams } from '@/lib/backend';
import { collectTopics, sortForDisplay } from '@/lib/event-model';

export const dynamic = 'force-dynamic';

export const metadata: Metadata = {
  title: 'Events',
  description: 'Upcoming Visioncast sessions and published recordings.',
};

const PAGE_SIZE = 12;
const WHEN = [
  { value: '', label: 'All' },
  { value: 'upcoming', label: 'Upcoming' },
  { value: 'recorded', label: 'Recordings' },
] as const;

type Search = Record<string, string | string[] | undefined>;
type When = NonNullable<ListParams['when']>;

interface Filters {
  q: string;
  topic: string;
  when?: When;
  page: number;
}

function one(value: string | string[] | undefined): string {
  return typeof value === 'string' ? value : '';
}

function isWhen(value: string): value is When {
  return value === 'upcoming' || value === 'recorded';
}

/** Query parameters, validated to what the backend accepts; anything else is dropped. */
function parseFilters(sp: Search): Filters {
  const q = one(sp.q).trim().slice(0, 200);
  const topicRaw = one(sp.topic);
  const topic = /^[A-Za-z0-9_-]{1,200}$/.test(topicRaw) ? topicRaw : '';
  const whenRaw = one(sp.when);
  const when = isWhen(whenRaw) ? whenRaw : undefined;
  const pageRaw = Number(one(sp.page) || '1');
  const page = Number.isSafeInteger(pageRaw) && pageRaw >= 1 && pageRaw <= 1000 ? pageRaw : 1;
  return { q, topic, when, page };
}

function hrefFor(f: Filters) {
  const qs = new URLSearchParams();
  if (f.q) qs.set('q', f.q);
  if (f.topic) qs.set('topic', f.topic);
  if (f.when) qs.set('when', f.when);
  if (f.page > 1) qs.set('page', String(f.page));
  const s = qs.toString();
  return s ? `/events?${s}` : '/events';
}

export default async function EventsIndex({ searchParams }: { searchParams: Promise<Search> }) {
  const filters = parseFilters(await searchParams);
  const [results, facets] = await Promise.all([
    listEvents({
      q: filters.q || undefined,
      topic: filters.topic || undefined,
      when: filters.when,
      limit: PAGE_SIZE,
      offset: (filters.page - 1) * PAGE_SIZE,
    }),
    // Topic options come from the catalogue itself (first 50 public events).
    listEvents({ limit: 50 }),
  ]);
  const topics = facets.ok ? collectTopics(facets.data.results) : [];
  const now = Date.now();
  const filtered = Boolean(filters.q || filters.topic || filters.when);
  const heading =
    filters.when === 'upcoming' ? 'Upcoming sessions' : filters.when === 'recorded' ? 'Recordings' : 'All events';

  return (
    <>
      <section className="page-head dark" aria-labelledby="events-title">
        <div className="wrap">
          <p className="eyebrow">Event index</p>
          <h1 id="events-title">{heading}</h1>
          <p className="lead">Live sessions you can join, and recordings from the ones that already happened.</p>

          <form className="filters" method="get" action="/events" role="search" aria-label="Filter events">
            <div>
              <label htmlFor="q">Search titles and summaries</label>
              <input id="q" name="q" type="search" defaultValue={filters.q} maxLength={200} autoComplete="off" />
            </div>
            <div>
              <label htmlFor="topic">Topic</label>
              <select id="topic" name="topic" defaultValue={filters.topic}>
                <option value="">All topics</option>
                {topics.map((t) => (
                  <option key={t.slug} value={t.slug}>
                    {t.name}
                  </option>
                ))}
                {filters.topic && !topics.some((t) => t.slug === filters.topic) && (
                  <option value={filters.topic}>{filters.topic}</option>
                )}
              </select>
            </div>
            <fieldset>
              <legend>Show</legend>
              <div className="segmented">
                {WHEN.map((w) => (
                  <span key={w.value || 'all'}>
                    <input
                      type="radio"
                      id={`when-${w.value || 'all'}`}
                      name="when"
                      value={w.value}
                      defaultChecked={(filters.when ?? '') === w.value}
                    />
                    <label htmlFor={`when-${w.value || 'all'}`}>{w.label}</label>
                  </span>
                ))}
              </div>
            </fieldset>
            <div>
              <button className="button" type="submit">
                Apply
              </button>
            </div>
          </form>
        </div>
      </section>

      <div className="wrap">
        {results.ok ? (
          <>
            <div className="results-bar">
              <p role="status" style={{ margin: 0 }}>
                {results.data.count === 1 ? '1 event' : `${results.data.count} events`}
                {filtered ? ' match these filters' : ''}
              </p>
              {filtered && <Link href="/events">Clear filters</Link>}
            </div>
            {results.data.results.length ? (
              <ul className="card-grid">
                {sortForDisplay(results.data.results, now).map((e) => (
                  <li key={e.slug}>
                    <EventCard event={e} now={now} headingLevel={2} />
                  </li>
                ))}
              </ul>
            ) : (
              <div className="notice">
                <h2>No events match</h2>
                <p>
                  Try a different search, or <Link href="/events">see every event</Link>.
                </p>
              </div>
            )}
            {results.data.count > PAGE_SIZE && (
              <nav className="pagination" aria-label="Pagination">
                {filters.page > 1 ? (
                  <Link href={hrefFor({ ...filters, page: filters.page - 1 })} rel="prev">
                    ← Previous page
                  </Link>
                ) : (
                  <span />
                )}
                <span>
                  Page {filters.page} of {Math.ceil(results.data.count / PAGE_SIZE)}
                </span>
                {filters.page * PAGE_SIZE < results.data.count ? (
                  <Link href={hrefFor({ ...filters, page: filters.page + 1 })} rel="next">
                    Next page →
                  </Link>
                ) : (
                  <span />
                )}
              </nav>
            )}
          </>
        ) : (
          <div style={{ marginTop: '2.5rem' }}>
            <BackendUnavailable reason={results.reason} />
          </div>
        )}
      </div>
    </>
  );
}
