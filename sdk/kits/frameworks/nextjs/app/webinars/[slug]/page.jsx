// App Router page (server component): event data from the public catalogue, form from the SDK.
// Copy sdk/kits/headless/webinar-events.mjs to lib/ and the React component to components/.
//
// The catalogue read happens inside a <Suspense> boundary and uses no route segment config
// (`revalidate`, `dynamic`), so the same file builds on Next 14/15 and on Next 16, where
// create-next-app enables `cacheComponents` and rejects those exports. The page is rendered
// per request; the form's state always comes from /webinar-api/profile in the browser.
import { Suspense } from 'react';
import { notFound } from 'next/navigation';
import WebinarForm from '../../../components/WebinarForm';
import { formProps, getWebinarEvent, pickOccurrence, sdkScriptUrl } from '../../../lib/webinar-events.mjs';

export default function WebinarPage({ params }) {
  return (
    <Suspense fallback={<main><p>Loading…</p></main>}>
      <WebinarContent params={params} />
    </Suspense>
  );
}

async function WebinarContent({ params }) {
  const { slug } = await params;
  const event = await getWebinarEvent(slug);
  if (!event) notFound();
  const occurrence = pickOccurrence(event);
  const props = formProps(event, occurrence);
  return (
    <main>
      <h1>{event.title}</h1>
      <p>{event.summary}</p>
      {occurrence ? (
        <p>
          <time dateTime={occurrence.startsAt}>{occurrence.startsAt}</time> ({occurrence.timezone})
        </p>
      ) : null}
      {props ? <WebinarForm {...props} sdkSrc={sdkScriptUrl()} /> : <p>Registration is closed.</p>}
    </main>
  );
}
