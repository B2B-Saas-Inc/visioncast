# Remaining gaps

What is still open on `0.4.10-pre`, reconciled 2026-11-05. **Everything here needs vendor accounts, an operator's decision, or a real event with real people.** Nothing implemented or already proven is listed; test evidence lives only in [validation.md](validation.md), and the ordered, tickable form of this page is [release-checklist.md](release-checklist.md). Detailed behaviour is owned by [mcp.md](mcp.md), [native-integrations.md](native-integrations.md), [tickets.md](tickets.md), [hosting.md](hosting.md), [kits.md](kits.md) and [operations.md](operations.md); where they disagree with this page on detail, theirs win.

## 1. Release blockers

Must be closed or explicitly accepted by the owner before production use.

### 1a. Evidence on the exact release tree and image

| # | Blocker |
|---|---|
| R24 | Pull a published manifest from GHCR (`read:packages` token or a public package) and run it on a real amd64 host. Every tag is published amd64 + arm64; the amd64 first run has only passed under emulation. |

### 1b. Vendor contracts (owner accounts required)

| # | Blocker |
|---|---|
| R11 | **Native adapters stay disabled** until each is checked in a sandbox the operator approves (any adapter already exercised live is noted in [validation.md](validation.md)): HubSpot (409 on email collision), Attio (`matching_attribute`, list entries), PostHog and Customer.io dedupe, Segment, GA4, Amplitude, Mixpanel, RudderStack (auth ambiguity, regional hosts), Brevo (campaign-eligible contacts under vendor defaults), **Salesforce** (client-credentials token flow unreadable in docs; composite upsert by External ID; `v62.0`), **Pipedrive** (v2 API; rate limits; `due_time` zone; activity dedupe scan limited to 500), **ActiveCampaign** (date custom-field format; regional hosts other than `api-us1.com`; account automations may fire), **Mailchimp** (`<dc>.api.mailchimp.com` form; whether API-created `pending` members get the opt-in mail; unknown merge tags), **beehiiv** (what a create returns for an existing email; tag re-add idempotency; API-key page unreadable), **Resend** (text/html size limits; 409 after a template change), **Amazon SES** (signer proven only against the AWS test vectors; non-ASCII subjects; sandbox/quotas; no idempotency, so a retry after a timeout can send twice). Vendor writes are never recalled; no vendor-side erasure. |
| R12 | **Stripe test mode** for the full paid flow: Checkout create, `expires_at` minimum, idempotency replay, expire endpoint, all five webhook events, refunds and disputes. Mocked only. |
| R13 | **Hosting provider sandboxes:** Zoom Webinars (create/update/attach **and** `past_webinars/{id}/participants` attendance pull), Wistia, Sequel, Riverside (attach, registrants, attendance: paths are our best reading). **LiveKit (self-hosted)** is covered by the live add-on's own tests against a real LiveKit stack; what it still lacks is in the live add-on section below. |
| R14 | **Real email delivery** — required, not optional, if paid tickets are sold (mailbox verification) or invitations / room codes are sent: an SMTP relay, or `EMAIL_DELIVERY=posthog` with a PostHog project. Both are mock-tested only in this repository. |
| R15 | **Real webhook receiver(s)** the owner will use (the receiver contract is verified offline only). The **Zapier, Make and n8n recipes** ran only as a Node simulation on backend-signed fixtures: import each on a real account/instance (Make blueprint structure and n8n node parameter names are the likeliest to need adjustment). |
| R16 | **Kit compatibility on the owner's real sites.** Fresh-install verification covered WordPress 7.1 / PHP 8.2 (block, shortcode, WP Super Cache, `/blog/` subdirectory), Next 16 self-hosted, SvelteKit 3 / Svelte 5, Astro 7 (behind nginx), Nuxt 4 (behind nginx), a static snippet behind nginx, and the four CMS schemas with vendor packages. **Not verified here:** https hosts other than localhost, CDN caches in front of `/webinar-api`, Vercel route handlers, real Webflow/Framer hosting, paid sessions through a kit, Next 14/15, Nuxt 3, Svelte 4, Astro middleware mode, WordPress multisite. |
| R17 | **Web calendar deep links** (Google/Outlook/Microsoft 365) against the live services, or accept unverified. |

### 1c. Owner policy acceptances

| # | Decision |
|---|---|
| R18 | Staff publish without confirmation tokens; admin inline-add fails closed; residual cancel/reschedule race; `team_admin` ≥ `content_write` + `publication`. |
| R19 | Confirmation tokens are not human approval. |
| R20 | `config_read` reads drafts over REST, or request a `content_read` scope (none exists). |
| R21 | Direct-mode ICS rewrite default: Mode B edges must forward `/webinar-api/calendar.ics` (and `my-registration`) or set `WEBINAR_DIRECT_ICS_REWRITE=false`. |
| R22 | Paid sessions require the waiting room and mail; join links sent to webhook destinations before a refund cannot be recalled. |
| R23 | Residual behaviour in §4. |
| R27 | Attendance import never runs automatically and never guesses an ambiguous match; a Zoom pull is capped at 50 pages (5,000 participants) and marked incomplete beyond that; no-shows are a separate confirmed step. |
| R28 | Marketing adapters never subscribe anyone: Mailchimp members default to `transactional` (pending double opt-in needs an explicit acknowledgement; `subscribed` is impossible), ActiveCampaign contacts are never added to lists or automations. **beehiiv is the deliberate exception**: its only record is the subscription, so it subscribes registrants (free tier, welcome email off and no double-opt-in override by default) behind an explicit `subscribe_ack`. Whether the vendor's own automations react to contact or tag changes is the operator's responsibility. |
| R29 | **Transactional attendee email** (Resend, Amazon SES) is now sent by the platform when such a destination is enabled: confirmation, reminder, new time, cancellation, attended / no-show, recording, follow-up. Plain-text templates with an allowlisted placeholder set, HTML generated from the text, no operator HTML; the join link is the managed room URL, or the provider link only when private links are allowed, else the public event page. This reverses the earlier out-of-scope decision (§6); the ESP-via-webhooks path still works and the operational SMTP for room codes is unchanged. |

## 2. Operational gates (target environment; operator/owner)

- A production host. Provisioning is the operator's job; `deploy/production/` is one complete
  layout (DigitalOcean droplet behind Cloudflare) and `deploy/quickstart/` the one-step installer.
- TLS, HTTP→HTTPS redirect, edge rate limits on `/webinar-api/*` (incl. the proxy's
  `my-registration`), `/w/`, `/mcp`, `/admin/`, `/studio/login/`, `/api/v1/payments/stripe/webhook`
  (`docker/nginx.conf.example` now covers all of them); MFA/SSO/VPN in front of `/admin/` and
  `/studio/`. Platform limits are per-process only.
- `RATE_LIMIT_TRUST_X_FORWARDED_FOR` only behind a trusted proxy chain of the configured depth.
- `INTEGRATION_MASTER_KEY` stored outside backups; scheduled `deploy/backup.sh` (encrypt with
  `--encrypt-to`); a restore drill with `deploy/restore.sh` on the target host.
- Worker process running (outbox, reminders, Stripe sweep, hosting sweep, stale operations
  including lost attendance pulls).
- `WEBHOOK_HOST_ALLOWLIST` entries for every enabled vendor (exact hosts for Salesforce My
  Domain, ActiveCampaign account host, Mailchimp data center and RudderStack data plane),
  Stripe and hosting hosts; https `PUBLIC_BASE_URL`; `PUBLIC_EVENT_URL_TEMPLATE`;
  `manage.py check` on every deploy.
- Stripe webhook endpoint registered with the five events in [tickets.md](tickets.md).
- Cron for `purge_personal_data` (now also blanks attendance-import rows).
- Existing deployments: `deploy/upgrade.sh --image <image>` (backup, `migrate` through 0017,
  restart, `sync_roles`, health); with the `deploy/production/` layout, `update.sh`.
- Launch acceptance in [implementation-guide.md §11.1](implementation-guide.md#111-launch-acceptance-run-on-staging-record-results-in-validationmd) on staging.

## 3. Requested feature backlog

Committed scope for this release is closed. Still **not** built:

| Item | State |
|---|---|
| Hosting update for Wistia/Sequel; Riverside create | Not built (Riverside create is undocumented by the vendor). |
| Intercom, Loops, Marketo, HubSpot Marketing, Postmark adapters | Roadmap only; nothing committed. |
| Wistia and Sequel attendance pull | No vendor attendance API this release; their reports go through the file import. |

## 4. Known residual behaviour (accepted or documented, not blockers)

| Item | Notes |
|---|---|
| Proxy `POST /webinar-api/registrations` 2xx body | Passed through apart from the `icsUrl` rewrite. |
| Thank-you `redirectUrl` | Any https URL; the proxy does not strip credentials. |
| "Recognized" oracle | `/profile` and `my-registration` reveal whether the browser holds a real capability. |
| `PUBLIC_EVENT_URL_TEMPLATE` check | `webinars.E101` does not run at Gunicorn start; run `manage.py check`. |
| Rate limits | Per-process, not global. |
| Stripe key rotation | Refused while orders are open. |
| `__Host-webinar_csrf` on plain http | Stored only in a secure context (https, `localhost`, `127.0.0.1`); other http hosts get `403 csrf_failed` on POST ([kits.md](kits.md)). |
| Native events are at-least-once | Mailchimp member events, Brevo and Customer.io events, Pipedrive activities beyond the 500-item dedupe scan can repeat on retry; consumers dedupe on the delivery ID. |

## 5. Optional / not planned (no commitment)

| Item | Notes / workaround |
|---|---|
| `/lobby/<event-slug>` route | Use REST `waitingRoom.url` or webhook `managedRoomUrl`. |
| Dedicated `content_read` scope | Alternative to R20; [continuation-api.md §5.1](continuation-api.md). |
| SDK calling `my-registration` | Sites wire it themselves. |
| Web calendar links following reschedules | Only ICS `UID`/`SEQUENCE` updates. |
| Waiting room under another prefix | Templates hard-code `/w/<code>/`. |
| Content-change webhooks | Use time-based revalidation. |
| Vendor-side erasure; native "send test to a chosen record" | Test sends cancel with `native_test_send_unsupported`. |
| Automated refunds, coupons, taxes, subscriptions, quantity > 1, Connect | [tickets.md](tickets.md) "Not supported". |
| Teams, Google Meet hosting | Not supported. |
| Automatic attendance pulls after a session | Imports are always a person's (or key's) explicit, confirmed action. |
| ActiveCampaign site tracking (`trackcmp.net`), Mailchimp `subscribed` status | Not implemented, by policy. |
| Global rate limiting, built-in TLS/MFA, built-in retention scheduler | Edge / cron. |
| MCP SSE/sessions/stdio | Stateless JSON over HTTP. |
| Over MCP: environment settings, host provisioning, Administrators/superusers, secrets, join/invitation links, registrant PII | Excluded by design ([mcp.md](mcp.md)). |

## 6. Out of scope by design (do not build)

- Streaming, video/recording hosting, gating or view tracking in the core application: the
  optional `live/` add-on does this on its own server (section on it below). In the core app,
  check-in and imported attendance measure admission or provider-reported presence, not viewing.
- SMS sending (consent evidence only). Attendee email is **no longer** out of scope: it is sent
  only when a Resend or SES destination is enabled (R29); otherwise the ESP sends it from
  webhooks as before.
- Cross-device recognition and account recovery ([security.md](security.md)).
- Multi-tenancy.

## Your own live server (LiveKit add-on)

Released in 0.4.0-pre. Tested on a real LiveKit stack in real browsers, on one machine ([validation.md](validation.md)).

**Needs a public server, a real event, or your own accounts.** Each has a script or a checklist:

- Certificates, firewalls and real networks: `deploy/live/verify-public.sh`.
- People on networks that only allow port 443: `live/tests/turn/check.cjs` against your server (a TURN relay over TLS on 443 through Traefik, with LiveKit's credentials).
- The studio talking to a live server by its public name and certificate; locally it's only been exercised over the test transport.
- A real session with real attendees.
- A real stream to YouTube, Meta, X and LinkedIn. So far only a local RTMP server has received one, over RTMP and RTMPS.
- A real bucket. A local S3-compatible server that checks every signature has taken recordings and captions, but Amazon S3 and DigitalOcean Spaces haven't.
- A load test on your own server: `live/tests/load/run.sh`. So far, on a laptop sharing its CPU with the load: 1,000 event streams and 100 simulated media viewers ([validation.md](validation.md)). No real browsers at that scale, and no recording under load.
- Safari itself, and real phones. So far: Playwright's WebKit build, including screen sharing, and phone screens in desktop browsers. Publishing a camera or microphone from Safari, and a transparent WebM graphic on a Safari viewer's screen, haven't been checked.
- Caption accuracy with your own speakers, names and jargon.

**Built, with limits you should know:**

- Slides lose animations and transitions. Notes come from PowerPoint files, not PDFs. A deck the converter never picks up shows as converting for up to 20 minutes, then fails. This happened once in testing and wasn't explained.
- RTMP input is RTMP only, not RTMPS, and an encoder is on stage for as long as its stream runs. There's no holding it in the green room.
- Translated captions are live only. Saved SRT and transcript files stay in the spoken language. Translation needs a language pack per language on your server.
- Speakers' own recordings stay on the live server's disk, listed with the recordings. They aren't copied to the bucket. A browser that can't keep up loses its own copy, not the main recording.
- `engagement.recorded` reaches webhook destinations and workflow presets. The native connectors (Salesforce, Pipedrive and the rest) don't send it yet.
- Surveys appear to people still on the page when the session ends. Nothing emails them afterwards unless you set that up from the event.
- Breakout rooms aren't recorded, captioned or included in attendance.
- `--local` on the installer shows the pages, but a session's room can't be created from the studio without real certificates.
- A personal link is in every request's address, so a proxy's logs see it and a copied download link carries it.
- Two-factor sign-in isn't built, for the studio or `/admin`. Put a VPN or SSO proxy in front.
- Removing someone from the studio's Team doesn't remove their host link from a session. Remove them under Crew.
- A replaced crew link's old LiveKit credential can watch for up to 15 minutes. It can't publish unless that person is on stage.
- A host or producer who deliberately modifies their browser could send their voice as a screen's audio.
- Go live is one press, with no undo.
- A poll's counts are in the event stream even with counts hidden on screen, so someone reading the stream can see them.
- Guests, and attendees brought on stage, see attendees' names in crew chat.
- A person can hold a breakout seat through `/act` without connecting, until the rooms close.
- Slides waiting in the converter's queue can be read by another job inside that container.
- iOS autoplay hasn't been checked on a real phone. If a browser blocks sound, the page offers a button to turn it on.
- After a page redraw, keyboard focus is restored for typing but not always for buttons.

**Not implemented:** captions from screen-share sound or video played on stage; pressing Go live on a platform for you; clustering across several LiveKit servers.
