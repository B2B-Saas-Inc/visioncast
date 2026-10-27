# Visioncast demo site

Next.js App Router (TypeScript) front end for the public demo at
`https://visioncast-ten.vercel.app`. The Django back end runs separately on its own server;
this site reads its public API and proxies registration through its own origin.

| Route | What it does |
|---|---|
| `/` | Project landing page; "Coming up" is live from `GET /api/v1/events?when=upcoming`. |
| `/events` | Search (`q`), topic and upcoming/recordings filters, pagination. Plain GET form, works without JS. |
| `/events/[slug]` | AIDA event page: attention/summary, interest, outcomes, decision and hosts, sessions, RSVP. |
| `/demo-art/<name>.svg` | Abstract SVG art generated locally from `<name>` (the event slug). |
| `/demo-recording/[slug]` | Target of the demo seed's recording links. States that the event is fictional and no recording exists; embeds no player. 404 unless the event's published recording link points here. |
| `/webinar-api/*` | Same-origin proxy: five fixed routes, see below. |

Pinned: `next@15.5.28`, `react@19.1.10`, `react-dom@19.1.10`, `typescript@5.9.2`. After changing
pins, regenerate `package-lock.json` with `npm install` (never edit it by hand) and run
`npm audit` before deploying.

## Environment (server-only)

See `.env.example`. Nothing is `NEXT_PUBLIC_`, so nothing is inlined into browser bundles.

* `VISIONCAST_API_BASE`: back-end origin, https, no path.
* `VISIONCAST_PROXY_KEY`: a key with the **`proxy` scope only**
  (`manage.py create_api_key --name visioncast-vercel --scope proxy`).
* `VISIONCAST_ALLOWED_ORIGINS`: exact origins for `/webinar-api/*`; default
  `https://visioncast-ten.vercel.app`. Vercel preview URLs are rejected unless listed.
* `VISIONCAST_SESSION_COOKIE`: must equal the back end's `CAPABILITY_COOKIE_NAME`.

Catalogue reads (`/api/v1/events`, `/api/v1/events/<slug>`, `/api/v1/forms/<version>`)
need no key and are sent without one. The key is attached only by `lib/proxy.ts`.

If the back end is unreachable or misconfigured, pages render an explicit "unavailable"
state. There is no static fallback data.

## Proxy (`lib/proxy.ts`, `lib/proxy-core.ts`)

Mirrors `sdk/proxy/server.js`:

| Browser route | Method | Upstream | Extra checks |
|---|---|---|---|
| `/webinar-api/profile` | GET | `/api/v1/profile` | exactly `occurrenceId` (UUID) + `formVersion`; issues CSRF cookie |
| `/webinar-api/my-registration` | GET | `/api/v1/my-registration` | CSRF header; exactly one `occurrenceId` |
| `/webinar-api/registrations` | POST | `/api/v1/registrations` | Origin required, CSRF, JSON, ≤ 32 KB, `schemaVersion: "1"` |
| `/webinar-api/reset` | POST | `/api/v1/reset` | Origin required, CSRF, JSON |
| `/webinar-api/calendar.ics` | GET | `/api/v1/occurrences/<id>/calendar.ics` | no key, no cookie forwarded |

Every other path under `/webinar-api/` is a 404. All routes check `Origin` (exact match
when present; required for POST) and `Sec-Fetch-Site` (`same-origin`, or `none` for GET).
Upstream requests use a 10 s timeout, do not follow redirects, and are capped at
256 KB (ICS 64 KB). Responses are projected to their documented keys. Error bodies keep
only `error.code` and field keys, never server messages. The site never receives or
renders join or waiting-room links.

### Cookie policy

* `webinar_session` (capability): only the back end's cookie of that name is re-issued,
  as `HttpOnly; Secure; SameSite=Lax; Path=/webinar-api` with its Max-Age/Expires. Any
  other upstream cookie and any `Domain` are dropped. Only this cookie is forwarded
  upstream.
* `__Host-webinar_csrf` (double-submit token): random 32 bytes, `Secure; SameSite=Strict;
  Path=/`, readable by page JS so it can be echoed in `X-Webinar-CSRF`. It is not an
  identity and is never forwarded upstream.
* No analytics or advertising cookies. Registrations send `analytics`/`advertising`
  consent `false`, and only `pageUrl` as attribution.

### Idempotency

`components/RsvpPanel.tsx` keeps one `idempotencyKey` per submission intent. It is keyed
by the SHA-256 of the request body (no personal data is stored), kept in `sessionStorage`,
and reused on retries after network or 5xx failures. A changed body gets a new key, and
the key is cleared after success. On `409 idempotency_conflict` the panel retries once
with a fresh key.

## Commands

```
npm install
npm run typecheck
npm test          # node:test with --experimental-strip-types (Node >= 22.18)
npm run build
```
