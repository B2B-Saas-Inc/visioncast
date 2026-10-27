const COPY = {
  not_configured: 'This deployment is not connected to a Visioncast back end yet (VISIONCAST_API_BASE is not set or is invalid).',
  unavailable: 'The Visioncast back end did not answer in time. Nothing is shown rather than out-of-date copies.',
  invalid_response: 'The Visioncast back end sent a response this page could not read.',
} as const;

/** Explicit failure state: the site never substitutes static data for the live catalogue. */
export function BackendUnavailable({ reason, what = 'events' }: { reason: keyof typeof COPY; what?: string }) {
  return (
    <div className="notice error" role="status">
      <h2>Live {what} are unavailable right now</h2>
      <p>{COPY[reason]}</p>
      <p>Please try again in a minute.</p>
    </div>
  );
}
