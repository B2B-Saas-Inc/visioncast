import Link from 'next/link';

export default function NotFound() {
  return (
    <div className="wrap" style={{ paddingTop: '4rem' }}>
      <p className="eyebrow">404</p>
      <h1>We could not find that page</h1>
      <p className="lead">The event may have been unpublished, or the link may be mistyped.</p>
      <p>
        <Link className="button" href="/events">
          Browse events
        </Link>
      </p>
    </div>
  );
}
