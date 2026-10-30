import Link from 'next/link';

import { NavLink } from './NavLink';

export function SiteHeader() {
  return (
    <header className="site-header dark">
      <div className="wrap">
        <Link href="/" className="wordmark" aria-label="VisionCast home">
          {/* eslint-disable-next-line @next/next/no-img-element -- static SVG logo, no optimisation needed */}
          <img src="/brand/visioncast-logo-dark.svg" alt="VisionCast" width={146} height={28} />
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
