"""Resend native adapter: one transactional email per delivery through ``POST /emails``.

Contract (official references fetched 2026-10-27, see docs/native-integrations.md):

* ``POST https://api.resend.com/emails`` with ``Authorization: Bearer <api key>`` and a JSON body
  ``from``, ``to`` (array, max 50; we send one), ``reply_to`` (optional), ``subject``, ``text``,
  ``html`` and ``headers`` (custom headers object). The response carries the email ``id``.
* ``Idempotency-Key`` request header (1-256 characters, kept for 24 hours): the same key with the
  same payload returns the same response without sending again; a different payload answers
  ``409 invalid_idempotent_request``. The delivery id is the key, so a retry of a request that
  was already accepted does not send a second email within that window.
* Rate limit: 10 requests per second per team by default; ``429`` with a ``retry-after`` header,
  which the transport parses and the outbox honours.

Rendering, placeholders, link rules and the operator settings are shared with Amazon SES
(``_transactional_email``).
"""
from webinars import native_integrations as ni
from webinars.native_providers import _transactional_email as tx

PROVIDER = "resend"
LABEL = "Resend"
EVENT_TYPES = tx.EVENT_TYPES
REQUIRED_GROUPS = tx.REQUIRED_GROUPS
HOSTS = {"global": "api.resend.com"}
# The email id is read from the response body.
NEEDS_REQUEST_TRANSPORT = True
SEND_PATH = "/emails"
MAX_IDEMPOTENCY_KEY_CHARS = 256

FIELDS = (
    ni.Field("region", "API host", "Resend API host.", choices=(("global", "api.resend.com"),)),
    *tx.FIELDS,
    ni.Field("api_key", "API key", "Resend API key with sending access, sent only as a Bearer token to api.resend.com.", secret=True, pattern=ni._SECRET),
    ni.PROCESSING_ACK,
)

validate_extra = tx.validate_extra


def plan(cfg, secrets, facts, event_type, body):
    """The single ``POST /emails`` step, fully validated before any call."""
    message = tx.render(cfg, facts, event_type, body)
    if not facts["delivery_id"] or len(facts["delivery_id"]) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise ni._Stop(ni.Outcome("dead", "native_delivery_id_missing"))
    payload = {
        "from": tx.from_header(cfg),
        "to": [message["to"]],
        "subject": message["subject"],
        "text": message["text"],
        "html": message["html"],
        "headers": tx.tracking_headers(facts, event_type),
    }
    reply = tx.reply_to(cfg)
    if reply:
        payload["reply_to"] = reply
    tx.require_request_size("send", payload)
    headers = {**ni._headers(f"Bearer {secrets['api_key']}"), "Idempotency-Key": facts["delivery_id"]}
    return payload, headers


def run(ctx, base, cfg, secrets, facts, event_type, body):
    payload, headers = plan(cfg, secrets, facts, event_type, body)
    data = ctx.call("send", "POST", base + SEND_PATH, payload, headers, True)
    # Accepted (2xx) means sent or deduplicated; the id is informational and never a reason to retry
    # (a retry could only send the message again once the idempotency window has passed).
    return ni._first_id(data, "id")


def preview_response(step, method, url, payload):
    return {"id": "<returned id>"}
