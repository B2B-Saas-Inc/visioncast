# Outgoing email

Used for waiting-room verification codes and team invitations. Off by default.

| `EMAIL_DELIVERY` | Behaviour |
|---|---|
| `disabled` (default) | `webinars.mail.DisabledBackend` drops messages. Invitation links are shown once to the inviting superuser to share manually. Unknown waiting-room visitors cannot verify. |
| `smtp` | Django SMTP backend. Requires `SMTP_HOST`; optional `SMTP_PORT` (587), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS` (1) or `SMTP_SSL`, `SMTP_TIMEOUT_SECONDS` (10), `DEFAULT_FROM_EMAIL`. Production refuses plaintext SMTP. |
| `posthog` | PostHog transactional email (`webinars.mail.PostHogBackend`). Requires `POSTHOG_EMAIL_API_KEY` (a project secret key, `phs_…`, with Workflow: Write) and `POSTHOG_EMAIL_TEMPLATE_ID`; optional `POSTHOG_EMAIL_HOST` (`us.posthog.com`; `eu.posthog.com` or your own instance's host, never the `.i.` ingestion host) and `POSTHOG_EMAIL_TIMEOUT_SECONDS` (10). The sender, page and footer are the template's, so `DEFAULT_FROM_EMAIL` isn't used. See [PostHog setup](#posthog-transactional-email). |

Credentials come only from the environment; they are never stored, rendered or logged. Send failures are logged by exception type only and never change the HTTP response (no enumeration).

**Preview only:** `preview/settings.py` sets `webinars.mail.PreviewMailboxBackend`, which stores messages Fernet-encrypted in `PreviewMessage`. Only superusers can read them (they contain invitation links and registrants' room codes/magic links) at `/studio/mailbox/` (404 when that backend is not configured). Housekeeping deletes them after 1 day.

## PostHog transactional email

PostHog can send both kinds of email: the platform's own (`EMAIL_DELIVERY=posthog`) and attendee email (the **PostHog transactional email** destination below). Each send names a transactional template and passes the message as variables, so one template can carry every message of its kind. VisionCast renders the words exactly as it does for SMTP, Resend and SES, and PostHog adds the brand, sender and footer.

One-time setup in PostHog ([their guide](https://posthog.com/docs/workflows/transactional-emails)):

1. **Sender.** In Workflows, add an email channel for an address on your domain and add the DNS records it shows (SPF, DKIM, DMARC alignment).
2. **Templates.** Create two templates with the sender. Each has the subject `{{ variables.subject }}` and a body whose content block outputs `{{ variables.html | raw }}` (PostHog escapes output unless told otherwise; VisionCast has already escaped every value in it) (paragraphs of escaped text with links; the plain text is in `{{ variables.text }}`). Use your logo, colours and footer around it. Then **Make transactional**, with no marketing category.
   - *Platform email*: turn on **Protect sensitive data**, because it carries sign-in codes and invitation links. Its ID is `POSTHOG_EMAIL_TEMPLATE_ID`.
   - *Attendee email*: confirmations, reminders and the rest. Its ID goes in the destination.
3. **Key.** Create a project secret API key with **Workflow: Write**. It can only send these emails and read their status. Put it in `POSTHOG_EMAIL_API_KEY` on the server, and in the destination's set-only key field. Never in a browser.
4. **Engagement.** With engagement events on in PostHog, sends, deliveries, opens and bounces land on the person. Attendee email uses the same `distinct_id` as the PostHog analytics destination (the verified email), so they meet the registrant's other events.

Limits: one recipient per call; PostHog's quota, sending allowance and suppression list apply; transactional email is in beta at PostHog. A send that PostHog refuses (for example `recipient_suppressed`) is logged with its code only, never with the address or the message.

## Transactional email to attendees (native destinations)

**Scope change (owner-approved).** Until this change attendee email was explicitly out of scope: the platform emitted webhooks and the operator's ESP sent the messages ([webhooks.md](webhooks.md), [implementation-guide.md §7](implementation-guide.md#7-webhooks-one-source-for-confirmations-reminders-and-follow-ups)). Two native destinations now let the platform render and send those emails itself through the operator's own provider account: **Resend**, **Amazon SES** and **PostHog transactional email** (`webinars/native_providers/resend.py`, `ses.py`, `posthog_email.py`, shared rendering in `_transactional_email.py`). They are ordinary outbox destinations: the same enqueue, consent, erasure, retry, lease and dead-letter rules as every webhook or native adapter apply, and the studio integration form configures them. Full contract, settings, placeholders and open points: [native-integrations.md](native-integrations.md#transactional-email-resend-and-amazon-ses-attendee-emails-sent-by-the-platform-contracts-checked-against-official-references-accounts-unverified). Status: source implemented, tests mock-only, no account used.

**What each event sends** (one email per registrant and delivery; only `identity.verified` registrations with an email):

| Event | Email |
| --- | --- |
| `registration.created` | Confirmation with the session time in the occurrence time zone, the join link, the calendar (ICS) link and the event page. |
| `reminder.due` | Reminder with the time and join link (one per configured reminder offset). |
| `occurrence.rescheduled` | New time, join link and calendar link. |
| `occurrence.cancelled` | Cancellation notice (also sent to registrations that are themselves cancelled). |
| `attendance.recorded` | Thank-you (attended) or sorry-we-missed-you (no-show); nothing for an unknown value. |
| `recording.published` | Recording link (only when the occurrence has a public https recording URL). |
| `follow_up.requested` | Follow-up note inviting a reply. |

Built-in plain-text templates are used unless the operator overrides the subject or body of a message in the studio; placeholders are a fixed allowlist (`{first_name}`, `{event_title}`, `{starts_at_local}`, `{timezone}`, `{duration_minutes}`, `{join_link}`, `{calendar_link}`, `{event_url}`, and `{recording_link}` for the recording message), and the HTML part is generated from the text. No operator HTML and no marketing content.

**Link rules.** The join link is the managed waiting-room URL (`/w/<code>/`) when the occurrence's room is on; the provider join link only when the room is off **and** the destination has *Include private links*; otherwise the public event page. The same `outbox.occurrence_links` policy as webhooks decides, at send time, so a room enabled after enqueue applies to queued reminders. Calendar links point at the public ICS endpoint; with the room on, the calendar entry's location and first line are the room URL too.

**Identity and dedupe.** Every message carries `X-Webinar-Delivery-Id` and `X-Webinar-Event-Type` headers. Resend receives the delivery id as its `Idempotency-Key` (retries within 24 hours do not send twice); SES has no idempotency key, so a retry after a timeout can send the same email twice. PostHog also receives the delivery id as its `Idempotency-Key` (24 hours), and gets it, the event type and the message name as variables instead of headers.

**Platform email is separate.** Waiting-room verification codes and team invitations go through `EMAIL_DELIVERY` (SMTP or PostHog) above, never through a destination. The two paths share nothing: destinations hold the provider credentials encrypted per destination, SMTP credentials come only from the environment.
