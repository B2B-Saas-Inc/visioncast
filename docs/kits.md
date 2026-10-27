# Website and CMS integration kits

The kits in `sdk/kits/` put webinar RSVP forms and live event data on sites you build yourself. All of them use the same contract as the browser SDK (`docs/browser-integration.md`) and the first-party proxy (`sdk/proxy/server.js`). No kit adds a new backend API.

> **Status.** None of these kits has been production-tested. The kit tests (`sdk/kits/test/kits.test.js`) check invariants only, and they have not been run in the authoring workstream. Every kit's status is listed in `sdk/kits/manifest.json`.

| Kit | Status | Files |
|---|---|---|
| WordPress | **installable plugin** | `sdk/kits/wordpress/webinar-platform/` |
| Proxy mount / nginx | maintained reference | `sdk/kits/node/` |
| Headless seam | maintained reference | `sdk/kits/headless/webinar-events.mjs` |
| Next.js, React | maintained reference | `sdk/kits/frameworks/{react,nextjs}/` |
| Astro | maintained reference | `sdk/kits/frameworks/astro/` |
| Nuxt / Vue | maintained reference | `sdk/kits/frameworks/vue/` |
| SvelteKit | maintained reference | `sdk/kits/frameworks/{svelte,sveltekit}/` |
| Webflow, Framer | instructions | `sdk/kits/nocode/embed-snippet.html` |
| Sanity, Payload, Prismic, Contentful | schema reference | `sdk/kits/cms/` |

## The rules every kit follows

1. **The browser talks only to `/webinar-api/*` on the page's own origin.** These routes are exactly `profile`, `registrations`, `reset`, `my-registration` and `calendar.ics`. The SDK refuses any other `proxyBase`.
   - The CSRF cookie is `__Host-webinar_csrf`.
   - The proxy requires `Sec-Fetch-Site: same-origin`.
   - A proxy on another subdomain does **not** work.
2. **The proxy key stays on a server.** Use a key with the `proxy` scope only: `python manage.py create_api_key --name <site> --scope proxy`.
   - Never put it in a `NEXT_PUBLIC_*`, `PUBLIC_*`, `VITE_*` or `NUXT_PUBLIC_*` variable.
   - Never put it in page markup, a CMS field or a page builder's custom code.
3. **The proxy is not a forwarder.** Upstream paths come from a fixed table, and query values are pattern-checked. Responses are projected the same way `sdk/proxy/server.js` projects them.
4. **The SDK script** is published by the backend at `https://<backend>/static/webinar-sdk/webinar-form.js`, which is what Studio's "Embed on your site" uses. You can also self-host a pinned copy on your own origin.
   - If your CSP restricts `script-src`, add the backend origin (or serve the copy from `'self'`).
   - `connect-src 'self'` is enough, because the SDK only calls `/webinar-api`.
5. **Paid sessions need no kit code.** The backend answers `POST /api/v1/registrations` with `202 {"status":"payment_required","order":{checkoutUrl,…}}` (see `docs/tickets.md`).
   - Both proxies pass that 202 through.
   - The SDK navigates only to `https://checkout.stripe.com/…`. Otherwise it shows "preparing checkout" and keeps the idempotency key, so a resubmit replays the same order.
   - Nobody is registered, and no confirmation or calendar link appears, until the Stripe webhook fulfils the order.
   - Do not add your own "success" UI on submit. Listen for `data-webinar-state="complete"` on the form instead.
6. **CMSs store references, not events.** A CMS entry holds `eventSlug` and an optional pinned `occurrenceId`. Titles, times, hosts, capacity and prices are read live from `GET /api/v1/events/{slug}` (public, no key) on the server.

## Serving /webinar-api

Pick one approach per site.

**A. In-process (Node frameworks you run yourself).** `sdk/kits/node/mount.cjs` wraps your framework's Node handler. It sends `/webinar-api/*` to `createHandler()` from `sdk/proxy/server.js`, which is reused unchanged. Everything else goes to your app.

1. Copy `sdk/kits/node/mount.cjs` to `<app>/webinar-kit/mount.cjs`.
2. Copy `sdk/proxy/server.js` to `<app>/webinar-kit/server.cjs`.
3. Set the proxy's environment variables (see `docs/browser-integration.md` for all of them):
   ```sh
   PROXY_BACKEND_URL=https://webinars.example.com
   PROXY_API_KEY=<proxy-scoped key, >= 32 chars>
   PROXY_ALLOWED_ORIGINS=https://www.example.com
   # PROXY_SESSION_COOKIE=webinar_session   # must equal the backend's CAPABILITY_COOKIE_NAME
   # PROXY_TRUST_FORWARDED_FOR=1            # only behind an edge that OVERWRITES X-Forwarded-For
   ```
4. Start the server through the kit's entry file: `frameworks/nextjs/server.mjs` or `frameworks/sveltekit/server.js`. Bad configuration stops startup, as the standalone proxy does.

**B. Sidecar plus same-origin reverse proxy (any stack).** Run `node sdk/proxy/server.js` on `127.0.0.1:8787` and route the five paths to it. `sdk/kits/node/nginx-webinar-api.conf` is a ready `location` block. It overwrites `X-Forwarded-For` with `$remote_addr`, because the proxy trusts the *first* entry when `PROXY_TRUST_FORWARDED_FOR=1`. Use this approach for Astro, Nuxt and static sites, and in front of Webflow or Framer where your plan allows it.

**C. Serverless Next.js (Vercel).** There is no long-lived Node server, so use the route-handler port in this repository's `site/` (`site/lib/proxy.ts`, `site/app/webinar-api/*`, `site/.env.example`). Those handlers run on the server, and their configuration variables are not exposed to the browser.

Whichever approach you use, exclude `/webinar-api/*` from page caches and CDNs. The responses carry `Cache-Control: no-store`, and some caches still need an explicit rule.

## Headless seam

`sdk/kits/headless/webinar-events.mjs` is server-only and dependency-free (native `fetch`, Node ≥ 18.17).

| Export | Purpose |
|---|---|
| `getWebinarEvent(slug, {env, fetchImpl, timeoutMs})` | Validates the slug (`[-a-zA-Z0-9_]`). Fetches `GET {WEBINAR_API_BASE}/api/v1/events/{slug}` with `redirect: 'error'` and a 5 s timeout. Returns a normalised event (documented public fields only), or `null` on 404. |
| `pickOccurrence(event, pinnedId?)` | Returns the pinned session if it is open, otherwise the next open one, otherwise `null`. Assumes the API lists occurrences in start order. |
| `formProps(event, occurrence)` | Returns `{occurrenceId, formVersion}` for the form components, or `null` when registration is closed. |
| `sdkScriptUrl(env)` | Returns `WEBINAR_SDK_URL` (validated) or `{WEBINAR_API_BASE}/static/webinar-sdk/webinar-form.js`. |
| `backendOrigin(env)` | Accepts only an https origin. http is allowed only for localhost with `WEBINAR_ALLOW_INSECURE_BACKEND=1`. |

`WEBINAR_API_BASE` is not a secret, but keep it server-side anyway so the browser never needs the backend host.

The form components (React, Vue, Svelte, Astro) render only canonical SDK fields: `email`, `firstName`, `lastName`, `name`, `phone`, `company` and `jobTitle`.

- Email is always present and required.
- They validate `occurrenceId` (UUID), `formVersion` and the script URL (https, or a path on this origin), and render nothing if any of these is invalid.
- They load the SDK once, with `autoInit: false`, and call `WebinarForm.init(container)` after render.
- For `custom.*` fields, SMS consent, selects or `data-field-map`, use Studio's "Embed on your site" markup, or extend the component's field table.
- Fields in the session's form version that the component does not render make submissions fail validation. Match the form version.

**Consent.** Set `window.WebinarFormConfig = { consent: {...} }` or `consentProvider` before the component mounts, or call `WebinarForm.setConsent()` from your CMP. The default is denied.

## WordPress

**Status: installable plugin** (`sdk/kits/wordpress/webinar-platform/`). Requires WordPress ≥ 6.3 and PHP ≥ 7.4. It uses only WordPress APIs: the Settings API, `wp_remote_request` and `register_block_type`.

**What it contains:**

- `includes/class-proxy.php`: a PHP port of `sdk/proxy/server.js`. It handles the same five routes and the same checks:
  - exact Origin and `Sec-Fetch-Site`
  - `__Host-webinar_csrf` double-submit compared with `hash_equals`
  - JSON content type, a 32 KiB body limit and `schemaVersion: "1"`
  - projected profile, error and my-registration bodies, with the ICS link rewritten
  - the session cookie re-issued `HttpOnly; Secure; SameSite=Lax`
  - no redirects followed, and upstream bodies capped at 256 KiB.

  It parses the raw `Cookie` header and query string, not `$_COOKIE` or `$_GET`, so values are forwarded byte-for-byte. It runs on `parse_request` and sets `DONOTCACHEPAGE`. It does not use the REST API.
- `includes/class-settings.php`: **Settings → Webinar Platform**.
  - Requires `manage_options`. Saves go through `options.php`, which checks the Settings API nonce.
  - Both options are registered with `show_in_rest => false`.
  - The key field is write-only: a blank submission keeps the stored key, and the page never displays it.
  - The key option is not autoloaded. A `WEBINAR_PLATFORM_PROXY_KEY` constant or environment variable overrides the stored key and is preferred.
  - The backend must be an https origin. http is allowed only when `WP_ENVIRONMENT_TYPE` is `local` or `development`.
- `includes/class-form.php`: the dynamic block `webinar-platform/webinar-form` and the `[webinar_form]` shortcode.
  - Every attribute is validated (UUID, form-version pattern, field allowlist, labels of up to 60 characters of plain text) and escaped on output.
  - The SDK is enqueued once, in the footer, with `proxyBase` pinned to this site's `/webinar-api`. The `webinar_platform_sdk_config` filter cannot override it.
- `blocks/webinar-form/`: `block.json`, plus an ES5 editor script on the `wp.*` globals (no build step), plus `index.asset.php`.

**Setup:**

1. Copy `webinar-platform/` to `wp-content/plugins/` and activate it.
2. On the backend, run `python manage.py create_api_key --name wordpress --scope proxy`.
3. In `wp-config.php`, add `define( 'WEBINAR_PLATFORM_PROXY_KEY', '<key>' );`.
4. In Settings → Webinar Platform, set the Backend URL. Add extra origins only if the site has more than one hostname.
5. Enable pretty permalinks, or make sure nginx's `try_files … /index.php?$args` sends `/webinar-api/*` to WordPress. Exclude `/webinar-api/*` from page and CDN caches.
6. Add the **Webinar registration form** block, or use the shortcode:
   `[webinar_form occurrence="<uuid>" form_version="v1" fields="email,firstName,lastName,company" required="email"]`.
   Get the occurrence UUID and form version from Studio → session → Embed on your site.
7. Test the form:
   1. Submit once and confirm that the thank-you message and calendar links appear.
   2. Reload the page. Recognised fields should be hidden, and the button should read "Confirm my RSVP".
   3. Use "Not you? Start over".
   4. For a paid session, check that you are sent to Stripe Checkout and that no confirmation is shown.

**Lint without running WordPress:**

```sh
for f in $(find sdk/kits/wordpress -name '*.php'); do php -l "$f"; done
# optional, needs network + Composer: phpcs --standard=WordPress sdk/kits/wordpress
```

**Subdirectory installs:** the proxy and `proxyBase` follow the `home_url()` path (for example `/blog/webinar-api`). The CSRF and session cookies use `Path=/`.

**Client IP:** the plugin sends `REMOTE_ADDR` by default. Behind a trusted edge that *overwrites* `X-Forwarded-For`, return that value from the `webinar_platform_client_ip` filter.

## Next.js

**Status: maintained reference.**

1. Copy the files:
   - `frameworks/react/WebinarForm.jsx` to `components/WebinarForm.jsx` (it starts with `'use client'`).
   - `headless/webinar-events.mjs` to `lib/webinar-events.mjs`.
   - `frameworks/nextjs/app/webinars/[slug]/page.jsx` to the same path in your app.
2. Server-only environment: `WEBINAR_API_BASE=https://webinars.example.com`. Optionally set `WEBINAR_SDK_URL`.
3. Serve `/webinar-api`:
   - Self-hosted: use approach A with `frameworks/nextjs/server.mjs`, then `next build && NODE_ENV=production node server.mjs`.
   - Vercel: use approach C (`site/`).
4. If you use the strict CSP from `site/next.config.ts`, add the backend origin to `script-src`, or self-host the SDK and set `WEBINAR_SDK_URL=/webinar-form.js`.
5. Visit `/webinars/<slug>` and run the end-to-end test from the WordPress steps.

The page is ISR-cached for catalogue data only. The form's state always comes from `/webinar-api/profile` in the browser.

## React

**Status: maintained reference.** `frameworks/react/WebinarForm.jsx` works in any React 18+ app (Vite, Remix, CRA).

```jsx
<WebinarForm occurrenceId={id} formVersion="v1" sdkSrc="https://webinars.example.com/static/webinar-sdk/webinar-form.js" />
```

Resolve `occurrenceId` and `formVersion` on your server through the headless seam, or take them from Studio. A Vite dev server can proxy `/webinar-api` to a local sidecar with `server.proxy`. In production, use approach A or B.

Avoid re-rendering the form with new props after mount: the SDK manages `hidden` and `disabled` on its fields. A change of session re-keys the form.

## Astro

**Status: maintained reference.** Copy `frameworks/astro/WebinarForm.astro` to `src/components/`, and copy `webinar-events.mjs` to `src/lib/`.

The component server-renders the markup and injects the SDK once (`is:inline`, `define:vars`). It re-binds forms on `astro:page-load` for view transitions. Serve `/webinar-api` with approach B. For approach A, use `@astrojs/node` in `mode: 'middleware'` and wrap its `handler` with `withWebinarProxy`. In middleware mode you must still serve `dist/client/` yourself; this repository has not verified that setup.

## Nuxt / Vue

**Status: maintained reference.** Copy `frameworks/vue/WebinarForm.vue`. It is a `<script setup>` SFC for Vue 3 and Nuxt 3. The SDK loads in `onMounted`, which runs only in the browser.

In Nuxt, call `getWebinarEvent` from a server route (`server/api/webinar/[slug].get.js`) or from `useAsyncData` on the server. Keep `WEBINAR_API_BASE` in server-only `runtimeConfig`, never in `runtimeConfig.public`.

Serve `/webinar-api` with approach B. Nitro `routeRules` proxying is convenient in development, but this repository has not verified how it handles `X-Forwarded-For` and `Set-Cookie`, so do not rely on it in production without checking.

## SvelteKit

**Status: maintained reference.**

1. Copy `frameworks/svelte/WebinarForm.svelte`. It uses legacy `export let` syntax, which works in Svelte 4 and 5.
2. Load the event in `+page.server.js` through the headless seam.
3. With `@sveltejs/adapter-node`, copy `frameworks/sveltekit/server.js` and the two `webinar-kit/` files (approach A).
4. Run `npm run build && node server.js`.

## Webflow

**Status: instructions.** Webflow details have not been verified against current Webflow plans.

Webflow hosting runs no server code, so the proxy must come from **your own same-origin reverse proxy** in front of the Webflow site. Approach B routes `/webinar-api/*` to the sidecar and everything else to Webflow. Whether Webflow supports or permits a customer reverse proxy depends on your plan; check with Webflow. If you cannot serve `/webinar-api` on the same origin, do not embed the form. Link to a registration page on a site that does serve the proxy.

1. Page settings → Custom code → *Before `</body>`*: paste the two `<script>` tags from `nocode/embed-snippet.html`, with your backend host.
2. Add an **Embed** element (not a Webflow Form block) and paste the `<form>`. Use Studio's "Embed on your site" output for exact fields.
3. Publish. Then check in DevTools that `GET /webinar-api/profile?...` returns 200 from your domain and sets `__Host-webinar_csrf`.
4. Run the end-to-end test from the WordPress steps.

## Framer

**Status: instructions.** Framer details have not been verified.

The same-origin proxy requirement is the same as for Webflow: use approach B in front of the Framer domain, if your plan permits a reverse proxy.

- **Option 1 (markup):** Site settings → Custom code → end of `<body>`: add the script tags. Add the `<form>` through an Embed/HTML component. Framer may render HTML embeds inside an iframe, and inside an iframe the SDK will not find the form. If it does, use option 2.
- **Option 2 (code component):** paste `frameworks/react/WebinarForm.jsx` as a code component (default export). Set `occurrenceId`, `formVersion` and `sdkSrc` as props. Property controls are not included.

## Sanity

**Status: schema reference.** Register `cms/sanity/webinarReference.js` in `schema.types`, then add `defineField({ name: 'webinar', type: 'webinarReference' })` to a document. Slug and UUID patterns are validated in the Studio.

At render time, pass the stored object to `resolveWebinarReference(doc.webinar)` from `cms/resolve-reference.mjs`. Fetching the document is up to your existing GROQ client; this kit adds no Sanity client code.

## Payload

**Status: schema reference.** Add `webinarReference()` from `cms/payload/webinarReference.ts` to a collection's `fields`. It is a group of `eventSlug` (required, validated) and `occurrenceId` (optional UUID). The type import targets Payload 3; Payload 2 uses `payload/types`. Resolve with `resolveWebinarReference(doc.webinar)`.

## Prismic

**Status: schema reference.** Copy `cms/prismic/webinar_reference/model.json` into your Slice Machine library as the `WebinarReference` shared slice, then add a slice component. Prismic Text fields cannot validate patterns, so the resolver validates instead: invalid values resolve to `null`, and the slice should render nothing. Pass the slice (`{primary: {event_slug, occurrence_id}}`) to `resolveWebinarReference(slice)`. The `docURL` placeholder and Slice Machine version details are unverified.

## Contentful

**Status: schema reference.** `cms/contentful/webinar-reference.import.json` is in the `contentful space import` format: content type `webinarReference` with Symbol fields `eventSlug` and `occurrenceId`, both regexp-validated. Import it yourself, or create the same fields in the web app; the kit makes no remote calls. Then reference the entry from your page type, and pass the delivery entry (`{fields: {eventSlug, occurrenceId}}`) to `resolveWebinarReference(entry)`. Check the file against your `contentful-cli` version before importing.

## Tests

```sh
node --test sdk/kits/test/kits.test.js                          # manifest, route parity, secret scan, PHP guards, mount, headless seam
node --test sdk/test/proxy.test.js sdk/test/webinar-form.test.js # existing SDK + proxy suites (unchanged)
```

The kit tests use no network: `fetch` is injected, and the proxy mount is called with fake request and response objects.
