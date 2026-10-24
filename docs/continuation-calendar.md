# Public event URL and calendar links

Calendar entries (the ICS file, Google Calendar and Outlook) can point at the event page on your marketing site instead of the JSON event API.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PUBLIC_EVENT_URL_TEMPLATE` | empty | Public event page URL, e.g. `https://www.example.com/webinars/{slug}`. When empty, calendar entries use `PUBLIC_BASE_URL/api/v1/events/<slug>`, which is the previous behaviour. |

Placeholders:

| Placeholder | Value |
|---|---|
| `{slug}` | Event slug |
| `{occurrence_id}` | Session (occurrence) UUID |

Rules (enforced by `webinars.calendar_links.validate_template`):

- At least one placeholder. Only `{slug}` and `{occurrence_id}` are accepted. There are no `{{` escapes, attribute or index access (`{slug.x}`, `{slug[0]}`), conversions or format specs.
- Placeholders may appear only in the path, query or fragment. They can't appear in the scheme, host or port.
- The template may contain only printable ASCII URI characters: no spaces, quotes, `<`, `>`, backslashes, control characters or non-ASCII characters.
- The URL must be `https` with a host. It can't contain credentials or a non-default port, and the rendered URL can be at most 1000 characters. This is the same policy as other public URLs (`validation.validate_public_https_url`).
- The template can be at most 500 characters.
- Values are percent-encoded before they are substituted, so `/`, `?` and `#` in a value can't change the URL's structure.

If the template is invalid, `manage.py check` reports `webinars.E101`. At runtime, the platform logs one warning and falls back to the API URL, so it never emits an unvalidated URL. A template that uses `{occurrence_id}` falls back to the API URL when no session is in context.

## What uses it

- **ICS** (`GET /api/v1/occurrences/<id>/calendar.ics`): sets the `URL` property and appends the URL to the end of `DESCRIPTION`.
- **`webinars.calendar_links.calendar_links(occurrence)`** returns:

  ```json
  {"ics": "https://webinars.example.com/api/v1/occurrences/<id>/calendar.ics",
   "google": "https://calendar.google.com/calendar/render?action=TEMPLATE&text=…&dates=20261110T150000Z/20261110T160000Z&details=…&ctz=Europe/London",
   "outlook": "https://outlook.live.com/calendar/0/deeplink/compose?path=/calendar/action/compose&rru=addevent&subject=…&startdt=…Z&enddt=…Z&body=…",
   "office365": "https://outlook.office.com/calendar/0/deeplink/compose?…"}
  ```

  `google`, `outlook` and `office365` are `null` when the session is cancelled or has ended. `ics` is always present, because a cancelled session's ICS carries `METHOD:CANCEL` so that calendars can remove the entry.

- **Public API** (`GET /api/v1/events`, `/api/v1/events/<slug>`, and MCP `get_event`/`list_events`, which share the serializer): every occurrence has `calendarLinks` with the object above.
- **Registration responses** (`registration.build_response_body`, so also idempotent replays and `my-registration`): `calendar.googleUrl`, `calendar.outlookUrl` and `calendar.office365Url` carry the three web links. `calendar.icsUrl` stays relative so the proxy and direct-mode rewrites still apply. A replay returns the links stored with the original response.
- `PUBLIC_EVENT_URL_TEMPLATE` is read from the environment (`config/settings.py`), and the `webinars.E101` check is registered in `webinars.apps`. The check runs under `manage.py check`, `migrate` and `runserver`, not at Gunicorn start-up.

Status: wired; tests (`tests/test_continuation_calendar.py`, `tests/test_continuation_glue.py`) written but not yet run.

## Privacy

Calendar output is built only from these fields:

- event title
- summary
- slug
- session ID
- start and end times
- time zone
- ICS UID and SEQUENCE

Join links, waiting-room links and managed-room codes are never read. Template values come only from the slug and session ID, so no configuration can place a private link in a calendar entry. Send join links only in confirmation and reminder emails.

## Injection hardening

- TEXT values (`SUMMARY`, `DESCRIPTION`, `UID`) are escaped according to RFC 5545 §3.3.11: `\`, `;`, `,` and newlines are escaped. Control characters other than newline and tab are removed, so submitted text can't start a new property or component.
- `URL` is written only if it is printable ASCII with no line break or quote.
- Content lines are folded at 75 octets.
- Web calendar link parameters are percent-encoded. The summary in the event details is cut to about 1000 characters, minus the length of the URL.
- `ctz` is included only if the time zone name matches `[A-Za-z0-9_+-/]{1,64}`.

## Limits

- The Google and Outlook deep-link formats are de facto conventions, not documented, versioned APIs. They are tested only for the URLs they produce, not against the live services.
- Outlook deep links have no time-zone parameter. Times are sent as UTC with `Z`.
- These are "add a copy" links. Unlike the ICS `UID`/`SEQUENCE`, they don't update the calendar entry when a session is rescheduled or cancelled.
