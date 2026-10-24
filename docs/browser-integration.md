# Browser integration

This guide covers the browser SDK (`sdk/src/webinar-form.js`) and the first-party
Node proxy (`sdk/proxy/server.js`). It describes what that code does. Some
behaviour depends on the webinar backend, which another team owns. Wherever this
guide relies on the backend, it says so.

Status: the SDK and proxy have not been tested in a real deployment yet. Do not
treat this integration as production-ready until you have verified it against
the real backend.

## 1. Overview and architecture

```
Browser (marketing site, e.g. https://www.example.com)
  webinar-form.js  --same-origin fetch-->  https://www.example.com/webinar-api/*
                                              | (reverse proxy on the marketing origin)
                                              v
                                    sdk/proxy/server.js (Node, stdlib only)
                                              | Authorization: Bearer <PROXY_API_KEY>
                                              v
                                    Backend /api/v1/{profile,registrations,reset}
```

| Browser route | Method | Upstream route |
|---|---|---|
| `/webinar-api/profile?occurrenceId=..&formVersion=..` | GET | `/api/v1/profile` |
| `/webinar-api/registrations` | POST | `/api/v1/registrations` |
| `/webinar-api/reset` | POST | `/api/v1/reset` |
| `/webinar-api/my-registration?occurrenceId=<uuid>` | GET | `/api/v1/my-registration` (requires the `X-Webinar-CSRF` double-submit header; body projected to `occurrenceId, recognized, registered, status` plus allowlisted `thankYou`/`calendar` keys when registered (see §5); not called by the SDK) |
| `/webinar-api/calendar.ics?occurrenceId=<uuid>` | GET | `/api/v1/occurrences/<uuid>/calendar.ics` (no key, no cookies forwarded) |

- The proxy keeps the scoped proxy key on the server. The browser, GTM and the
  page never see it, so you put no secrets in GTM.
- The SDK sends requests with `credentials: 'same-origin'` to a relative
  `proxyBase`. It rejects absolute and protocol-relative values and falls back
  to `/webinar-api`.
- The backend issues the browser identity cookie (the session capability) as an
  opaque value. The proxy only relays it and re-issues it with safe attributes.
  The backend owns how that cookie is issued, hashed, resolved and revoked.

## 2. Proxy deployment

### Environment variables (`loadConfig`)

If any variable is invalid, the proxy prints every error and exits with status 1.

| Variable | Required | Default | Rules |
|---|---|---|---|
| `PROXY_BACKEND_URL` | yes | – | Absolute URL, **origin only** (`scheme://host[:port]`): no path, query, hash or credentials. Must be `https:` unless `PROXY_ALLOW_INSECURE_BACKEND=1`. |
| `PROXY_API_KEY` | yes | – | 32 or more characters, no whitespace. Sent upstream as `Authorization: Bearer <key>`. |
| `PROXY_ALLOWED_ORIGINS` | yes | – | Comma-separated exact origins (`https://www.example.com`). Each one must equal its own normalised origin (no trailing slash or path). Must be https, except hostnames `localhost` / `127.0.0.1`. |
| `PROXY_BODY_LIMIT` | no | `32768` | Bytes, 1024 to 1048576. |
| `PROXY_TIMEOUT_MS` | no | `10000` | Upstream timeout, 100 to 60000. |
| `PROXY_SESSION_COOKIE` | no | `webinar_session` | Name of the backend session cookie that is forwarded and re-issued. Must match what the backend sets (backend-dependent). |
| `PROXY_CSRF_COOKIE` | no | `__Host-webinar_csrf` | Double-submit cookie name. **The SDK hard-codes `__Host-webinar_csrf`, so leave this at the default.** |
| `PROXY_TRUST_FORWARDED_FOR` | no | off | `1` = use the first `X-Forwarded-For` entry as the client IP. Enable it only behind a reverse proxy that overwrites the header. |
| `PROXY_ALLOW_INSECURE_BACKEND` | no | off | `1` allows an `http:` backend. Use it for local development only. |
| `HOST` | no | `127.0.0.1` | Listen address. |
| `PORT` | no | `8787` | Listen port. |

Run the proxy:

```sh
PROXY_BACKEND_URL=https://webinars.internal.example.com \
PROXY_API_KEY=... \
PROXY_ALLOWED_ORIGINS=https://www.example.com \
node sdk/proxy/server.js
```

Use a process supervisor and a secret store for the key. Never commit the key.

### Reverse-proxy routing

Mount the proxy on the **marketing origin** under `/webinar-api/`. Keep the
path unchanged: the proxy matches the full `/webinar-api/...` path. Example
nginx block (adapt it to your setup):

```nginx
location /webinar-api/ {
    proxy_pass http://127.0.0.1:8787;          # no URI part: path is passed through unchanged
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;  # overwrite, do not append
    client_max_body_size 64k;
    proxy_buffering off;
    proxy_cache off;
}
```

nginx forwards `Origin`, `Cookie` and `Sec-Fetch-Site` by default. Do not strip
those headers: the proxy needs them. If your site sends a Content-Security-Policy,
`connect-src 'self'` already covers the proxy.

### Security checks (in order)

1. **Exact route allowlist.** Only the three paths above are accepted. The raw
   request path must equal its normalised form, so trailing slashes, `..` and
   encoded variants get `404`.
2. **Method.** GET for profile, POST for registrations and reset. Any other
   method gets `405` with an `Allow` header.
3. **Fetch metadata and Origin.** If `Sec-Fetch-Site` is present, it must be
   `same-origin` (`none` is also accepted for GET). If `Origin` is present, it
   must exactly match one entry in `PROXY_ALLOWED_ORIGINS`. GET without `Origin`
   is allowed. POST without `Origin` is rejected.
4. **Profile query.** `occurrenceId` and `formVersion` must each match
   `^[A-Za-z0-9._:-]{1,128}$`. Only those two parameters are forwarded.
5. **CSRF cookie issue (GET).** If the request has no valid CSRF cookie, the
   proxy sets `__Host-webinar_csrf=<random 32 bytes base64url>; Path=/; Secure;
   SameSite=Strict; Max-Age=86400`. The cookie is deliberately **not** HttpOnly,
   because the SDK reads it. The cookie is only sent when the response
   succeeds. Error responses do not carry it.
6. **CSRF double-submit (POST).** The `X-Webinar-CSRF` header must equal the
   cookie value. Both must be 43-character base64url strings. The comparison is
   timing-safe.
7. **Content type (POST).** Must be `application/json`, optionally with
   `charset=utf-8`. The one exception: `/reset` may have an empty body with
   `Content-Length: 0`.
8. **Body limit.** Enforced on the declared `Content-Length` and while the body
   streams in.
9. **JSON shape.** The body must be a JSON object. `/registrations` also
   requires `schemaVersion === '1'`. The proxy re-serialises the body before
   forwarding it.
10. **Cookie allowlist forwarding.** Only the session cookie, with a valid
    cookie value, goes upstream. Every other cookie (analytics, ads, CSRF) is
    dropped.
11. **Upstream headers.** The proxy sends `Authorization: Bearer`, a fresh
    `X-Request-Id`, `X-Forwarded-For` (client IP) and
    `X-Forwarded-Proto: https`.
12. **Set-Cookie rewriting.** Only upstream cookies named
    `PROXY_SESSION_COOKIE` are passed on. They are rebuilt as
    `name=value; Path=/; [Max-Age|Expires]; HttpOnly; Secure; SameSite=Lax`
    with no `Domain`. All other upstream cookies and attributes are dropped.
13. **Profile projection.** Any 2xx profile response is reduced to
    `{recognized, missingFields, formVersion}`. Non-string `missingFields`
    entries, and entries over 128 characters, are removed.
14. **Upstream errors.** Upstream 5xx or 1xx responses, non-JSON bodies,
    unparseable JSON and bodies over 256 KiB become `502`. A timeout becomes
    `504`. Upstream 4xx statuses are kept, but the body is projected to
    `{error?: "<snake_code>", errors?: {"<fieldKey>": ["invalid"]}}`. Server
    messages (which could contain PII) never reach the browser. Registration
    and reset 2xx bodies are passed through as JSON.
15. **Response headers.** Every response carries `Cache-Control: no-store`,
    `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and
    `Vary: Origin, Cookie`.

### Proxy error codes

Error bodies look like `{"error": "<code>"}`.

| Status | Code | Cause |
|---|---|---|
| 400 | `bad_request` | Unparseable URL or request stream error |
| 400 | `invalid_query` | Missing or invalid `occurrenceId` / `formVersion` |
| 400 | `invalid_json` | Body is not a JSON object (or is empty, except on reset) |
| 400 | `unsupported_schema_version` | Registration `schemaVersion` is not `'1'` |
| 403 | `origin_not_allowed` | Origin or Sec-Fetch-Site check failed, or POST has no Origin |
| 403 | `csrf_failed` | CSRF header and cookie missing, malformed or mismatched |
| 404 | `not_found` | Path is not on the allowlist |
| 405 | `method_not_allowed` | Wrong method for the route |
| 413 | `payload_too_large` | Body exceeds `PROXY_BODY_LIMIT` |
| 415 | `unsupported_media_type` | POST content type is not JSON |
| 500 | `internal_error` | Unexpected proxy error (only the error name is logged) |
| 502 | `bad_gateway` | Upstream 5xx/1xx, invalid or oversized response, connection failure |
| 504 | `gateway_timeout` | Upstream exceeded `PROXY_TIMEOUT_MS` |

The backend defines its own 4xx codes and body shapes (for example validation
errors, 409 idempotency conflicts and 429 rate limits). The proxy keeps their
status codes and projects their bodies as described above.

## 3. HTML form markup

The SDK enhances every `form[data-webinar-form]`.

| Attribute or element | Purpose |
|---|---|
| `data-occurrence-id` | Required. Occurrence identifier. Must match the proxy's query pattern. |
| `data-form-version` | Required. Form version, sent with the profile lookup and the registration. |
| `data-field-map` | Optional JSON `{"htmlName": "canonicalField"}`. Values must be canonical field names; any other value is ignored. Takes precedence over canonical names and aliases. |
| `data-confirm-label` | Optional. Plain-text submit label shown when a recognised browser has nothing missing (default `Confirm my RSVP`; whitespace collapsed, max 60 characters, set as text, never HTML). The normal label is the submit button's own text. Studio's per-session "Embed on your site" page fills both from the event's CTA labels. |
| `name="email"` and similar | Canonical fields: `email`, `phone`, `firstName`, `lastName`, `name`, `company`, `jobTitle`, `companySize`. |
| `name="custom.<key>"` | Custom field. `<key>` must match `^[A-Za-z][A-Za-z0-9_]{0,63}$`. |
| `name="organization.externalId"` + `data-namespace` | External organisation ID. The namespace must match `^[a-z][a-z0-9_.-]{0,63}$`, otherwise the value is silently dropped. |
| `name="consents.sms"` (checkbox) + `data-disclosure-version` | Explicit SMS consent. A missing disclosure version blocks submission. |
| `[data-webinar-field]` | Optional wrapper. If present, the SDK hides and shows the wrapper; otherwise it hides the control and its `<label>`s. |
| `[data-webinar-status]` | Status region (`role=status`, `aria-live=polite`). Created if absent. |
| `[data-webinar-error]` | Error region (`role=alert`, `tabindex=-1`). Created at the top of the form if absent. |
| `[data-webinar-reset]` | "Not you?" button. Created if absent. Visible only when the browser is recognised. |

Supported snake_case aliases (exact match only): `first_name`, `last_name`,
`full_name` (maps to `name`), `job_title`, `company_name` (maps to `company`),
`company_size`, `email_address` (maps to `email`), `phone_number` (maps to
`phone`). Any other name needs `data-field-map`. Names the SDK does not
recognise are ignored and not sent.

```html
<form data-webinar-form
      data-occurrence-id="occ_2026_11_spring"
      data-form-version="3"
      data-field-map='{"work_email": "email"}'>
  <div data-webinar-error></div>

  <div data-webinar-field>
    <label for="wf-email">Work email</label>
    <input id="wf-email" name="work_email" type="email" autocomplete="email" required>
  </div>
  <div data-webinar-field>
    <label for="wf-first">First name</label>
    <input id="wf-first" name="first_name" autocomplete="given-name" required>
  </div>
  <div data-webinar-field>
    <label for="wf-last">Last name</label>
    <input id="wf-last" name="lastName" autocomplete="family-name">
  </div>
  <div data-webinar-field>
    <label for="wf-company">Company</label>
    <input id="wf-company" name="company" autocomplete="organization">
  </div>
  <div data-webinar-field>
    <label for="wf-size">Company size</label>
    <select id="wf-size" name="companySize">
      <option value="">Choose…</option><option>1-49</option><option>50-249</option><option>250+</option>
    </select>
  </div>
  <div data-webinar-field>
    <label for="wf-phone">Mobile (international format, e.g. +14155550100)</label>
    <input id="wf-phone" name="phone" type="tel" autocomplete="tel">
  </div>
  <div data-webinar-field>
    <label for="wf-role">Your role</label>
    <input id="wf-role" name="custom.role">
  </div>
  <input type="hidden" name="organization.externalId" data-namespace="crm.account" value="ACC-1234">

  <label>
    <input type="checkbox" name="consents.sms" data-disclosure-version="sms-2026-01">
    Text me event reminders. Message rates may apply. Reply STOP to opt out.
  </label>

  <button type="submit">Save my seat</button>
  <button type="button" data-webinar-reset hidden>Not you? Start over</button>
  <div data-webinar-status></div>
</form>
<script src="/assets/webinar-form.js" defer></script>
```

The SDK sets `novalidate` on the form and runs its own validation with
`checkValidity()` on enabled controls, so HTML attributes such as `required`
and `type="email"` still apply. The backend decides whether a phone number
is a valid E.164 number and how email is normalised.

## 4. Field mapping guide

| HTML `name` | Payload path |
|---|---|
| `email`, `email_address` | `profile.email` |
| `phone`, `phone_number` | `profile.phone` |
| `firstName`, `first_name` | `profile.firstName` |
| `lastName`, `last_name` | `profile.lastName` |
| `name`, `full_name` | `profile.name` |
| `company`, `company_name` | `profile.company` |
| `jobTitle`, `job_title` | `profile.jobTitle` |
| `companySize`, `company_size` | `profile.companySize` |
| any key in `data-field-map` | `profile.<mapped canonical field>` |
| `custom.<key>` | `customFields.<key>` |
| `organization.externalId` (+ `data-namespace`) | `organization.externalIds[] = {namespace, value}` |
| `consents.sms` (checkbox) | `consents.sms = {granted, disclosureVersion}` |
| (from consent state) | `consents.analytics.granted`, `consents.advertising.granted` |

Rules applied by the SDK:

- Values are trimmed. **Blank values are left out**, so a blank field cannot
  erase a stored value. This also means the form cannot clear a value on
  purpose.
- Disabled controls (including fields hidden by progressive profiling) are
  never sent. Unchecked checkboxes and radios are skipped, except
  `consents.sms`, which sends `granted: false`.
- If two controls map to the same canonical field with **different** values
  (for example `first_name` and `firstName`), the SDK rejects the submission
  and marks the field. Identical values are accepted. Duplicate `custom.*`
  names are not checked: the last value wins.
- The SDK **never splits names**. `name` and `firstName`/`lastName` are sent
  exactly as entered.

Full request body (`schemaVersion: '1'`):

```json
{
  "schemaVersion": "1", "occurrenceId": "...", "formVersion": "...", "idempotencyKey": "<uuid>",
  "profile": {"email": "..."},
  "organization": {"externalIds": [{"namespace": "crm.account", "value": "ACC-1234"}]},
  "consents": {"analytics": {"granted": false}, "advertising": {"granted": false},
               "sms": {"granted": true, "disclosureVersion": "sms-2026-01"}},
  "attribution": {"parameters": {}, "identifiers": {}, "pageUrl": "...", "referrer": "..."},
  "customFields": {"role": "..."}
}
```

The backend owns server-side normalisation, validation and alias handling,
including any per-form mappings it stores. The SDK's mapping only shapes the
request.

## 5. Progressive profiling behaviour

- **Profile lookup.** On init the SDK calls `GET /profile` with the form's
  occurrence and version. If the browser is anonymous
  (`recognized !== true`), the request fails, or either attribute is missing,
  the user sees the **full form**.
- **Recognised.** Fields whose key (canonical name or `custom.<key>`) is
  **not** in `missingFields` are hidden and disabled, and `required` is
  suspended. The status region shows "Welcome back…". The backend decides
  which browser is recognised and what counts as missing.
- **Nothing missing.** The submit button label becomes the form's
  `data-confirm-label`, or `Confirm my RSVP` if absent. "Not you?" restores
  the button's original text.
- **Not you? Start over.** Sends `POST /reset` with `{}`. On success the SDK
  resets the form, shows every field, clears the idempotency key and focuses the
  first field. On failure it shows an error and changes nothing. What reset does
  on the server (for example revoking the cookie) is up to the backend.
- **No PII displayed.** The SDK never shows or pre-fills stored profile values.
  The proxy projection lets only field keys reach the browser.
- **Idempotency.** The SDK generates a key with `crypto.randomUUID` or
  `getRandomValues` and keeps it **in memory only**. A retry with an identical
  payload reuses the key. A changed payload gets a new key. The key is cleared
  after success or reset. The backend handles conflict detection and returns 409
  on a conflict.
- **No false success.** The SDK reports success only on a 2xx response.
  Network errors and its 15 s timeout show "your RSVP was not saved". The SDK
  maps 400/422, 409, 429 and other statuses to fixed messages and never echoes
  server error text.
- **Accessible errors.** The error region (`role=alert`) receives focus.
  Invalid fields get `aria-invalid="true"` and an `aria-describedby` link to an
  inline message. If the server returns field keys in `errors` (an array of
  `{field}` objects or a map) or in `fieldErrors`, the SDK highlights those
  fields. A `profile.` prefix is stripped and `customFields.` becomes `custom.`.
  The backend's exact error format is not confirmed yet.
- **Success.** The SDK reads `thankYou.redirectUrl`, `thankYou.message` and
  `calendar.icsUrl` from the response. The backend returns these fields
  (`webinars/registration.py` `build_response_body`). The backend's relative
  `calendar.icsUrl` is rewritten by this proxy to
  `/webinar-api/calendar.ics?occurrenceId=<uuid>`. In direct pass-through mode the
  platform rewrites it the same way unless `WEBINAR_DIRECT_ICS_REWRITE=false` (see
  [implementation-guide.md §6.2](implementation-guide.md#62-calendar-ics-supported)).
  A redirect is followed only if it
  is https or same-origin. Otherwise the SDK disables the form, sets
  `data-webinar-state="complete"`, shows the message (as text) or a default,
  and appends an "Add to calendar" link (`a[data-webinar-calendar]`, unchanged)
  if `icsUrl` passes the same URL check.
- **Web calendar links.** After the ICS link the SDK appends "Add to Google
  Calendar", "Add to Outlook.com" and "Add to Outlook (Microsoft 365)" links from
  `calendar.googleUrl`, `outlookUrl` and `office365Url`. Each link is rendered only
  if it is https, has no credentials, port or fragment, and its origin and path
  exactly match that service's endpoint (`calendar.google.com/calendar/render`,
  `outlook.live.com/calendar/0/deeplink/compose`,
  `outlook.office.com/calendar/0/deeplink/compose`). Anything else, including
  `null` (cancelled or ended sessions), is skipped. The links are text-only
  anchors marked `data-webinar-calendar-link="google|outlook|office365"`. They
  open in a new tab with `rel="noopener noreferrer"`, and their `aria-label` is
  the visible text followed by "(opens in a new tab)".
- **Proxy projection.** On `/webinar-api/my-registration`, the proxy keeps only
  `thankYou.{mode, message, redirectUrl (https only)}` and
  `calendar.{title, startsAt, endsAt, timezone, icsUrl, googleUrl, outlookUrl,
  office365Url, uid, sequence}`. It applies the same web-link check (failing links
  become `null`). The `/webinar-api/registrations` 2xx body is still passed through
  (apart from the `icsUrl` rewrite), so the SDK check above is the browser-side guard.
- **dataLayer.** On success, if analytics consent is granted and
  `window.dataLayer` exists, the SDK pushes
  `{event: 'webinar_registration', webinarOccurrenceId, webinarFormVersion}`.
  The event contains no PII.

## 6. GTM configuration

Host `webinar-form.js` on a first-party URL (your own origin or CDN path). The
platform image also publishes the unmodified file at
`/static/webinar-sdk/webinar-form.js` (collected from
`webinars/static/webinar-sdk/`; `sdk/src/webinar-form.js` is a symlink to that
single source, including in the bounded preview). Then create a **Custom HTML** tag:

```html
<script>
  window.WebinarFormConfig = {
    proxyBase: '/webinar-api',          // relative path only
    autoInit: true,                     // false = call WebinarForm.init() yourself
    consent: { analytics: false, advertising: false },   // initial state (default denied)
    // consentProvider: function () { return { analytics: ..., advertising: ... }; },
    messages: { success: 'Thanks, see you there!' }       // override any default message key
  };
</script>
<script src="https://www.example.com/assets/webinar-form.js"></script>
```

- **Trigger.** Fire on the pages that contain the form, for example
  "Page View – DOM Ready" filtered on the page path. Fire it once per page. The
  script ignores a second load if `window.WebinarForm` already exists.
- **Config before script.** `WebinarFormConfig` is read when the script runs,
  so define it first. You can also set `data-proxy-base` on the script tag.
- **Late-rendered forms** (SPA, modal, CMS widget): call
  `WebinarForm.init()` or `WebinarForm.init(containerElement)` after the form
  is in the DOM. Forms that are already bound are skipped.
- **Message keys you can override:** `welcomeBack`, `welcomeBackComplete`,
  `notYou`, `resetDone`, `resetFailed`, `submitting`, `success`,
  `addToCalendar`, `addToGoogleCalendar`, `addToOutlookCalendar`,
  `addToOffice365Calendar`, `opensInNewTab`, `network`, `invalid`, `conflict`, `rateLimited`, `server`,
  `fieldRequired`, `fieldInvalid`, `fieldConflict`. The confirm-only button
  label is set per form with `data-confirm-label`, not through `messages`.

## 7. Consent hooks

- **Default denied.** Without a consent signal, the SDK reads no URL
  parameters, cookies, page URL or referrer, stores nothing in
  `sessionStorage` and pushes no dataLayer event. The payload still sends
  `consents.analytics/advertising.granted: false`.
- `WebinarForm.setConsent({analytics: true|false, advertising: true|false})`
  sets the state. Only the value `true` grants a purpose.
- `WebinarForm.setConsentProvider(fn)` registers a function that is called
  whenever the SDK needs consent. If it throws, consent counts as denied. It
  overrides `setConsent` state. Pass a non-function to remove it.
- `WebinarForm.getConsent()` returns the current normalised state.

Example only, adapt it to your CMP. Suppose your CMP pushes a `consent_update`
event to the dataLayer and you have mapped its purposes into GTM Data Layer
Variables. Create a Custom HTML tag with a Custom Event trigger on
`consent_update`:

```html
<script>
  if (window.WebinarForm) {
    WebinarForm.setConsent({
      analytics: {{DLV - consent analytics}} === 'granted',     // e.g. maps to analytics_storage
      advertising: {{DLV - consent advertising}} === 'granted'  // e.g. ad_storage / ad_user_data
    });
  }
</script>
```

If the CMP has a synchronous JS API, a provider avoids timing gaps:
`WebinarForm.setConsentProvider(function () { var c = myCmp.getConsent(); return {analytics: c.statistics, advertising: c.marketing}; });`.
Google Consent Mode has no supported public read API for page scripts. Map
your CMP's own signal instead.

- **Withdrawal.** Calling `setConsent` (or `setConsentProvider`) with both
  purposes denied removes the session-stored parameters. A partial withdrawal
  re-filters the stored values to the purposes that are still granted. If you
  rely on a provider alone, stored values are only re-filtered when they are
  read, so call `setConsent` or `setConsentProvider` again on withdrawal to
  clear storage.
- **SMS consent** is separate. It comes only from the explicit
  `consents.sms` checkbox with a disclosure version, never from the CMP.
- **Client consent is a claim.** The `consents` block reports what the
  browser said. The backend must record it as client-asserted, not as
  server-verified evidence (backend-dependent).

## 8. Attribution

Collected only at submit time, and only with consent.

| Gate | Allowlisted URL parameters |
|---|---|
| analytics | `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`, `utm_id`, `utm_source_platform`, `utm_creative_format`, `utm_marketing_tactic` |
| advertising | `gclid`, `gbraid`, `wbraid`, `gad_source` (Google Ads); `fbclid` (Meta); `twclid` (X); `ScCid` (Snap); `rdt_cid` (Reddit); ValueTrack `campaignid`, `adgroupid`, `keyword`, `matchtype`, `network`, `device`, `creative`, `placement`, `targetid` |

- A value is kept only if it is populated: non-empty after trimming, 500
  characters or fewer, and without `{` or `}`. Unsubstituted ValueTrack or
  macro placeholders such as `{keyword}` are dropped.
- `pageUrl` and `referrer` are sent only with analytics consent, and are
  stripped to **origin + path**. Query and hash are removed, and non-http(s)
  URLs are dropped.
- **First touch in session.** At boot and on every consent change, the SDK
  saves allowlisted landing parameters to `sessionStorage`
  (`webinarForm.attribution.v1`). The first value per key is kept for the
  tab session. At submit, stored values override the current page's values
  for the same key. Nothing is persisted beyond the tab session, and form data
  is never stored.
- **No fingerprinting.** The SDK never reads the whole URL or the full cookie
  string. It reads individual named parameters and named cookies only. It
  collects no device, screen or font data.
- Attribution never authenticates anyone. How it is stored (first-touch or
  registration-touch tables) is up to the backend.

## 9. Cookie identifiers

| Cookie | Payload key | Vendor / purpose | Consent gate |
|---|---|---|---|
| `_ga` | `ga4ClientId` (parsed to `<random>.<timestamp>`) | GA4 client ID | analytics |
| `_gcl_aw` | `gclAw` | Google Ads click | advertising |
| `_fbp` | `fbp` | Meta browser ID | advertising |
| `_fbc` | `fbc` | Meta click ID | advertising |
| `_twclid` | `twclid` | X click ID | advertising |
| `_scid` | `scid` | Snap ID | advertising |
| `_rdt_uuid` | `rdtUuid` | Reddit ID | advertising |

Values must match `^[A-Za-z0-9._~:|%-]{1,256}$`. Any other value is dropped.

**Availability is NOT guaranteed.** Any of these may be missing or stale
because of:

- ad blockers or privacy extensions
- browser tracking protection (Safari ITP, Firefox ETP): cookie lifetime caps,
  partitioning, link decoration stripping
- the vendor tag not being installed, or not having fired yet when the user
  submits
- cookies set server-side as HttpOnly, which JS cannot read
- the user not granting consent

The values are **unverified claims from the browser**. They never authenticate
or identify a person, and downstream systems must treat them as best-effort.

## 10. Demo

```sh
node sdk/demo/demo-server.js
# open http://localhost:8080/
```

The demo uses an in-memory mock backend and is for local demonstration only.
It is not a reference for backend behaviour and must not be deployed.
It serves `sdk/demo/index.html` and the SDK, mounts the real proxy handler on
`/webinar-api/*`, and points it at `sdk/demo/mock-backend.js` on
`127.0.0.1:8090` with an ephemeral proxy key. Use Chrome or Firefox: the
`Secure` / `__Host-` cookies rely on `http://localhost` being treated as a
secure context.

## 11. Known limitations

- The backend's response fields (`thankYou`, `calendar`, profile
  `{recognized, missingFields, formVersion}`, reset `{reset: true}`, cookie
  `CAPABILITY_COOKIE_NAME` default `webinar_session`) match the code. None of
  this has been verified in a real deployment.
- The SDK hard-codes the CSRF cookie `__Host-webinar_csrf` and header
  `X-Webinar-CSRF`. Changing `PROXY_CSRF_COOKIE` breaks submissions.
- The proxy routes are fixed at `/webinar-api/*`. A different SDK `proxyBase`
  only works if your reverse proxy rewrites requests back to those paths.
- Both `data-occurrence-id` and `data-form-version` are required. Without
  them the profile lookup cannot issue a CSRF cookie, so POSTs fail with
  `csrf_failed`.
- Blank inputs cannot clear stored values. There is no correction UI beyond
  "Not you? Start over".
- Cross-device recognition is not supported. A new browser always sees the
  full form (backend-dependent verification is not implemented here).
- Any https redirect or ICS URL from the backend is accepted. The SDK
  relies on the backend to validate thank-you URLs. Web calendar links are
  limited to the three fixed service endpoints. Their deep-link formats have not
  been verified against the live services.
- The `Confirm my RSVP` label cannot be changed through `messages`; use the
  form's `data-confirm-label` attribute instead.
- Duplicate `custom.*` fields are not checked for conflicts.
- Registration 2xx bodies are passed through as JSON. Keeping private join
  links and profile data out of them is the backend's job.
- Consent-provider-only setups do not clear `sessionStorage` on withdrawal
  until `setConsent` or `setConsentProvider` is called.
- The proxy is a single Node process with no rate limiting of its own. Rate
  limiting is left to the backend and the edge.
- None of this has been tested in a real deployment yet.
