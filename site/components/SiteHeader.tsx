import Link from 'next/link';

import { NavLink } from './NavLink';

export function SiteHeader() {
  return (
    <header className="site-header dark">
      <div className="wrap">
        <Link href="/" className="wordmark" aria-label="Visioncast home">
          <span className="wordmark-dot" aria-hidden="true" />
          Visioncast
        </Link>
        <nav className="site-nav" aria-label="Main">
          <ul>
            <li>
              <NavLink href="/events">Events</NavLink>
            </li>
            <li>
              <Link href="/events?when=recorded">Recordings</Link>
            </li>
            <li>
              <NavLink href="/integrations">Integrations</NavLink>
            </li>
            <li>
              <NavLink href="/features">Features</NavLink>
            </li>
            <li>
              <Link href="/#platform">Platform</Link>
            </li>
          </ul>
        </nav>
      </div>
    </header>
  );
}
