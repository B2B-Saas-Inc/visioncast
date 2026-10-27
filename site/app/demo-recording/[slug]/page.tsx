import type { Metadata } from 'next';
import Link from 'next/link';
import { notFound } from 'next/navigation';

import { BackendUnavailable } from '@/components/BackendUnavailable';
import { getEvent } from '@/lib/backend';
import { formatWhen, type EventItem, type Occurrence } from '@/lib/event-model';

/*
 * Target of the demo seed's recording links (seed_public_demo.py RECORDING_PAGE_BASE).
 * The seeded past events are fictional, so there is nothing to play: this page says so
 * plainly and embeds no player, video or stream. It renders only for an event whose
 * published recording link points here; any other slug is a 404.
 */

export const dynamic = 'force-dynamic';

type Params = Promise<{ slug: string }>;

function demoRecordings(event: EventItem): Occurrence[] {
  const path = `/demo-recording/${event.slug}`;
  return event.occurrences.filter((o) => o.recordingUrl === path);
}

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { slug } = await params;
  const res = await getEvent(slug);
  const title = res.ok && demoRecordings(res.data).length ? `Demo recording page: ${res.data.title}` : 'Demo recording page';
  return { title, robots: { index: false, follow: true } };
}

export default async function DemoRecordingPage({ params }: { params: Params }) {
  const { slug } = await params;
  const res = await getEvent(slug);
  if (!res.ok && res.reason === 'not_found') notFound();
  if (!res.ok) {
    return (
      <div className="wrap" style={{ paddingTop: '3rem' }}>
        <p className="breadcrumb">
          <Link href="/events">← All events</Link>
        </p>
        <BackendUnavailable reason={res.reason} what="event details" />
      </div>
    );
  }
  const event = res.data;
  const sessions = demoRecordings(event);
  if (!sessions.length) notFound();
  const eventHref = `/events/${encodeURIComponent(event.slug)}`;

  return (
    <article>
      <header className="page-head dark">
        <div className="wrap">
          <p className="breadcrumb">
            <Link href={eventHref}>← Back to the event</Link>
          </p>
          <p className="eyebrow">Demo recording page</p>
          <h1>{event.title}</h1>
          <p className="lead">There is no recording to watch. This is a fictional demo event and the session never took place.</p>
        </div>
      </header>

      <div className="wrap" style={{ paddingTop: '2.5rem', paddingBottom: '3rem' }}>
        <div className="notice" role="note">
          <h2>Why this page exists</h2>
          <p>
            The Visioncast demo catalogue includes past events so you can see how a finished session with a published
            recording link looks. That link leads here. No video is hosted, streamed or embedded for this event, and
            none will appear later.
          </p>
          <p>
            The hosts, company and session details are made up for the demo. <Link href={eventHref}>The event page</Link>{' '}
            shows the full description, and <Link href="/events?when=upcoming">upcoming demo events</Link> show the
            registration flow.
          </p>
        </div>

        <section className="aida-section" aria-labelledby="listed">
          <h2 id="listed">{sessions.length === 1 ? 'Listed session' : 'Listed sessions'}</h2>
          <ul>
            {sessions.map((o) => {
              const w = formatWhen(o);
              return (
                <li key={o.id}>
                  {w.date} · {w.time} {w.zone} <span className="tag">fictional</span>
                </li>
              );
            })}
          </ul>
        </section>

        {event.learningOutcomes.length > 0 && (
          <section className="aida-section" aria-labelledby="outline">
            <h2 id="outline">What the session description covers</h2>
            <ol className="outcomes">
              {event.learningOutcomes.map((o, i) => (
                <li key={i}>{o}</li>
              ))}
            </ol>
          </section>
        )}

        <p>
          <Link className="button" href="/events">
            Browse events
          </Link>
        </p>
      </div>
    </article>
  );
}
