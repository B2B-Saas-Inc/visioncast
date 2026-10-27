import Link from 'next/link';

import { artPath, eventState, formatWhen, latestRecording, nextOccurrence, type EventItem } from '@/lib/event-model';

const STATE_LABEL = { upcoming: 'Upcoming', recording: 'Recording', past: 'Past event' } as const;

export function EventCard({ event, now, headingLevel = 3 }: { event: EventItem; now: number; headingLevel?: 2 | 3 }) {
  const state = eventState(event, now);
  const occ = nextOccurrence(event, now) ?? latestRecording(event) ?? event.occurrences[event.occurrences.length - 1] ?? null;
  const when = occ ? formatWhen(occ) : null;
  const Heading = headingLevel === 2 ? 'h2' : 'h3';
  return (
    <article className="card">
      <div className="card-art">
        {/* Decorative: the title carries the meaning. */}
        <img src={artPath(event)} alt="" width={1600} height={900} loading="lazy" decoding="async" />
      </div>
      <div className="card-body">
        <span className={`pill ${state}`}>{STATE_LABEL[state]}</span>
        <Heading>
          <Link href={`/events/${encodeURIComponent(event.slug)}`}>{event.title}</Link>
        </Heading>
        {when && (
          <p className="card-meta">
            {when.date} · {when.time} {when.zone}
          </p>
        )}
        {event.summary && <p>{event.summary}</p>}
        {(event.topics.length > 0 || event.hosts.length > 0) && (
          <div className="card-foot">
            {event.topics.slice(0, 3).map((t) => (
              <span className="tag" key={t.slug}>
                {t.name}
              </span>
            ))}
            {event.hosts.length > 0 && (
              <span className="tag">
                <span className="visually-hidden">Hosted by </span>
                {event.hosts.map((h) => h.name).join(', ')}
              </span>
            )}
          </div>
        )}
      </div>
    </article>
  );
}
