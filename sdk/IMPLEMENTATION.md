# Browser SDK and first-party proxy: implementation report

Scope owned by this workstream: `sdk/**` and `docs/browser-integration.md`.
I modified no other files. **No tests were executed** in this workstream (no
shell access). The supervisor needs to run the commands below.

## Files

| Path | Purpose |
|---|---|
| `sdk/src/webinar-form.js` | Dependency-free ES5 browser script (GTM Custom HTML / `<script>`), also `require()`-able for tests |
| `sdk/proxy/server.js` | Node stdlib-only first-party proxy (`node sdk/proxy/server.js`), exports `loadConfig`, `createHandler`, `createProxyServer` |
| `sdk/test/proxy.test.js` | `node:test` suite for the proxy (spins up a fake backend on 127.0.0.1 with ephemeral ports) |
| `sdk/test/webinar-form.test.js` | `node:test` suite for SDK pure logic (consent gating, attribution, payload, field plan, idempotency, URL safety) |
| `sdk/demo/index.html`, `demo-server.js`, `mock-backend.js` | Local HTML demo using the real proxy handler and an in-memory mock backend |
| `sdk/package.json` | No dependencies; `test`, `proxy` and `demo` scripts |
| `docs/browser-integration.md` | GTM configuration, consent hooks, cookie availability, field mapping, proxy deployment |

## Test commands (for the supervisor)

```sh
cd sdk && node --test test/proxy.test.js test/webinar-form.test.js
# or, from the repo root:
node --test sdk/test/proxy.test.js sdk/test/webinar-form.test.js
# syntax-only checks:
node --check sdk/src/webinar-form.js && node --check sdk/proxy/server.js && node --check sdk/demo/demo-server.js
# manual demo (binds 127.0.0.1:8080 and 127.0.0.1:8090; foreground process):
node sdk/demo/demo-server.js   # open http://localhost:8080/ in Chrome/Firefox
```

Requires Node >= 18.17 (uses `crypto.randomUUID`, `Buffer` base64url and `node:test`). The proxy tests use loopback sockets only.

## Contract implemented (from SPEC "Browser integration")

- Browser → `/webinar-api/profile` GET (`occurrenceId`, `formVersion`), `/webinar-api/registrations` POST and `/webinar-api/reset` POST.
- Proxy → `/api/v1/profile`, `/api/v1/registrations`, `/api/v1/reset` with `Authorization: Bearer <PROXY_API_KEY>`. The key is server-held only.
- Profile responses are projected to exactly `{recognized, missingFields, formVersion}`.
- The POST body follows the SPEC shape exactly: `schemaVersion:'1'`, `occurrenceId`, `formVersion`, `idempotencyKey`, `profile`, `organization.externalIds[]`, `consents.{sms,analytics,advertising}`, `attribution.{parameters,identifiers,pageUrl,referrer}`, `customFields`.

## Security behaviour

### Proxy

- Fixed exact route allowlist, and the raw path must equal the normalised path, so traversal and encoded variants get 404.
- Wrong method gets 405 with an `Allow` header.
- Origin and CSRF checks:
  - Origin must exactly match `PROXY_ALLOWED_ORIGINS`.
  - POST requires `Origin`. `Sec-Fetch-Site` must be `same-origin` (`none` is allowed on GET).
  - Double-submit CSRF uses the `__Host-webinar_csrf` cookie, issued on the profile GET, plus the `X-Webinar-CSRF` header, compared in constant time.
- Request body checks:
  - JSON content type only. Reset also accepts an empty body.
  - Size is checked against the declared and the streamed length (`PROXY_BODY_LIMIT`, default 32 KiB).
  - The body must be a JSON object and is re-serialised before forwarding. Registrations require `schemaVersion: '1'`.
- Only the configured session cookie is forwarded upstream. Incoming `Authorization`, other cookies and the CSRF header are never forwarded. `X-Forwarded-For` comes from the socket unless `PROXY_TRUST_FORWARDED_FOR=1`.
- Upstream `Set-Cookie` handling:
  - Only the session cookie is kept, and it is rewritten to `Path=/; HttpOnly; Secure; SameSite=Lax` with no `Domain`.
  - Only `Max-Age` and `Expires` survive from the original attributes.
- Upstream response handling:
  - Upstream 5xx, non-JSON or oversized responses become 502. A timeout becomes 504.
  - 4xx bodies are projected to a code plus field keys, so server text and possible PII never reach the browser.
  - Every response is `no-store` and `nosniff`.
- Config fails closed: https backend (http only with an explicit dev flag), key of at least 32 characters, exact origins.

### SDK

Consent and attribution:

- Default consent is denied. Before consent, `attribution` is `{parameters:{}, identifiers:{}}`.
- With **analytics** consent it collects UTMs, GA4 `_ga` → client id, and `pageUrl`/`referrer` stripped to origin + path.
- With **advertising** consent it collects ad click IDs (gclid/gbraid/wbraid/fbclid/twclid/ScCid/rdt_cid; only keys the server allowlist accepts), populated ValueTrack params, and cookies `_gcl_aw`, `_fbp`, `_fbc`, `_twclid`, `_scid`, `_rdt_uuid`.
- Values containing `{`/`}` (unsubstituted macros) are dropped.
- Cookies are read by exact name and format-checked. The cookie string and full URLs are never sent. No fingerprinting.
- First-touch-in-session parameters go to `sessionStorage` only after consent, are cleared on `setConsent` withdrawal, and are re-filtered by current consent at submit.
- The client sends only `granted` / `disclosureVersion` claims, never "verified" flags. SMS consent requires a `data-disclosure-version`.

Form behaviour:

- Anonymous browsers, failed lookups and malformed responses all get the full form.
- For a recognised browser, fields not in `missingFields` are hidden **and disabled**, so they are not submitted and `required` is suspended. With nothing missing, the button becomes the form's `data-confirm-label` (plain text via `textContent`/`value`, whitespace collapsed, max 60 chars) or **Confirm my RSVP** by default; the normal label is the button's own text. The image serves this file unmodified at `/static/webinar-sdk/webinar-form.js` (canonical source: `webinars/static/webinar-sdk/webinar-form.js`; `sdk/src/webinar-form.js` links to it).
- "Not you? Start over" POSTs to reset, then clears the form, shows all fields, rotates the idempotency key and moves focus to the first field.
- No profile values are ever displayed. The welcome text is generic.
- Field handling:
  - Canonical `name` attributes, a fixed snake_case alias list, and explicit `data-field-map` (canonical targets only).
  - Conflicting alias values are rejected client-side.
  - Blank values are omitted. Names are never split.
  - `custom.<key>` fields go to `customFields`. `organization.externalId` requires `data-namespace`.
- Idempotency keys are in memory only and come from a CSPRNG (fails closed without one). A retry of the same intent reuses its key, a changed intent rotates it, and success or reset clears it.
- Failure handling:
  - No success UI on network failure, timeout (15 s) or non-2xx responses.
  - The `role="alert"` region gets focus. Fields get `aria-invalid` and `aria-describedby` messages.
  - Error messages are generic and never echo server text.
- On success:
  - Shows the thank-you message, or redirects only to https or same-origin URLs.
  - Shows an optional calendar link.
  - Pushes a `dataLayer` event with occurrence and form IDs only, and only with analytics consent.

## Gaps and honest limitations

- **Unrun:** none of the tests or the demo have been executed. Expect possible small fixes once the supervisor runs them.
- **No DOM-level tests:**
  - `FormController` (hide/disable, button relabel, reset flow, focus, ARIA) is covered only by the planning and pure-function tests plus the manual demo.
  - jsdom was not used because the work had to stay dependency-free.
  - Recommended: browser or a11y testing (e.g. Playwright + axe) in the supervisor's environment.
- **Backend coupling unverified:** the backend is owned by another workstream. These assumptions need confirmation:
  - Auth header `Authorization: Bearer <proxy key>`.
  - Session cookie name `webinar_session` (configurable via `PROXY_SESSION_COOKIE`).
  - Error body shapes `{error, errors:{field:[…]}}` or `errors:[{field}]`.
  - Success shape `thankYou.{message,redirectUrl}` and `calendar.icsUrl`.
  - The backend trusting `X-Forwarded-For` from the proxy.
- **CSRF cookie name:** the SDK hard-codes `__Host-webinar_csrf`. Changing `PROXY_CSRF_COOKIE` breaks submissions unless the SDK constant changes too.
- **Proxy paths:** fixed at `/webinar-api/*`. A custom SDK `proxyBase` requires a reverse-proxy rewrite.
- **Required form attributes:** forms without `data-occurrence-id`/`data-form-version` cannot obtain a CSRF cookie, so they fail closed with `csrf_failed`.
- **Correction UX:** limited to "Not you? Start over". Blank inputs cannot clear stored values (by design per SPEC). Cross-device recognition is not supported.
- **Redirect and calendar URLs:** any https URL from the backend is accepted. The backend must validate thank-you URLs, as SPEC requires.
- **Consent provider:** with a `consentProvider` function alone, withdrawal does not clear `sessionStorage` until `setConsent`/`setConsentProvider` is called again. Values are still filtered by current consent at submit.
- **No rate limiting in the proxy.** It relies on the edge or backend. The proxy is a single process with no clustering or graceful shutdown handling.
- **Demo:** `Secure`/`__Host-` cookies over `http://localhost` work in Chrome and Firefox. Safari may refuse them. The mock backend is illustrative only and is not the reference backend.
- **No Dockerfile** for the proxy. It is a single stdlib file runnable with any Node >= 18.17 image as a non-root user.
- **Not production-ready:** nothing here has been deployed or load-tested.
