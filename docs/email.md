# Outgoing email

Used for waiting-room verification codes and team invitations. Off by default.

| `EMAIL_DELIVERY` | Behaviour |
|---|---|
| `disabled` (default) | `webinars.mail.DisabledBackend` drops messages. Invitation links are shown once to the inviting superuser to share manually. Unknown waiting-room visitors cannot verify. |
| `smtp` | Django SMTP backend. Requires `SMTP_HOST`; optional `SMTP_PORT` (587), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS` (1) or `SMTP_SSL`, `SMTP_TIMEOUT_SECONDS` (10), `DEFAULT_FROM_EMAIL`. Production refuses plaintext SMTP. |

Credentials come only from the environment; they are never stored, rendered or logged. Send failures are logged by exception type only and never change the HTTP response (no enumeration).

**Preview only:** `preview/settings.py` sets `webinars.mail.PreviewMailboxBackend`, which stores messages Fernet-encrypted in `PreviewMessage`. Only superusers can read them (they contain invitation links and registrants' room codes/magic links) at `/studio/mailbox/` (404 when that backend is not configured). Housekeeping deletes them after 1 day.
