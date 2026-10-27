"""Destination presets for workflow-automation platforms (Zapier, Make, n8n).

A preset never changes delivery: the destination stays a generic signed webhook
(``webinars.delivery``), with the same headers, signature, retries and SSRF policy as any
other. What a preset adds is a *save-time* contract and guidance:

* the URL must have the platform's documented shape (host and path rules below), on top of
  the usual ``validate_destination_url`` policy (https, operator ``WEBHOOK_HOST_ALLOWLIST``);
* ``kind`` is pinned to ``generic`` and ``include_private_links`` to ``False`` — the
  platform receives a JSON message to route, never a per-registrant join link;
* a native provider and a preset are mutually exclusive;
* the studio and the MCP previews point at the platform's verification recipe
  (``sdk/kits/automation/<preset>/``), which checks ``Webinar-Signature`` and dedupes on
  ``Webinar-Delivery-Id`` with the platform's own tools.

The choice is recorded as the non-secret key ``webhook_preset`` in the destination's
encrypted credentials dict (like the ``native_*`` keys): no model field, no migration.
Delivery ignores the key. URL shapes were checked against the vendors' public pages on
2026-10-27 (citations in docs/webhooks.md); none of the platforms was used.
"""
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

PRESET_KEY = "webhook_preset"
RECIPE_ROOT = "sdk/kits/automation/"
N8N_CLOUD_SUFFIX = ".app.n8n.cloud"
N8N_TEST_PREFIX = "/webhook-test/"

# Make zones listed on developers.make.com (API structure page): eu1, eu2, us1, us2.
MAKE_ZONES = ("eu1", "eu2", "us1", "us2")

_ZAPIER_HOST = "hooks.zapier.com"
# help.zapier.com "Trigger Zaps from webhooks": https://hooks.zapier.com/hooks/catch/123456/zbB61
_ZAPIER_PATH = re.compile(r"^/hooks/catch/\d+/[A-Za-z0-9]+/?$")
_MAKE_HOST = re.compile(r"^hook\.(" + "|".join(MAKE_ZONES) + r")\.make\.com$")
# help.make.com examples: https://hook.eu1.make.com/abc123xyz
_MAKE_PATH = re.compile(r"^/[A-Za-z0-9]+$")
# docs.n8n.io: https://<your-n8n-url>/webhook/<path>; N8N_ENDPOINT_WEBHOOK default "webhook".
_N8N_PATH = re.compile(r"^/webhook/[A-Za-z0-9._~\-]+(?:/[A-Za-z0-9._~\-]+)*/?$")


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    url_rule: str  # one sentence for the studio help text and errors
    recipe: str  # repository path of the verification recipe
    verification: str  # what the platform can verify, and its limits
    retries: str  # how the platform itself retries / why dedupe matters


PRESETS = {
    "zapier": Preset(
        id="zapier",
        label="Zapier (Catch Raw Hook)",
        url_rule="A Zapier Catch Hook URL: https://hooks.zapier.com/hooks/catch/<account id>/<hook id>/",
        recipe=RECIPE_ROOT + "zapier/",
        verification="A Code by Zapier (JavaScript) step verifies Webinar-Signature (HMAC-SHA256 with Node's "
                     "crypto), checks the timestamp window and dedupes on Webinar-Delivery-Id with StoreClient. "
                     "Use the Catch Raw Hook trigger: only it keeps the raw body and the headers.",
        retries="Zapier answers 200 on receipt and does not retry a failed run; a platform retry re-sends the "
                "same Webinar-Delivery-Id, so dedupe on it.",
    ),
    "make": Preset(
        id="make",
        label="Make (custom webhook)",
        url_rule="A Make custom webhook URL: https://hook.<zone>.make.com/<hook id> (zones eu1, eu2, us1, us2)",
        recipe=RECIPE_ROOT + "make/",
        verification="Make's sha256(text; 'hex'; key) built-in function computes the HMAC; a router filter compares "
                     "it with Webinar-Signature and checks the timestamp window. The raw body needs the webhook's "
                     "JSON pass-through option. Dedupe with a Data store keyed by Webinar-Delivery-Id.",
        retries="Make queues the request and answers 200 'Accepted' before the scenario runs; scenario errors are "
                "not re-requested, and a platform retry re-sends the same Webinar-Delivery-Id, so dedupe on it.",
    ),
    "n8n": Preset(
        id="n8n",
        label="n8n (Webhook node)",
        url_rule="A production n8n webhook URL: https://<your n8n host>/webhook/<path> (not /webhook-test/); the host "
                 "must be an exact WEBHOOK_HOST_ALLOWLIST entry or end in .app.n8n.cloud",
        recipe=RECIPE_ROOT + "n8n/",
        verification="Webhook node with Raw Body on, Extract from File to get the body text, Crypto node (Hmac, "
                     "SHA256, hex, secret from a Crypto credential), IF on the signature and timestamp, Remove "
                     "Duplicates on Webinar-Delivery-Id, then a Switch on eventType.",
        retries="n8n answers once the workflow starts or finishes; a failed execution is not re-requested, and a "
                "platform retry re-sends the same Webinar-Delivery-Id, so dedupe on it.",
    ),
}

CHOICES = [("", "None (plain signed webhook)")] + [(p.id, p.label) for p in PRESETS.values()]
LABELS = {p.id: p.label for p in PRESETS.values()}


def preset_of(credentials):
    """The stored preset id ('' when none or unknown). Never raises."""
    value = (credentials or {}).get(PRESET_KEY, "")
    return value if value in PRESETS else ""


def destination_preset(destination):
    """Preset id for ``destination`` ('' when none, or when credentials cannot be read)."""
    from webinars import native_integrations as native

    try:
        return preset_of(native.load_credentials(destination))
    except native.CredentialsUnavailable:
        return ""


def with_preset(credentials, preset):
    """New credentials dict with ``preset`` recorded (or the key removed when empty)."""
    out = {k: v for k, v in (credentials or {}).items() if k != PRESET_KEY}
    if preset:
        if preset not in PRESETS:
            raise ValueError("unknown preset")
        out[PRESET_KEY] = preset
    return out


def _n8n_host_problem(host):
    from webinars.native_integrations import destination_host_problem

    if host.endswith(N8N_CLOUD_SUFFIX) and len(host) > len(N8N_CLOUD_SUFFIX):
        return None
    problem = destination_host_problem(host)
    if problem is None:
        return None
    if problem == "vendor_host":
        return "The n8n host must be your own n8n host, not another vendor's API host."
    if problem == "not_allowlisted":
        return "The n8n host must be an exact WEBHOOK_HOST_ALLOWLIST entry (or end in .app.n8n.cloud)."
    return "The n8n host must be a plain https host name."


def url_problem(preset, url):
    """Why ``url`` does not fit ``preset``, else None. Shape only: the SSRF policy
    (``validate_destination_url``) is applied separately by the destination form."""
    if not preset:
        return None
    if preset not in PRESETS:
        return "Unknown preset."
    rule = PRESETS[preset].url_rule
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return f"Enter {rule}."
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.port not in (None, 443):
        return f"Enter {rule}."
    if parts.query or parts.fragment:
        return f"Remove the query string or fragment. {rule}."
    path = parts.path or ""
    if preset == "zapier":
        if host != _ZAPIER_HOST or not _ZAPIER_PATH.match(path):
            return f"Not a Zapier Catch Hook URL. {rule}."
        return None
    if preset == "make":
        if not _MAKE_HOST.match(host) or not _MAKE_PATH.match(path):
            return f"Not a Make webhook URL. {rule}."
        return None
    # n8n
    if path.startswith(N8N_TEST_PREFIX) or path.rstrip("/") == N8N_TEST_PREFIX.rstrip("/"):
        return "This is the n8n test URL (/webhook-test/); it only listens while you test in the editor. Use the production URL."
    if not _N8N_PATH.match(path) or any(set(segment) == {"."} for segment in path.split("/")):
        return f"Not an n8n production webhook URL. {rule}."
    return _n8n_host_problem(host)


def describe(preset):
    """Template/MCP-ready description of a preset (None when no preset)."""
    p = PRESETS.get(preset or "")
    if p is None:
        return None
    return {"id": p.id, "label": p.label, "urlRule": p.url_rule, "recipe": p.recipe,
            "verification": p.verification, "retries": p.retries}


def pin_defaults(data):
    """Apply the preset defaults to DestinationForm data (in place): generic kind, no private links."""
    data["kind"] = "generic"
    data.pop("include_private_links", None)
    return data
