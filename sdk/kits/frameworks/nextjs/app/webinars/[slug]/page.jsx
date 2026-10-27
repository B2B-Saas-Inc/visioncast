// App Router page (server component): event data from the public catalogue, form from the SDK.
// Copy sdk/kits/headless/webinar-events.mjs to lib/ and the React component to components/.
import { notFound } from 'next/navigation';
import WebinarForm from '../../../components/WebinarForm';
import { formProps, getWebinarEvent, pickOccurrence, sdkScriptUrl } from '../../../lib/webinar-events.mjs';

export const revalidate = 60; // catalogue data only; the form itself is never cached

export default async function WebinarPage({ params }) {
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
