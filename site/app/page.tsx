import Link from 'next/link';

import { BackendUnavailable } from '@/components/BackendUnavailable';
import { EventCard } from '@/components/EventCard';
import { listEvents } from '@/lib/backend';
import { sortForDisplay } from '@/lib/event-model';

export const dynamic = 'force-dynamic';

const FEATURES = [
  {
    title: 'Pages written to persuade',
    body: 'Every event carries Attention, Interest, Decision and Action copy alongside learning outcomes, hosts and topics. Publishing is refused until the event has an upcoming session, Attention copy and an Action.',
  },
  {
    title: 'Registration that survives retries',
    body: 'Each submission carries an idempotency key. Retrying after a network failure replays the stored result instead of creating a second registration or a second webhook.',
  },
  {
    title: 'Recognition without accounts',
    body: 'An HttpOnly capability cookie lets a returning browser skip fields it already gave. An email address alone never reveals or updates an existing profile.',
  },
  {
    title: 'Calendars that follow changes',
    body: 'ICS files keep a stable UID and bump SEQUENCE on reschedule or cancellation, so attendees’ calendars update the entry instead of duplicating it.',
  },
  {
    title: 'Forms from one schema',
    body: 'Fields, labels, options and SMS disclosures come from a versioned form schema served by the API. This site renders that schema; it hard-codes no fields.',
  },
  {
    title: 'APIs, webhooks and MCP',
    body: 'A public catalogue API, a scoped management API, signed webhooks delivered from a transactional outbox, and an MCP endpoint for agents.',
  },
];

export default async function Home() {
  const upcoming = await listEvents({ when: 'upcoming', limit: 3 });
  const now = Date.now();
  return (
    <>
      <section className="hero dark" aria-labelledby="hero-title">
        <div className="wrap hero-grid">
          <div>
            <p className="eyebrow">VisionCast · Headless CMS for virtual events</p>
            <h1 id="hero-title">
              Your webinars. Your infrastructure. <em>No vendor lock-in.</em>
            </h1>
            <p className="lead">
              VisionCast is a headless CMS for deploying your virtual events and webinars using your own infrastructure
              and without vendor lock-in. Whether you’re starting small or have extensive experience, VisionCast makes
              it easy to use virtual events to grow your company.
            </p>
            <div className="hero-actions">
              <Link className="button" href="/events">
                Browse events
              </Link>
              <Link className="button secondary" href="#how-it-works">
                How this demo works
              </Link>
            </div>
          </div>
          <div className="hero-art">
            <img src="/demo-art/visioncast-landing.svg" alt="" width={1600} height={900} />
          </div>
        </div>
      </section>

      <section className="section" aria-labelledby="upcoming-title">
        <div className="wrap">
          <div className="section-head">
            <h2 id="upcoming-title">Coming up</h2>
            <Link href="/events?when=upcoming">All upcoming events →</Link>
          </div>
          {upcoming.ok ? (
            upcoming.data.results.length ? (
              <ul className="card-grid">
                {sortForDisplay(upcoming.data.results, now).map((e) => (
                  <li key={e.slug}>
                    <EventCard event={e} now={now} />
                  </li>
                ))}
              </ul>
            ) : (
              <div className="notice">
                <h3>No upcoming sessions are scheduled</h3>
                <p>
                  Past sessions and recordings are in the <Link href="/events">event index</Link>.
                </p>
              </div>
            )
          ) : (
            <BackendUnavailable reason={upcoming.reason} />
          )}
        </div>
      </section>

      <section className="section" id="platform" aria-labelledby="platform-title">
        <div className="wrap">
          <div className="section-head">
            <h2 id="platform-title">What is in the platform</h2>
          </div>
          <ul className="feature-grid">
            {FEATURES.map((f, i) => (
              <li key={f.title}>
                <span className="num" aria-hidden="true">
                  {String(i + 1).padStart(2, '0')}
                </span>
                <h3>{f.title}</h3>
                <p>{f.body}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className="section" id="how-it-works" aria-labelledby="how-title">
        <div className="wrap">
          <div className="section-head">
            <h2 id="how-title">How this demo is wired</h2>
          </div>
          <ol className="flow">
            <li>
              <h3>Live catalogue</h3>
              <p>
                Pages are rendered on the server from <code>GET /api/v1/events</code> on the Visioncast back end, a
                Django service on its own dedicated server. If it does not answer, pages say so; there is no cached
                stand-in.
              </p>
            </li>
            <li>
              <h3>Same-origin proxy</h3>
              <p>
                Your browser only talks to this site. <code>/webinar-api/*</code> forwards five fixed routes to the
                back end with a scoped key that stays on the server, after checking Origin and a CSRF token.
              </p>
            </li>
            <li>
              <h3>Your browser, your cookie</h3>
              <p>
                Registering sets an HttpOnly, Secure, SameSite=Lax cookie scoped to <code>/webinar-api</code>. It
                lets this browser skip known fields next time, and “Not you?” revokes it. No tracking scripts run
                here.
              </p>
            </li>
          </ol>
        </div>
      </section>
    </>
  );
}
