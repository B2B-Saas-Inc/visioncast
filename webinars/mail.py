"""Outgoing email: transport selection, preview mailbox and message helpers.

Backends (``settings.EMAIL_BACKEND``):

* ``webinars.mail.DisabledBackend`` (default): drops every message. Nothing is sent,
  stored or logged except a count. Waiting-room verification for unknown browsers
  and emailed invitations therefore do not reach anyone until SMTP is configured
  (the studio shows a warning; invitation links can be copied once by the inviter).
* ``webinars.mail.PreviewMailboxBackend``: preview/dev only. Stores each message
  Fernet-encrypted in ``PreviewMessage``; only superusers read them in the studio
  mailbox (``/studio/mailbox/``). Never public. Enabled by preview/settings.py.
* Django's SMTP backend when ``EMAIL_DELIVERY=smtp`` (config/settings.py, docs/email.md).
* ``webinars.mail.PostHogBackend`` when ``EMAIL_DELIVERY=posthog``: each message goes to one
  PostHog transactional template as ``subject``, ``text`` and ``html`` (docs/email.md).

Message bodies contain one-time links/codes, so they are never logged.
"""
import json
import logging
import urllib.error
import urllib.request
import uuid

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

PREVIEW_BACKEND = "webinars.mail.PreviewMailboxBackend"
DISABLED_BACKEND = "webinars.mail.DisabledBackend"
POSTHOG_BACKEND = "webinars.mail.PostHogBackend"
POSTHOG_SEND_PATH = "/api/projects/@current/messaging/send/email/"


class PostHogEmailError(Exception):
    """A refused or failed PostHog send. ``code`` is PostHog's error code or ``http_<status>``.

    Never carries the recipient or the message: both can hold a one-time code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class PostHogBackend(BaseEmailBackend):
    """Sends through PostHog transactional email: one API call per recipient.

    The template (``POSTHOG_EMAIL_TEMPLATE_ID``) owns the sender, the page and the footer; it shows
    ``{{ variables.subject }}`` and ``{{ variables.html | raw }}``. Turn on *Protect sensitive data* on
    it, because these messages carry sign-in codes and invitation links."""

    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages or []:
            for recipient in message.to or []:
                try:
                    posthog_send(str(recipient), str(message.subject), str(message.body))
                except PostHogEmailError:
                    if not self.fail_silently:
                        raise
                    continue
                sent += 1
        return sent


def posthog_send(to, subject, text):
    """One send. Returns PostHog's message id; raises ``PostHogEmailError`` otherwise."""
    from webinars.email_html import SPACED_PARAGRAPH, to_html_fragment

    payload = {
        "template_id": settings.POSTHOG_EMAIL_TEMPLATE_ID,
        "to": to,
        "distinct_id": to,
        "message_data": {"subject": subject, "text": text, "html": to_html_fragment(text, SPACED_PARAGRAPH)},
    }
    request = urllib.request.Request(
        f"https://{settings.POSTHOG_EMAIL_HOST}{POSTHOG_SEND_PATH}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.POSTHOG_EMAIL_API_KEY}",
            "Content-Type": "application/json",
            # Each message is sent once; the key only makes a transport-level resend harmless.
            "Idempotency-Key": f"visioncast-{uuid.uuid4()}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.EMAIL_TIMEOUT) as response:  # noqa: S310 (fixed https host)
            data = json.loads(response.read(64 * 1024) or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            code = json.loads(exc.read(16 * 1024) or b"{}").get("code")
        except ValueError:
            code = None
        raise PostHogEmailError(code if isinstance(code, str) and code.isidentifier() else f"http_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise PostHogEmailError("network_error") from None
    message_id = data.get("message_id") if isinstance(data, dict) else None
    if not isinstance(message_id, str):
        raise PostHogEmailError("no_message_id")
    return message_id


class DisabledBackend(BaseEmailBackend):
    """Accepts and discards messages. Reports zero sent so callers can tell."""

    def send_messages(self, email_messages):
        count = len(list(email_messages or []))
        if count:
            logger.info("Email delivery disabled; dropped %d message(s)", count)
        return 0


class PreviewMailboxBackend(BaseEmailBackend):
    """Stores messages encrypted in the database for the admin-only studio mailbox."""

    def send_messages(self, email_messages):
        from webinars.crypto import encrypt_secret
        from webinars.models import PreviewMessage

        stored = 0
        for message in email_messages or []:
            for recipient in message.to or []:
                PreviewMessage.objects.create(
                    to=str(recipient)[:254],
                    subject=str(message.subject)[:300],
                    body_encrypted=encrypt_secret(str(message.body)),
                )
                stored += 1
        return stored


def delivery_mode():
    """``"smtp"``, ``"posthog"``, ``"preview"`` or ``"disabled"`` for display and feature checks."""
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if backend == PREVIEW_BACKEND:
        return "preview"
    if backend == POSTHOG_BACKEND:
        return "posthog"
    if backend == DISABLED_BACKEND or backend.endswith("dummy.EmailBackend"):
        return "disabled"
    return "smtp"


def preview_mailbox_enabled():
    return getattr(settings, "EMAIL_BACKEND", "") == PREVIEW_BACKEND


def send(to, subject, body):
    """Send one plain-text message. Returns True only when the backend accepted it.

    Transport errors are swallowed (logged by type only) so a mail outage never turns
    into a different HTTP response, which would let callers distinguish addresses.
    """
    try:
        sent = EmailMessage(subject=subject, body=body, to=[to]).send(fail_silently=False)
    except Exception as exc:  # never log the exception text: it may contain the address
        logger.warning("Email send failed: %s", type(exc).__name__)
        return False
    return bool(sent)


def read_preview_body(message):
    from webinars.crypto import DecryptionError, decrypt_secret

    try:
        return decrypt_secret(message.body_encrypted)
    except DecryptionError:
        return "(This message was encrypted with a key that is no longer configured.)"
