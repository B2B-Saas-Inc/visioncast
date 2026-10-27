"""Amazon SES native adapter: one transactional email per delivery through the SES API v2.

Contract (official references fetched 2026-10-27, see docs/native-integrations.md):

* ``POST https://email.<region>.amazonaws.com/v2/email/outbound-emails`` (SendEmail) with a JSON
  body ``FromEmailAddress``, ``Destination.ToAddresses``, ``ReplyToAddresses`` (optional),
  ``Content.Simple`` (``Subject``/``Body.Text``/``Body.Html`` with ``Data`` and ``Charset``, plus
  ``Headers`` as ``[{Name, Value}]``, at most 15) and ``ConfigurationSetName`` (optional). The
  response is ``{"MessageId": ...}``. ``TooManyRequestsException`` is 429; the other documented
  errors are 400 (``MessageRejected``, ``SendingPausedException``, ``AccountSuspendedException``,
  ``MailFromDomainNotVerifiedException``, ``BadRequestException``, ``LimitExceededException``)
  and 404, which the shared status mapping dead-letters.
* Authentication is AWS Signature Version 4 (``sigv4``), implemented here with the standard
  library only: canonical request over ``content-type``, ``host`` and ``x-amz-date`` with the
  SHA-256 of the exact bytes ``native_integrations._encode`` sends, string to sign with the
  ``<date>/<region>/ses/aws4_request`` scope, the four-step HMAC key derivation and the
  ``Authorization`` header. ``clock`` supplies ``x-amz-date`` (injectable for tests).
* SES has no idempotency key: a retry after a timeout or a lost lease can send the same
  message twice. Every message carries ``X-Webinar-Delivery-Id`` for downstream dedupe.

Credentials are an IAM access key pair with ``ses:SendEmail`` only; temporary credentials
(session tokens) are not supported.
"""
import datetime
import hashlib
import hmac
import logging
import re
from urllib.parse import quote

from webinars import native_integrations as ni
from webinars.native_providers import _transactional_email as tx

logger = logging.getLogger(__name__)

PROVIDER = "ses"
LABEL = "Amazon SES"
EVENT_TYPES = tx.EVENT_TYPES
REQUIRED_GROUPS = tx.REQUIRED_GROUPS
# Commercial regions from the SES endpoints table (fetched 2026-10-27); GovCloud is not offered.
REGIONS = (
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "af-south-1",
    "ap-south-1",
    "ap-south-2",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-northeast-3",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-southeast-3",
    "ap-southeast-5",
    "ca-central-1",
    "ca-west-1",
    "eu-central-1",
    "eu-central-2",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-north-1",
    "eu-south-1",
    "il-central-1",
    "me-south-1",
    "me-central-1",
    "sa-east-1",
)
HOSTS = {region: f"email.{region}.amazonaws.com" for region in REGIONS}
# The MessageId is read from the response body.
NEEDS_REQUEST_TRANSPORT = True
SEND_PATH = "/v2/email/outbound-emails"
SERVICE = "ses"
ALGORITHM = "AWS4-HMAC-SHA256"
MAX_HEADERS = 15
_ACCESS_KEY_ID = re.compile(r"^[A-Z0-9]{16,128}$")
_CONFIGURATION_SET = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEADER_NAME = re.compile(r"^[!-9;-@A-~]{1,126}$")  # MessageHeader.Name pattern
_HEADER_VALUE = re.compile(r"^[ -~]{1,995}$")  # MessageHeader.Value pattern
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9._@-]{1,200}$")

FIELDS = (
    ni.Field("region", "AWS Region", "Region of the verified SES identity; the API host is email.<region>.amazonaws.com.", choices=tuple((r, f"{r} (email.{r}.amazonaws.com)") for r in REGIONS)),
    *tx.FIELDS,
    ni.Field("configuration_set", "Configuration set", "Optional SES configuration set name applied to every message.", required=False, pattern=_CONFIGURATION_SET),
    ni.Field("access_key_id", "Access key ID", "IAM access key with ses:SendEmail only. Used to sign requests (SigV4); never sent in clear.", secret=True, pattern=_ACCESS_KEY_ID),
    ni.Field("secret_access_key", "Secret access key", "Secret of that access key. Only ever used to derive the request signature.", secret=True, pattern=ni._SECRET),
    ni.PROCESSING_ACK,
)

validate_extra = tx.validate_extra


def clock():
    """UTC now; module attribute so tests can pin ``x-amz-date``."""
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# AWS Signature Version 4 (standard library only)
# ---------------------------------------------------------------------------


def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _hmac(key, message):
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _canonical_value(value):
    # Trim, and collapse sequential spaces to one (create-signed-request: CanonicalHeaders).
    return " ".join(str(value).split())


def _canonical_query(query):
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        name, _, value = part.partition("=")
        pairs.append((quote(name, safe="-_.~"), quote(value, safe="-_.~")))
    return "&".join(f"{n}={v}" for n, v in sorted(pairs))


def sigv4(method, host, path, query, headers, payload, *, access_key_id, secret_access_key, region, service, now):
    """``(authorization, amz_date, canonical_request, string_to_sign)`` for ``headers`` (all signed).

    ``headers`` are the request headers to sign (``host`` is added when absent); ``x-amz-date`` is
    always signed and derived from ``now`` (an aware UTC datetime). ``payload`` are the exact
    request bytes. Follows https://docs.aws.amazon.com/IAM/latest/UserGuide/create-signed-request.html.
    """
    amz_date = now.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    signed = {str(k).lower().strip(): _canonical_value(v) for k, v in (headers or {}).items()}
    signed.setdefault("host", host)
    signed["x-amz-date"] = amz_date
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join(
        [method.upper(), quote(path or "/", safe="/-_.~"), _canonical_query(query), canonical_headers, signed_headers, _sha256_hex(payload)]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([ALGORITHM, amz_date, scope, _sha256_hex(canonical_request.encode("utf-8"))])
    key = _hmac(("AWS4" + secret_access_key).encode("utf-8"), date)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = f"{ALGORITHM} Credential={access_key_id}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    return authorization, amz_date, canonical_request, string_to_sign


# ---------------------------------------------------------------------------
# Request plan
# ---------------------------------------------------------------------------


def _message_headers(facts, event_type):
    out = []
    for name, value in tx.tracking_headers(facts, event_type).items():
        if not _HEADER_NAME.match(name) or not _HEADER_VALUE.match(value) or len(name) + len(value) > 996:
            raise ni._Stop(ni.Outcome("dead", "send: payload_unsupported"))
        out.append({"Name": name, "Value": value})
    if len(out) > MAX_HEADERS:
        raise ni._Stop(ni.Outcome("dead", "send: payload_unsupported"))
    return out


def plan(cfg, secrets, facts, event_type, body, *, now=None):
    """``(payload, headers)`` for the single SendEmail step, fully validated and signed before any call."""
    message = tx.render(cfg, facts, event_type, body)
    region = cfg.get("region") or ""
    host = HOSTS.get(region)
    if not host:
        raise ni._Stop(ni.Outcome("dead", "native_config_incomplete"))
    payload = {
        "FromEmailAddress": tx.from_header(cfg),
        "Destination": {"ToAddresses": [message["to"]]},
        "Content": {
            "Simple": {
                "Subject": {"Data": message["subject"], "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": message["text"], "Charset": "UTF-8"},
                    "Html": {"Data": message["html"], "Charset": "UTF-8"},
                },
                "Headers": _message_headers(facts, event_type),
            }
        },
    }
    reply = tx.reply_to(cfg)
    if reply:
        payload["ReplyToAddresses"] = [reply]
    if cfg.get("configuration_set") and _CONFIGURATION_SET.match(cfg["configuration_set"]):
        payload["ConfigurationSetName"] = cfg["configuration_set"]
    tx.require_request_size("send", payload)
    base_headers = ni._headers(None)
    authorization, amz_date, _canonical, _sts = sigv4(
        "POST",
        host,
        SEND_PATH,
        "",
        {"host": host, "content-type": base_headers["Content-Type"]},
        ni._encode(payload),
        access_key_id=secrets["access_key_id"],
        secret_access_key=secrets["secret_access_key"],
        region=region,
        service=SERVICE,
        now=now or clock(),
    )
    headers = {**base_headers, "Authorization": authorization, "X-Amz-Date": amz_date}
    return payload, headers


def run(ctx, base, cfg, secrets, facts, event_type, body):
    payload, headers = plan(cfg, secrets, facts, event_type, body)
    data = ctx.call("send", "POST", base + SEND_PATH, payload, headers, True)
    # 200 means SES accepted the message. No idempotency exists, so a missing MessageId is never a
    # reason to retry (that could send the email twice); it is only logged.
    message_id = ni._first_id(data, "MessageId")
    if message_id and _MESSAGE_ID.match(message_id):
        logger.info("Native ses delivery %s accepted as MessageId %s", facts["delivery_id"], message_id)
    return message_id


def preview_response(step, method, url, payload):
    return {"MessageId": "<returned id>"}
