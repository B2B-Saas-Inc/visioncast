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

Message bodies contain one-time links/codes, so they are never logged.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

PREVIEW_BACKEND = "webinars.mail.PreviewMailboxBackend"
DISABLED_BACKEND = "webinars.mail.DisabledBackend"


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
    """``"smtp"``, ``"preview"`` or ``"disabled"`` for display and feature checks."""
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if backend == PREVIEW_BACKEND:
        return "preview"
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
