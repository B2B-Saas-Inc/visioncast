import type { Metadata } from 'next';
import Link from 'next/link';
import { notFound } from 'next/navigation';

import { BackendUnavailable } from '@/components/BackendUnavailable';
import { RsvpPanel, type RsvpOccurrence } from '@/components/RsvpPanel';
import { getEvent, getFormSchema } from '@/lib/backend';
import {
  artPath,
  eventState,
  formatWhen,
  isLocalPath,
  isUpcoming,
  latestRecording,
  nextOccurrence,
  openOccurrences,
  paragraphs,
  type EventItem,
  type Occurrence,
} from '@/lib/event-model';

export const dynamic = 'force-dynamic';

type Params = Promise<{ slug: string }>;

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { slug } = await params;
  const res = await getEvent(slug);
  if (!res.ok) return { title: res.reason === 'not_found' ? 'Event not found' : 'Event' };
  return { title: res.data.title, description: res.data.summary.slice(0, 300) || undefined };
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join('');
}

function Prose({ text }: { text: string }) {
  return (
    <div className="body">
      {paragraphs(text).map((p, i) => (
        <p key={i}>{p}</p>
      ))}
    </div>
  );
}

function durationLabel(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

/** The seeded demo page stays in this tab; a real external recording opens a new one. */
function RecordingLink({ url, className }: { url: string; className?: string }) {
  if (isLocalPath(url)) {
    return (
      <Link className={className} href={url}>
        Open the demo recording page
      </Link>
    );
  }
  return (
    <a className={className} href={url} rel="noopener noreferrer" target="_blank">
      Watch the recording<span className="visually-hidden"> (opens in a new tab)</span>
    </a>
  );
}

function SessionStatus({ occ, now }: { occ: Occurrence; now: number }) {
  if (occ.status === 'cancelled') return <span className="pill cancelled">Cancelled</span>;
  if (occ.registrationOpen) return <span className="pill upcoming">Open for registration</span>;
  if (isUpcoming(occ, now)) {
    return Date.parse(occ.startsAt) <= now ? <span className="pill">Live now</span> : <span className="pill">Registration closed</span>;
  }
  if (occ.recordingUrl) return <span className="pill recording">Recording</span>;
  return <span className="pill past">Ended</span>;
}

function Sessions({ event, now }: { event: EventItem; now: number }) {
  return (
    <ul className="sessions">
      {event.occurrences.map((occ) => {
        const w = formatWhen(occ);
        const showCalendar = isUpcoming(occ, now);
        return (
          <li className="session" key={occ.id}>
            <div>
              <SessionStatus occ={occ} now={now} />
              <p className="session-when" style={{ margin: '0.5rem 0 0' }}>
                {w.date} · {w.time} {w.zone}
              </p>
            </div>
            <div className="session-links">
              {occ.recordingUrl && <RecordingLink url={occ.recordingUrl} />}
              {showCalendar && occ.icsUrl && (
                <a href={occ.icsUrl} download>
                  Calendar file (.ics)
                </a>
              )}
              {showCalendar && occ.googleUrl && (
                <a href={occ.googleUrl} rel="noopener noreferrer" target="_blank">
                  Google Calendar<span className="visually-hidden"> (opens in a new tab)</span>
                </a>
              )}
              {showCalendar && occ.outlookUrl && (
                <a href={occ.outlookUrl} rel="noopener noreferrer" target="_blank">
                  Outlook.com<span className="visually-hidden"> (opens in a new tab)</span>
                </a>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

export default async function EventPage({ params }: { params: Params }) {
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
  const now = Date.now();
  const state = eventState(event, now);
  const next = nextOccurrence(event, now);
  const headline = next ?? latestRecording(event) ?? event.occurrences[event.occurrences.length - 1] ?? null;
  const headlineWhen = headline ? formatWhen(headline) : null;
  const open = openOccurrences(event);
  const schema = open.length && event.formVersion ? await getFormSchema(event.formVersion) : null;
  const rsvpOccurrences: RsvpOccurrence[] = open.map((o) => {
    const w = formatWhen(o);
    return { id: o.id, label: `${w.date} · ${w.time} ${w.zone}` };
  });
  const attention = event.aida.attention.trim();
  const recording = latestRecording(event);

  return (
    <article>
      <header className="event-hero dark">
        <div className="wrap">
          <p className="breadcrumb">
            <Link href="/events">← All events</Link>
          </p>
          <div className="event-hero-grid">
            <div>
              <p className="eyebrow">
                {event.topics.map((t) => t.name).join(' · ') || 'Webinar'}
                {state === 'recording' ? ' · Recording available' : ''}
              </p>
              <h1>{event.title}</h1>
              {attention && attention !== event.title && <p className="lead">{attention}</p>}
              {event.summary && <p>{event.summary}</p>}
              {headlineWhen && (
                <dl className="session-strip">
                  <div>
                    <dt>{next ? 'Next session' : 'Took place'}</dt>
                    <dd>{headlineWhen.date}</dd>
                  </div>
                  <div>
                    <dt>Time</dt>
                    <dd>
                      {headlineWhen.time} {headlineWhen.zone}
                    </dd>
                  </div>
                  <div>
                    <dt>Length</dt>
                    <dd>{durationLabel(headlineWhen.minutes)}</dd>
                  </div>
                  {event.hosts.length > 0 && (
                    <div>
                      <dt>{event.hosts.length === 1 ? 'Host' : 'Hosts'}</dt>
                      <dd>{event.hosts.map((h) => h.name).join(', ')}</dd>
                    </div>
                  )}
                </dl>
              )}
            </div>
            <div className="event-hero-art">
              <img src={artPath(event)} alt="" width={1600} height={900} />
            </div>
          </div>
        </div>
      </header>

      <div className="wrap event-layout">
        <div>
          {event.aida.interest.trim() && (
            <section className="aida-section" aria-labelledby="why">
              <p className="eyebrow">Why it matters</p>
              <h2 id="why">What this session is about</h2>
              <Prose text={event.aida.interest} />
            </section>
          )}

          {event.learningOutcomes.length > 0 && (
            <section className="aida-section" aria-labelledby="outcomes">
              <p className="eyebrow">What you will take away</p>
              <h2 id="outcomes">By the end, you will be able to</h2>
              <ol className="outcomes">
                {event.learningOutcomes.map((o, i) => (
                  <li key={i}>{o}</li>
                ))}
              </ol>
            </section>
          )}

          {(event.aida.decision.trim() || event.hosts.length > 0) && (
            <section className="aida-section" aria-labelledby="fit">
              <p className="eyebrow">Is it for you</p>
              <h2 id="fit">Who should attend</h2>
              {event.aida.decision.trim() && <Prose text={event.aida.decision} />}
              {event.hosts.length > 0 && (
                <>
                  <h3 style={{ marginTop: '1.8rem' }}>{event.hosts.length === 1 ? 'Your host' : 'Your hosts'}</h3>
                  <ul className="hosts">
                    {event.hosts.map((h) => (
                      <li className="host" key={h.slug || h.name}>
                        <span className="monogram" aria-hidden="true">
                          {initials(h.name)}
                        </span>
                        <div>
                          <strong>{h.name}</strong>
                          {h.title && <span>{h.title}</span>}
                        </div>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </section>
          )}

          <section className="aida-section" aria-labelledby="sessions">
            <p className="eyebrow">Schedule</p>
            <h2 id="sessions">{event.occurrences.length === 1 ? 'Session' : 'Sessions'}</h2>
            {event.occurrences.length ? <Sessions event={event} now={now} /> : <p>No sessions are scheduled.</p>}
          </section>
        </div>

        <aside className="rsvp-column" aria-label="Registration">
          {open.length === 0 ? (
            <div className="rsvp dark">
              <p className="eyebrow">{recording ? 'Recording' : 'Registration'}</p>
              <h2>
                {recording
                  ? isLocalPath(recording.recordingUrl!)
                    ? 'No recording for this demo'
                    : 'Catch up on the recording'
                  : 'Registration is closed'}
              </h2>
              {recording ? (
                isLocalPath(recording.recordingUrl!) ? (
                  <>
                    <p>This demo session is marked as finished. Its recording page explains that no actual recording exists.</p>
                    <RecordingLink className="button" url={recording.recordingUrl!} />
                  </>
                ) : (
                  <>
                    <p>This session has finished. The recording is available to watch now.</p>
                    <RecordingLink className="button" url={recording.recordingUrl!} />
                  </>
                )
              ) : (
                <p>There is no session open for registration right now.</p>
              )}
              <p style={{ marginTop: '1.2rem' }}>
                <Link href="/events?when=upcoming">See upcoming events →</Link>
              </p>
            </div>
          ) : schema && schema.ok ? (
            <RsvpPanel
              eventTitle={event.title}
              formVersion={event.formVersion}
              occurrences={rsvpOccurrences}
              schema={schema.data}
              cta={event.cta}
              actionCopy={event.aida.action}
            />
          ) : (
            <div className="rsvp dark">
              <h2>Registration is unavailable</h2>
              <p>
                {schema && !schema.ok && schema.reason === 'not_found'
                  ? 'This event’s registration form could not be found.'
                  : 'The registration form could not be loaded from the Visioncast back end. Please try again shortly.'}
              </p>
            </div>
          )}
        </aside>
      </div>
    </article>
  );
}
