import type { Metadata } from 'next';
import Link from 'next/link';

import { HOSTING_CAPABILITIES, HOSTING_LIMITATIONS, SUPPORT_LABEL, TICKET_POINTS } from '@/lib/features';

export const metadata: Metadata = {
  title: 'Features',
  description:
    'Paid tickets, seat limits and session provisioning in VisionCast. Explained here; not configured on this demo.',
};

const COLUMNS = [
  { key: 'create', label: 'Create' },
  { key: 'update', label: 'Update' },
  { key: 'attach', label: 'Attach existing' },
  { key: 'perRegistrantLinks', label: 'Per-registrant links' },
  { key: 'manualLink', label: 'Manual link' },
] as const;

export default function FeaturesPage() {
  return (
    <>
      <section className="page-head dark" aria-labelledby="features-title">
        <div className="wrap">
          <p className="eyebrow">Product features</p>
          <h1 id="features-title">Paid tickets and session provisioning</h1>
          <p className="lead">
            How VisionCast sells seats and sets up sessions on your video platform. These features exist in the current
            back end; this demo runs an older version and has none of them configured.
          </p>
        </div>
      </section>

      <div className="wrap">
        <p className="section not-live-note" role="note">
          <strong>Not live on this demo.</strong> No checkout is configured, no demo event charges for registration, and
          no hosting provider is connected. Demo events show no seat limits or sold-out states.
        </p>

        <section className="section" id="tickets" aria-labelledby="tickets-title">
          <div className="section-head">
            <h2 id="tickets-title">Paid tickets and seat limits</h2>
          </div>
          <ul className="feature-grid">
            {TICKET_POINTS.map((p, i) => (
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

        <section className="section" id="hosting" aria-labelledby="hosting-title">
          <div className="section-head">
            <h2 id="hosting-title">Session provisioning by platform</h2>
            <Link href="/integrations?category=hosting">Hosting integrations →</Link>
          </div>
          <p>Not every platform supports the same things. This is what each adapter can do today.</p>
          <div className="table-scroll">
            <table className="capability-table">
              <caption className="visually-hidden">Hosting platform capabilities</caption>
              <thead>
                <tr>
                  <th scope="col">Platform</th>
                  {COLUMNS.map((c) => (
                    <th scope="col" key={c.key}>
                      {c.label}
                    </th>
                  ))}
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {HOSTING_CAPABILITIES.map((row) => (
                  <tr key={row.provider}>
                    <th scope="row">{row.provider}</th>
                    {COLUMNS.map((c) => (
                      <td key={c.key}>{SUPPORT_LABEL[row[c.key]]}</td>
                    ))}
                    <td>{row.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3>Limitations</h3>
          <ul>
            {HOSTING_LIMITATIONS.map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </section>
      </div>
    </>
  );
}
