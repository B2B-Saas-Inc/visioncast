"""PostHog transactional email: one attendee email per delivery through PostHog Workflows.

Contract (official references fetched 2026-11-02, see docs/native-integrations.md):

* ``POST https://<us|eu>.posthog.com/api/projects/@current/messaging/send/email/`` with
  ``Authorization: Bearer <project secret API key>`` (``phs_…``, scope Workflow: Write) and a JSON
  body ``template_id``, ``to`` (one address), ``distinct_id`` and ``message_data``. ``202`` returns
  ``message_id``. This is the app host, never the ingestion host (``us.i.posthog.com``).
* ``Idempotency-Key`` request header (1-255 visible ASCII characters, kept for 24 hours): the same
  key and request returns the first ``message_id`` with ``replayed: true`` and sends nothing. The
  delivery id is the key.
* The email itself is a PostHog template marked transactional. A template can't read person
  properties, so everything it shows comes from ``message_data``. VisionCast renders the message
  exactly as it does for Resend and Amazon SES and sends it as ``subject``, ``text`` and ``html``,
  so one template serves every message: subject ``{{ variables.subject }}`` and a body that outputs
  ``{{ variables.html | raw }}`` (docs/email.md). The sender and its verified domain live on the template,
  so there are no from or reply-to settings here.
* Refusals with a ``code``: ``recipient_suppressed`` and ``recipient_unreachable`` (422) are final;
  ``throttled`` (429, ``Retry-After``) and ``delivery_unavailable`` (503) are retried with the same
  key, which the transport and outbox already do for 429 and 5xx.

Rendering, placeholders, link rules and the operator overrides are shared with Resend and SES
(``_transactional_email``).
"""
import re

from webinars import native_integrations as ni
from webinars.email_html import SPACED_PARAGRAPH
from webinars.native_providers import _transactional_email as tx

PROVIDER = "posthog_email"
LABEL = "PostHog transactional email"
EVENT_TYPES = tx.EVENT_TYPES
REQUIRED_GROUPS = tx.REQUIRED_GROUPS
HOSTS = {"us": "us.posthog.com", "eu": "eu.posthog.com"}
# The message id is read from the response body.
NEEDS_REQUEST_TRANSPORT = True
SEND_PATH = "/api/projects/@current/messaging/send/email/"
MAX_IDEMPOTENCY_KEY_CHARS = 255
TEMPLATE_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
API_KEY = re.compile(r"^phs_[A-Za-z0-9_-]{16,200}$")
# The sender is the template's, so the shared from and reply-to settings don't apply.
_NOT_HERE = {"from_address", "from_name", "reply_to"}

FIELDS = (
    ni.Field("region", "PostHog host", "Where you sign in to PostHog: US or EU Cloud.", choices=(("us", "us.posthog.com"), ("eu", "eu.posthog.com"))),
    ni.Field("template_id", "Template ID", "The transactional email's ID, from its API tab in PostHog. Its subject is {{ variables.subject }} and its body shows {{ variables.html | raw }}.", pattern=TEMPLATE_ID),
    *(f for f in tx.FIELDS if f.name not in _NOT_HERE),
    ni.Field("api_key", "Project secret API key", "A phs_ key with Workflow set to Write, sent only as a Bearer token to the PostHog host.", secret=True, pattern=API_KEY),
    ni.PROCESSING_ACK,
)

validate_extra = tx.validate_extra


def plan(cfg, secrets, facts, event_type, body):
    """The single send step, fully validated before any call."""
    message = tx.render(cfg, facts, event_type, body)
    delivery_id = facts["delivery_id"]
    if not delivery_id or len(delivery_id) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise ni._Stop(ni.Outcome("dead", "native_delivery_id_missing"))
    template_id = cfg.get("template_id") or ""
    if not TEMPLATE_ID.match(template_id):
        raise ni._Stop(ni.Outcome("dead", "native_config_incomplete"))
    payload = {
        "template_id": template_id,
        "to": message["to"],
        # The same person PostHog analytics uses: the verified email, else the platform person id.
        "distinct_id": ni.identified_id({}, facts),
        "message_data": {
            "subject": message["subject"],
            "text": message["text"],
            # Paragraphs only: the PostHog template supplies the page, the brand and the footer.
            "html": tx.to_html_fragment(message["text"], SPACED_PARAGRAPH),
            "event_type": event_type,
            "message": message["key"],
            "delivery_id": delivery_id,
        },
    }
    tx.require_request_size("send", payload)
    headers = {**ni._headers(f"Bearer {secrets['api_key']}"), "Idempotency-Key": delivery_id}
    return payload, headers


def run(ctx, base, cfg, secrets, facts, event_type, body):
    payload, headers = plan(cfg, secrets, facts, event_type, body)
    data = ctx.call("send", "POST", base + SEND_PATH, payload, headers, True)
    # 202 means queued, or replayed for the same key; the id is informational, never a reason to retry.
    return ni._first_id(data, "message_id")


def preview_response(step, method, url, payload):
    return {"message_id": "<returned id>", "replayed": False}
