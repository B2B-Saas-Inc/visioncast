# Browser integration: proxy, errors and calendar

Companion to `docs/browser-integration.md`, the OpenAPI document served at `GET /api/v1/openapi.json` and `docs/registration.schema.json`. Where the two disagree on browser behaviour (SDK attributes, consent, proxy configuration), `docs/browser-integration.md` is authoritative; this page only adds proxy error shapes, the calendar rewrite and attribution details. For the end-to-end rollout see [implementation-guide.md](implementation-guide.md).

## Deployment modes

| Mode | Browser calls | Backend sees |
| --- | --- | --- |
| Example proxy (`sdk/proxy/server.js`) on the marketing origin | `/webinar-api/profile`, `/webinar-api/registrations`, `/webinar-api/reset`, `/webinar-api/calendar.ics` | `/api/v1/profile`, `/api/v1/registrations`, `/api/v1/reset` with the server-held proxy key; `/api/v1/occurrences/<id>/calendar.ics` without key or cookies |
| Same-origin direct (Django serves the page origin) | `/webinar-api/profile`, `/webinar-api/registrations`, `/webinar-api/reset`, `/api/v1/occurrences/<id>/calendar.ics` | the same paths |

`proxyBase` (`WebinarFormConfig.proxyBase` or `data-proxy-base`) must be a plain absolute path matching `^/[A-Za-z0-9._~/-]*$`, with no leading `//`, no backslash and no `.`/`..` segments. Anything else falls back to `/webinar-api`. URL parsers treat `/\evil.com` as `//evil.com`, so before this check that value sent form data to another origin.

## Error bodies

Backend (`webinars/registration.py` `error_body`):

```json
{"error": {"code": "validation_error", "message": "Some required fields are missing.", "fields": {"firstName": "Required."}},
 "fieldErrors": {"firstName": "Required."}}
```

Example proxy projection. The server text is removed and only the code and field keys are kept:

```json
{"error": {"code": "validation_error"}, "fieldErrors": {"firstName": "invalid"}}
```

Errors that the proxy itself generates are `{"error": "<code>"}` (for example `csrf_failed` or `payload_too_large`).

The SDK reads `fieldErrors`, `error.fields` and the legacy `errors` key. For each key it sets `aria-invalid="true"` on the matching enabled control, adds an `aria-describedby` message, and focuses the `role="alert"` summary. Field-key mapping:

- `profile.<k>` maps to `<k>`;
- `customFields.<k>` maps to `custom.<k>`;
- `consents.sms.*` maps to the `consents.sms` checkbox.

## Calendar link

The registration response returns `calendar.icsUrl` as the relative backend path `/api/v1/occurrences/<id>/calendar.ics`. In proxy mode that path does not exist on the marketing origin. The proxy therefore rewrites it to `/webinar-api/calendar.ics?occurrenceId=<id>` and drops any `icsUrl` it does not recognise.

Its calendar route:

- accepts only a UUID `occurrenceId`;
- checks `Sec-Fetch-Site`/`Origin` like other GETs;
- accepts only a `text/calendar` upstream response of 64 KB or less;
- returns a fixed `Content-Disposition`.

In same-origin direct mode the platform does the same rewrite and serves
`/webinar-api/calendar.ics` itself, so the edge must forward that path (or the whole
`/webinar-api/` prefix). `WEBINAR_DIRECT_ICS_REWRITE=false` restores the old
`/api/v1/occurrences/<id>/calendar.ics` link for edges that route that path instead.

The response's `calendar` block also carries `googleUrl`, `outlookUrl` and
`office365Url` (absolute; `null` when cancelled or ended). The proxy passes them through
unchanged; the SDK does not render them.

## My registration

`GET /webinar-api/my-registration?occurrenceId=<uuid>` returns this browser's
registration status for one occurrence (`{occurrenceId, recognized, registered, status}`,
plus `thankYou`/`calendar` when registered), from the capability cookie only. The proxy
forwards it to `/api/v1/my-registration` with its key and requires the double-submit
`X-Webinar-CSRF` header (the cookie is issued by `/webinar-api/profile`). It accepts only
a single UUID `occurrenceId` and projects the response to those keys. Direct mode applies
the same-origin checks plus the custom-header rule and a per-process rate limit. The SDK
does not call it.

## Attribution allowlist

The SDK collects only parameters that the server accepts (`webinars/attribution.py`). Values longer than 256 characters and unsubstituted `{...}` placeholders are skipped.

- **Analytics consent:** `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`, `utm_id`.
- **Advertising consent:**
  - `gclid`, `gbraid`, `wbraid`, `fbclid`, `twclid`, `ScCid`, `rdt_cid`;
  - ValueTrack `campaignid`, `adgroupid`, `keyword`, `matchtype`, `network`, `device`, `creative`, `placement`, `targetid`, `loc_physical_ms`.

The SDK no longer collects `gad_source`, `utm_source_platform`, `utm_creative_format` or `utm_marketing_tactic`, because the server dropped them anyway. The GA4 client id is sent as `gaClientId`, the server's canonical key. Cookie identifiers depend on vendor tags having set them, and their availability is never guaranteed.

## Identity limits (stated honestly)

- **No email verification:** there is no email-verified identity. Recognition relies only on the HttpOnly capability cookie issued to the browser that created the person.
- **Same email from another browser:** an existing email submitted from a different browser or device is accepted as an *unverified* submission. It is not linked, it does not reveal or update the stored profile, and that browser is not recognised later. Cross-device linking is not supported until verification exists.
- **No email correction:** a recognised browser cannot correct its email address. Submitting a different email is treated as registering someone else, and the capability is not rebound. The only correction path is **"Not you? Start over"** (reset), which revokes the capability and shows the full form.
