import Link from 'next/link';

export function SiteFooter() {
  return (
    <footer className="site-footer dark">
      <div className="wrap">
        <div>
          <p>
            <span className="demo-flag">Demo</span>
          </p>
          <p>
            This is a public demonstration of the Visioncast webinar platform. The events, hosts and organisations
            shown are fictional and were created for the demo. Event data comes live from the platform&rsquo;s public
            API; registrations made here are real submissions to the demo back end. Use fictional test details only.
            No confirmation emails or marketing messages are sent.
          </p>
        </div>
        <div>
          <p>
            <Link href="/events">All events</Link>
            <br />
            <Link href="/events?when=recorded">Recordings</Link>
            <br />
            <Link href="/integrations">Integrations</Link>
            <br />
            <Link href="/features">Paid tickets &amp; provisioning</Link>
            <br />
            <Link href="/#how-it-works">How this demo works</Link>
            <br />
            <a href="https://github.com/B2B-Saas-Inc/visioncast">Open source · Apache-2.0 · GitHub</a>
          </p>
        </div>
      </div>
    </footer>
  );
}
