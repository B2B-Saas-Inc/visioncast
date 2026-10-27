# Website and CMS integration kits

The kits in `sdk/kits/` put webinar RSVP forms and live event data on sites you build yourself. All of them use the same contract as the browser SDK (`docs/browser-integration.md`) and the first-party proxy (`sdk/proxy/server.js`). No kit adds a new backend API.

> **Status.** None of these kits has been production-tested (`productionTested: false` in `sdk/kits/manifest.json`). On 2026-10-27 every kit was installed from a fresh vendor scaffold against the real backend and driven with Chromium; what was exercised, with versions, is in each kit's `verified` entry in the manifest and in `docs/validation.md`. The run is repeatable with `bash sdk/kits/test/fresh-install.sh` (Docker, npm registry access and Playwright; see the script header). `sdk/kits/test/kits.test.js` checks invariants only and runs in `node --test`.

| Kit | Status | Fresh-install result (2026-10-27) | Files |
|---|---|---|---|
| WordPress | **installable plugin** | WordPress 7.1.3 / PHP 8.2: block, shortcode, admin, WP Super Cache bypass, `/blog/` subdirectory install | `sdk/kits/wordpress/webinar-platform/` |
| Proxy mount / nginx | maintained reference | in-process with Next and SvelteKit; nginx 1.27 block fronts Astro, Nuxt and a static page | `sdk/kits/node/` |
| Headless seam | maintained reference | used server-side by all four framework pages | `sdk/kits/headless/webinar-events.mjs` |
| Next.js | maintained reference | Next 16.4 (App Router, `cacheComponents`), full flow | `sdk/kits/frameworks/{react,nextjs}/` |
| React (Vite) | maintained reference | Vite 8 build + mount smoke | `sdk/kits/frameworks/react/` |
| Astro | maintained reference | Astro 7.4 SSR (`@astrojs/node`) behind nginx, full flow | `sdk/kits/frameworks/astro/` |
| Nuxt / Vue | maintained reference | Nuxt 4.6 behind nginx, full flow | `sdk/kits/frameworks/vue/` |
| SvelteKit | maintained reference | SvelteKit 3.1 / Svelte 5 (runes) with adapter-node, full flow | `sdk/kits/frameworks/{svelte,sveltekit}/` |
| Webflow, Framer | instructions | snippet verified as a static page behind nginx; the vendors' hosting was not used | `sdk/kits/nocode/embed-snippet.html` |
| Sanity, Payload, Prismic, Contentful | schema reference | validated with each vendor's package (no CMS instance) | `sdk/kits/cms/` |
| Zapier (webhook verification) | maintained reference | Node simulation of the Code step on backend-signed fixtures; no Zap created | `sdk/kits/automation/zapier/` |
| Make (webhook verification) | instructions | Node simulation of the blueprint's expressions; no scenario imported | `sdk/kits/automation/make/` |
| n8n (webhook verification) | maintained reference | Node simulation of the workflow's nodes; no n8n instance used | `sdk/kits/automation/n8n/` |

The three automation kits are the receiving side of the signed webhooks in
[webhooks.md](webhooks.md#workflow-automation-platforms), not registration forms; they use
no proxy route.

"Full flow" means the 30-check browser scenario in `sdk/kits/test/fresh-install/flow.cjs`: profile and CSRF cookie, anonymous form, consent denied by default, registration, thank-you and calendar links, recognised reload with "Confirm my RSVP", "Not you? Start over", `WebinarForm.setConsent`, and 404/405/400 proxy hardening.

## The rules every kit follows

1. **The browser talks only to `/webinar-api/*` on the page's own origin.** These routes are exactly `profile`, `registrations`, `reset`, `my-registration` and `calendar.ics`. The SDK refuses any other `proxyBase`.
   - The CSRF cookie is `__Host-webinar_csrf`, set with `Secure`. Browsers store it only in a secure context: **https**, or `http://localhost` / `http://127.0.0.1` (Chromium and Firefox treat those as secure, which is why the local runs below work over plain http). On any other http host the cookie is silently dropped, `GET /webinar-api/profile` still returns 200, and every POST then fails with `403 csrf_failed`. Local testing on a LAN hostname needs a local TLS proxy.
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

   Local development against an http backend needs two more variables, one per layer: `PROXY_ALLOW_INSECURE_BACKEND=1` for the proxy and `WEBINAR_ALLOW_INSECURE_BACKEND=1` for the headless seam (localhost only). The SDK script URL must be https or a path, so with an http backend set `WEBINAR_SDK_URL=/webinar-form.js` and copy `webinars/static/webinar-sdk/webinar-form.js` into your static directory (`public/`, `static/`). Verified this way with Next 16.4 and SvelteKit 3.1.

**B. Sidecar plus same-origin reverse proxy (any stack).** Run `node sdk/proxy/server.js` on `127.0.0.1:8787` and route the five paths to it. `sdk/kits/node/nginx-webinar-api.conf` is a ready `location` block. It overwrites `X-Forwarded-For` with `$remote_addr`, because the proxy trusts the *first* entry when `PROXY_TRUST_FORWARDED_FOR=1`. Use this approach for Astro, Nuxt and static sites, and in front of Webflow or Framer where your plan allows it. Verified with nginx 1.27.5 in front of Astro 7.4, Nuxt 4.6 and a static page: `include` the file inside your `server {}` block and run `nginx -t`. The catch-all `location /webinar-api/ { return 404; }` deliberately has no `^~`: with it, nginx never evaluated the regex route block and every proxy route returned 404 (that was the shipped bug; `kits.test.js` now guards it).

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
- The form version's **required** fields must all be rendered, or submissions fail validation (`400`, highlighted fields). Optional fields may be omitted (verified: a form with an optional `company` accepted a registration from the three-field snippet), but every optional field you leave out stays in `missingFields`, so a returning visitor sees "Welcome back. We only need the details below." instead of the "Confirm my RSVP" button. To reach confirm-only, render every field of the form version.

**Consent.** Set `window.WebinarFormConfig = { consent: {...} }` or `consentProvider` before the component mounts, or call `WebinarForm.setConsent()` from your CMP. The default is denied: verified on every kit that a first submission with `?utm_source=...` in the URL sends `consents.analytics.granted: false` and an empty `attribution`, and that after `WebinarForm.setConsent({ analytics: true })` the next submission carries `granted: true` and `attribution.parameters.utm_source`. The framework components set `window.WebinarFormConfig` themselves only to pin `proxyBase` and `autoInit: false`; they merge any `consent`/`consentProvider` you set earlier.

## WordPress

**Status: installable plugin** (`sdk/kits/wordpress/webinar-platform/`). Requires WordPress ≥ 6.3 and PHP ≥ 7.4. It uses only WordPress APIs: the Settings API, `wp_remote_request` and `register_block_type`.

**Verified 2026-10-27** on WordPress 7.1.3 / PHP 8.2.35 / MariaDB 11.8.9 (official Docker images, wp-cli 2.12.0), Chromium 147: `php -l` on every file; activation; block page and `[webinar_form]` page each pass the full browser flow; the settings page (key never echoed, backend URL with a path rejected and the old value kept, Settings-API save, options absent from `/wp-json/wp/v2/settings`); block registered in the editor; WP Super Cache 3.1.4 caches pages but not `/webinar-api/*` (fresh CSRF token on every call, no cache files); a `/blog/` subdirectory install. Two bugs were fixed during the run: the reset response's `webinar_session=""` expiry cookie was dropped (the revoked cookie lingered in the browser), and a self-hosted SDK path was emitted as `/blog/blog/…` on subdirectory installs. Not verified: https hosting, nginx-fronted WordPress, multisite, paid sessions.

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

1. Copy `webinar-platform/` to `wp-content/plugins/` and activate it (wp-cli: `wp plugin activate webinar-platform`).
2. On the backend, run `python manage.py create_api_key --name wordpress --scope proxy`.
3. In `wp-config.php`, add `define( 'WEBINAR_PLATFORM_PROXY_KEY', '<key>' );`. The settings page then shows "Set by WEBINAR_PLATFORM_PROXY_KEY" and no key field.
4. In Settings → Webinar Platform, set the Backend URL (an https origin; `http://…` is accepted only with `define( 'WP_ENVIRONMENT_TYPE', 'local' )` or `development`). Add extra origins only if the site has more than one hostname. The same values can be written with wp-cli: `wp option update webinar_platform_settings --format=json '{"backend_url":"https://webinars.example.com","sdk_url":"","allowed_origins":[],"session_cookie":"webinar_session","timeout":10}'` (this bypasses the page's validation, so only use values the page would accept).
5. Enable pretty permalinks (`wp rewrite structure '/%postname%/'`), or make sure nginx's `try_files … /index.php?$args` sends `/webinar-api/*` to WordPress. The official Docker image ships an `.htaccess` that does this at the document root. Exclude `/webinar-api/*` from page and CDN caches; the proxy also defines `DONOTCACHEPAGE` and sends `Cache-Control: no-store`, which WP Super Cache honours without extra rules.
6. Add the **Webinar registration form** block, or use the shortcode:
   `[webinar_form occurrence="<uuid>" form_version="v1" fields="email,firstName,lastName,company" required="email"]`.
   Get the occurrence UUID and form version from Studio → session → Embed on your site. The form version must be the one attached to the session's event, or `/webinar-api/profile` returns `400` (`formVersion: Mismatch`) and the full form is shown.
7. Test the form:
   1. Submit once and confirm that the thank-you message and calendar links appear.
   2. Reload the page. Recognised fields should be hidden, and the button should read "Confirm my RSVP" (only when the block renders every field of the form version; otherwise it reads "Register" under "Welcome back. We only need the details below.").
   3. Use "Not you? Start over". The full form returns and the `webinar_session` cookie is cleared.
   4. For a paid session, check that you are sent to Stripe Checkout and that no confirmation is shown (not exercised by the fresh-install run).

**Lint without running WordPress:**

```sh
for f in $(find sdk/kits/wordpress -name '*.php'); do php -l "$f"; done
# or without local PHP: docker run --rm -v "$PWD/sdk/kits/wordpress/webinar-platform:/p:ro" php:8.2-cli sh -c 'for f in $(find /p -name "*.php"); do php -l "$f"; done'
# optional, needs network + Composer: phpcs --standard=WordPress sdk/kits/wordpress
```

**Subdirectory installs:** the proxy and `proxyBase` follow the `home_url()` path (for example `/blog/webinar-api`); the root-level `/webinar-api/*` is not served. The CSRF and session cookies use `Path=/`. A self-hosted SDK path in the settings is resolved against `home_url()` as well, so enter `/webinar-form.js` for a file at `<home>/webinar-form.js` (entering `/blog/webinar-form.js` would double the prefix). WordPress itself needs the usual subdirectory `.htaccess` (`RewriteBase /blog/` and `RewriteRule . /blog/index.php`); the Docker image's default one rewrites to `/index.php` and returns 404 for every pretty permalink until it is replaced.

**Local testing in Docker:** the browser and the WordPress container see the backend at different hostnames (`host.docker.internal` versus `127.0.0.1`), and the backend URL also determines the default SDK URL. Either self-host the SDK (`sdk_url` = `/webinar-form.js` plus a copy of `webinars/static/webinar-sdk/webinar-form.js` in the WordPress directory) or put both behind one hostname. Over plain http the `__Host-` CSRF cookie is only stored for `localhost`/`127.0.0.1` (see rule 1).

**Client IP:** the plugin sends `REMOTE_ADDR` by default. Behind a trusted edge that *overwrites* `X-Forwarded-For`, return that value from the `webinar_platform_client_ip` filter.

## Next.js

**Status: maintained reference.** Verified 2026-10-27 with `npx create-next-app@latest --js --app` (Next 16.4.3, React 19.3, `cacheComponents` on by default), approach A, full browser flow on `http://localhost:3000/webinars/<slug>`.

1. Copy the files:
   - `frameworks/react/WebinarForm.jsx` to `components/WebinarForm.jsx` (it starts with `'use client'`).
   - `headless/webinar-events.mjs` to `lib/webinar-events.mjs`.
   - `frameworks/nextjs/app/webinars/[slug]/page.jsx` to the same path in your app.
   - For approach A: `node/mount.cjs` to `webinar-kit/mount.cjs`, `sdk/proxy/server.js` to `webinar-kit/server.cjs`, and `frameworks/nextjs/server.mjs` to the project root.
2. Server-only environment: `WEBINAR_API_BASE=https://webinars.example.com`. Optionally set `WEBINAR_SDK_URL`.
3. Serve `/webinar-api`:
   - Self-hosted: use approach A, then `next build && NODE_ENV=production node server.mjs` with `PROXY_*` set.
   - Vercel: use approach C (`site/`).
4. If you use the strict CSP from `site/next.config.ts`, add the backend origin to `script-src`, or self-host the SDK and set `WEBINAR_SDK_URL=/webinar-form.js`.
5. Visit `/webinars/<slug>` and run the end-to-end test from the WordPress steps.

The page exports no route segment config. Next 16 enables Cache Components in new projects and rejects `export const revalidate` (the original kit page failed `next build` with "Route segment config "revalidate" is not compatible with `nextConfig.cacheComponents`"), so the catalogue read runs inside a `<Suspense>` boundary and the page is rendered per request on every Next version. Add `"use cache"` + `cacheLife()` around `getWebinarEvent` yourself if you want catalogue caching on Next 16. The form's state always comes from `/webinar-api/profile` in the browser.

## React

**Status: maintained reference.** `frameworks/react/WebinarForm.jsx` works in any React 18+ app (Vite, Remix, CRA). Verified 2026-10-27 as a build + mount smoke in a fresh `npm create vite@latest -- --template react` app (Vite 8.3, React 19.3); the component's browser behaviour was exercised through the Next.js kit, which uses the same file.

```jsx
<WebinarForm occurrenceId={id} formVersion="v1" sdkSrc="https://webinars.example.com/static/webinar-sdk/webinar-form.js" />
```

Resolve `occurrenceId` and `formVersion` on your server through the headless seam, or take them from Studio. A Vite dev server can proxy `/webinar-api` to a local sidecar with `server.proxy`. In production, use approach A or B.

Avoid re-rendering the form with new props after mount: the SDK manages `hidden` and `disabled` on its fields. A change of session re-keys the form.

## Astro

**Status: maintained reference.** Verified 2026-10-27 with `npm create astro@latest -- --template minimal` (Astro 7.4.2), `npx astro add node` (standalone, 11.1.9), an SSR page `src/pages/webinars/[slug].astro` with `export const prerender = false`, `astro build`, `node dist/server/entry.mjs` behind nginx with the sidecar (approach B); full browser flow on `http://localhost:8081`, SDK script present exactly once.

Copy `frameworks/astro/WebinarForm.astro` to `src/components/`, and copy `webinar-events.mjs` to `src/lib/`. The page reads the event with the headless seam and returns `new Response(null, { status: 404 })` for unknown slugs.

The component server-renders the markup and injects the SDK once (`is:inline`, `define:vars`). It re-binds forms on `astro:page-load` for view transitions (not exercised). Serve `/webinar-api` with approach B. For approach A, use `@astrojs/node` in `mode: 'middleware'` and wrap its `handler` with `withWebinarProxy`. In middleware mode you must still serve `dist/client/` yourself; this repository has not verified that setup.

## Nuxt / Vue

**Status: maintained reference.** Verified 2026-10-27 with `npx create-nuxt@latest --template minimal` (Nuxt 4.6.3, Vue 3.5.44; the `app/` directory layout), `nuxt build`, `node .output/server/index.mjs` behind nginx with the sidecar (approach B); full browser flow on `http://localhost:8082`. Nuxt 3 was not scaffolded; the component and server route use nothing Nuxt 4-specific apart from the `app/` paths.

Copy `frameworks/vue/WebinarForm.vue` to `app/components/` (Nuxt 4) or `components/` (Nuxt 3). It is a `<script setup>` SFC for Vue 3. The SDK loads in `onMounted`, which runs only in the browser.

In Nuxt, call `getWebinarEvent` from a server route (`server/api/webinar/[slug].get.js`, importing the seam from `server/utils/`) and read it in the page with `useFetch`, as the fresh-install script does; or call it from `useAsyncData` on the server. Keep `WEBINAR_API_BASE` in server-only `runtimeConfig` or the process environment, never in `runtimeConfig.public`.

Serve `/webinar-api` with approach B. Nitro `routeRules` proxying is convenient in development, but this repository has not verified how it handles `X-Forwarded-For` and `Set-Cookie`, so do not rely on it in production without checking.

## SvelteKit

**Status: maintained reference.** Verified 2026-10-27 with `npx sv@latest create --template minimal --no-types --no-add-ons` (SvelteKit 3.1.0, Svelte 5.57.3) plus `@sveltejs/adapter-node` 6.0.0, approach A; full browser flow on `http://localhost:3001/webinars/<slug>`.

1. Copy `frameworks/svelte/WebinarForm.svelte` to `src/lib/`. It is written in Svelte 5 runes syntax (`$props`, `$derived`, `$effect`): a fresh `sv create` project compiles every project file with `runes: true` (in `vite.config.js`), where the legacy `export let` / `$:` syntax is a compile error, so the earlier legacy version did not build. For Svelte 4, make the three edits described at the top of the file.
2. Load the event in `src/routes/webinars/[slug]/+page.server.js` through the headless seam and pass `data.form` plus `data.sdkSrc` to the component from `+page.svelte` (`let { data } = $props()`).
3. With `@sveltejs/adapter-node`, copy `frameworks/sveltekit/server.js` to the project root and the two `webinar-kit/` files (approach A). In a `sv create` project the adapter is configured inside `vite.config.js` (`sveltekit({ adapter: adapter() })`), not `svelte.config.js`: replace `@sveltejs/adapter-auto` with `@sveltejs/adapter-node` there.
4. Run `npm run build && ORIGIN=https://www.example.com node server.js` with `PROXY_*` set (`ORIGIN` is adapter-node's own setting for form actions and redirects).

## Webflow

**Status: instructions.** Webflow hosting itself was not used. What was verified on 2026-10-27: `nocode/embed-snippet.html`, with the two placeholders replaced and the SDK self-hosted, served as a plain static page behind the nginx approach-B front passes the browser flow (28 checks; the snippet's three fields leave the form's optional `company` uncovered, so a returning visitor sees "Welcome back…" rather than "Confirm my RSVP"; add the field if your form has it). Whether Webflow permits a customer reverse proxy, and how its Embed element and custom code behave, remain unverified.

Webflow hosting runs no server code, so the proxy must come from **your own same-origin reverse proxy** in front of the Webflow site. Approach B routes `/webinar-api/*` to the sidecar and everything else to Webflow. Whether Webflow supports or permits a customer reverse proxy depends on your plan; check with Webflow. If you cannot serve `/webinar-api` on the same origin, do not embed the form. Link to a registration page on a site that does serve the proxy.

1. Page settings → Custom code → *Before `</body>`*: paste the two `<script>` tags from `nocode/embed-snippet.html`, with your backend host.
2. Add an **Embed** element (not a Webflow Form block) and paste the `<form>`. Use Studio's "Embed on your site" output for exact fields.
3. Publish. Then check in DevTools that `GET /webinar-api/profile?...` returns 200 from your domain and sets `__Host-webinar_csrf`.
4. Run the end-to-end test from the WordPress steps.

## Framer

**Status: instructions.** Framer hosting itself was not used; the snippet evidence is the same static-page run as for Webflow, and the React component (option 2) builds under Vite 8 / React 19. Framer's custom-code, HTML-embed (iframe) and code-component behaviour remain unverified.

The same-origin proxy requirement is the same as for Webflow: use approach B in front of the Framer domain, if your plan permits a reverse proxy.

- **Option 1 (markup):** Site settings → Custom code → end of `<body>`: add the script tags. Add the `<form>` through an Embed/HTML component. Framer may render HTML embeds inside an iframe, and inside an iframe the SDK will not find the form. If it does, use option 2.
- **Option 2 (code component):** paste `frameworks/react/WebinarForm.jsx` as a code component (default export). Set `occurrenceId`, `formVersion` and `sdkSrc` as props. Property controls are not included.

## Sanity

**Status: schema reference.** Verified 2026-10-27 with the `sanity` package 6.20.0: `createSchema()` compiles `webinarReference` plus a document that uses it with no validation problems, and the field rules resolve to required + regex (`eventSlug`) and regex (`occurrenceId`) and reject `bad slug` / `nope`. No Studio was run. The file is ESM (`import … from 'sanity'`), as Studio projects are; it will not load under CommonJS.

Register `cms/sanity/webinarReference.js` in `schema.types`, then add `defineField({ name: 'webinar', type: 'webinarReference' })` to a document. Slug and UUID patterns are validated in the Studio.

At render time, pass the stored object to `resolveWebinarReference(doc.webinar)` from `cms/resolve-reference.mjs`. Fetching the document is up to your existing GROQ client; this kit adds no Sanity client code.

## Payload

**Status: schema reference.** Verified 2026-10-27: `tsc --strict` (TypeScript 6.0.3) type-checks `webinarReference()` as a `Field` from `payload` 3.92.1 inside a `CollectionConfig` (`sdk/kits/test/fresh-install/payload-check.ts`). No Payload admin was run.

Add `webinarReference()` from `cms/payload/webinarReference.ts` to a collection's `fields`. It is a group of `eventSlug` (required, validated) and `occurrenceId` (optional UUID). The type import targets Payload 3; Payload 2 uses `payload/types`. Resolve with `resolveWebinarReference(doc.webinar)`.

## Prismic

**Status: schema reference.** Verified 2026-10-27: `model.json` parses with `SharedSliceModelSchema` from `@prismicio/types-internal` 4.8.0 (`/zod4`), the codec Slice Machine uses for shared-slice models. Slice Machine itself was not run.

Copy `cms/prismic/webinar_reference/model.json` into your Slice Machine library as the `WebinarReference` shared slice, then add a slice component. Prismic Text fields cannot validate patterns, so the resolver validates instead: invalid values resolve to `null`, and the slice should render nothing. Pass the slice (`{primary: {event_slug, occurrence_id}}`) to `resolveWebinarReference(slice)`. The `docURL` placeholder and Slice Machine version details are unverified.

## Contentful

**Status: schema reference.** `cms/contentful/webinar-reference.import.json` is in the `contentful space import` format: content type `webinarReference` with Symbol fields `eventSlug` and `occurrenceId`, both regexp-validated. Verified 2026-10-27: the file passes the Joi payload check (`assertPayload`) that `contentful-import` 10.5.7 runs before any API call, and both patterns accept a slug/UUID and reject junk. No space import was run (it needs a space and a management token). Import it yourself, or create the same fields in the web app; the kit makes no remote calls. Then reference the entry from your page type, and pass the delivery entry (`{fields: {eventSlug, occurrenceId}}`) to `resolveWebinarReference(entry)`.

## Zapier

**Status: maintained reference.** `automation/zapier/verify-step.js` is a *Code by Zapier* (JavaScript) step that verifies one platform delivery after a *Catch Raw Hook* trigger: HMAC-SHA256 over `<delivery id>.<timestamp>.<raw body>` with Node's `crypto`, constant-time compare (rotation header accepted), 300 s timestamp window, dedupe on `Webinar-Delivery-Id` through *StoreClient* (one pruned key), and the parsed payload plus lifted fields (`email`, `eventTitle`, `sessionStartsAt`, …) as output. Verified 2026-10-27 only by the Node simulation in `automation/test/automation.test.js` (fixtures signed with `examples/verify_webhook.py`); no Zap was created, and the raw hook's field names in the mapping picker are unverified. Set-up, Input Data table and limits (secret in Input Data, Storage limits, Zapier's 200-regardless response): `automation/zapier/README.md`. Platform side: destination with preset **Zapier** (`docs/webhooks.md`).

## Make

**Status: instructions.** `automation/make/blueprint.json` is a scenario (custom webhook with JSON pass-through and request headers → set variables → `v1={{sha256(id + "." + ts + "." + body; "hex"; secret)}}` → router: `contains(signature; expected)` and `abs(timestamp - ts) <= 300` → Parse JSON → router on `eventType`; rejected requests get a 401 *Webhook response*). The expressions are evaluated by `automation/make/simulate.js` in the Node test; the blueprint's module/filter structure follows Make's export format from memory and is unverified on a Make account, as is the raw-body item name. Dedupe with a Data store keyed by the delivery id (README). Platform side: preset **Make**; the `hook.<zone>.make.com` host must be allowlisted.

## n8n

**Status: maintained reference.** `automation/n8n/workflow.json` imports a chain Webhook (Raw Body) → Extract from File → Crypto *Hmac* SHA256 hex (secret from a Crypto credential) → IF (signature contains `v1=<hmac>`, age ≤ 300 s) → Remove Duplicates on `Webinar-Delivery-Id` → Switch on `eventType`. `automation/n8n/simulate.js` replays each node's documented behaviour in the Node test, and the test pins the workflow's expressions and node types to it. No n8n instance was used; node parameter names were taken from the n8n docs and source (2026-10-27) and may need adjusting on your version. Platform side: preset **n8n**; the production URL only (`/webhook-test/` is refused), host an exact allowlist entry or `*.app.n8n.cloud`.

## Tests

```sh
node --test sdk/kits/automation/test/automation.test.js         # Zapier code step, Make and n8n simulations on backend-signed fixtures
node --test sdk/kits/test/kits.test.js                          # manifest, route parity, secret scan, PHP guards, mount, headless seam, fresh-install findings
node --test sdk/test/proxy.test.js sdk/test/webinar-form.test.js # SDK + proxy suites (proxy: quoted cookie values added)
bash sdk/kits/test/fresh-install.sh [stage]                     # real scaffolds + backend + Chromium; Docker and network; not run by node --test
```

The kit tests use no network: `fetch` is injected, and the proxy mount is called with fake request and response objects. They also pin the findings of the fresh-install run (nginx location precedence, runes syntax, no Next segment config, quoted `Set-Cookie` values in both proxies, the WordPress SDK path resolution, and a `verified` entry per kit in the manifest).

`fresh-install.sh` repeats the 2026-10-27 run stage by stage (`backend`, `wordpress`, `next`, `sveltekit`, `approach-b`, `react`, `cms`, `down`). Its header lists the prerequisites; everything it creates is prefixed `wk-` and removed by `down`. The helpers in `sdk/kits/test/fresh-install/` are the Playwright flow (`flow.cjs`, usable on any kit page: `NODE_PATH=/tmp/pw/node_modules node sdk/kits/test/fresh-install/flow.cjs https://www.example.com/webinars/<slug> [--https-insecure] [--confirm-only]`), the WordPress admin checks, the CMS validators and the nginx test front.
