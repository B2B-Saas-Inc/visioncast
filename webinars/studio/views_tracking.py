"""Studio "Tracking & GTM": a copyable site-wide Google Tag Manager install tag.

Unlike the per-session "Embed on your site" page (``views_embed``), this tag
contains no form. It loads ``webinar-form.js`` once on every page, applies the
visitor's consent state and binds the ``form[data-webinar-form]`` elements that
are already on the page. The SDK itself then does what
docs/browser-integration.md describes: browser recognition / progressive
profiling on those forms and consented attribution at submit time.

Consent never enters the tag through GTM ``{{variable}}`` substitution (which
pastes arbitrary text into JavaScript with no escaping). Instead the tag carries
only the *name* of a dataLayer key; at fire time the loader reads the newest
plain ``window.dataLayer`` message that owns that key, such as
``{event: 'webinar_consent_update', webinarConsent: {analytics: 'granted',
advertising: 'denied'}}``, and treats anything but an exact grant as denied.
"Always denied" writes ``null`` for the key.

Nothing is persisted: the inputs are plain GET parameters, validated here, and
the tag is rebuilt server-side. Every value lands in the tag through
``json.dumps`` with ``</`` and ``<!--`` escaped, so no input can close the
script element. The tag never contains API keys, join links or personal data.
"""
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from webinars.studio.access import render, studio_view
from webinars.studio.views_embed import sdk_script_url

LOADER_PATH = Path(__file__).resolve().parents[1] / "static" / "webinar-sdk" / "gtm-loader.js"
LOADER_START, LOADER_END = "// @gtm-loader-start", "// @gtm-loader-end"

DEFAULT_PROXY_BASE = "/webinar-api"
DEFAULT_CONSENT_KEY = "webinarConsent"
CONSENT_EVENT = "webinar_consent_update"
CONSENT_MODES = ("cmp", "denied")

MAX_SCRIPT_SRC = 512
MAX_PROXY_BASE = 128
# Same shape the SDK's safeProxyBase() accepts: a plain absolute path on this origin.
PATH_RE = re.compile(r"^/[A-Za-z0-9._~\-/]*$")
DOT_SEGMENT_RE = re.compile(r"(^|/)\.\.?(/|$)")
PRINTABLE_RE = re.compile(r"^[\x21-\x7e]+$")
# dataLayer key: a plain JavaScript identifier. Read with an own-property check in the loader,
# but the names that could still confuse a reader are refused outright.
CONSENT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
RESERVED_KEYS = frozenset({"__proto__", "constructor", "prototype", "hasOwnProperty", "event", "gtm", "toString", "valueOf"})
LOCAL_HOSTS = ("localhost", "127.0.0.1")

LABELS = {
    "script_src": "SDK script URL",
    "proxy_base": "Proxy path",
    "consent_mode": "Consent source",
    "consent_key": "dataLayer consent key",
}


def _is_plain_path(value):
    # One leading slash, no empty segments ("//" anywhere), no "." or ".." segments.
    return bool(PATH_RE.fullmatch(value)) and "//" not in value and not DOT_SEGMENT_RE.search(value)


def validate_script_src(raw):
    """A plain absolute path on the site, or an https URL (http only for localhost)."""
    value = (raw or "").strip()
    if not value or len(value) > MAX_SCRIPT_SRC or not PRINTABLE_RE.fullmatch(value) or "\\" in value:
        raise ValueError("Enter a path on your site (/assets/webinar-form.js) or an https URL, up to 512 characters.")
    if value.startswith("/"):
        if not _is_plain_path(value):
            raise ValueError("A path must be plain: starts with one slash, no '..', no '//' and no backslashes.")
        return value
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ValueError("That is not a valid URL.")
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise ValueError("Only https URLs (or a path on your own site) are allowed.")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("The URL needs a host name.")
    if scheme == "http" and host not in LOCAL_HOSTS:
        raise ValueError("Use https. Plain http is only allowed for localhost during development.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("The URL must not contain a user name or password.")
    if parts.query or parts.fragment or "?" in value or "#" in value:
        raise ValueError("The URL must not contain a query string or fragment.")
    if not parts.path or not _is_plain_path(parts.path):
        raise ValueError("The URL path must point at the script file, without '..' or '//'.")
    return value


def validate_proxy_base(raw):
    value = (raw or "").strip()
    if not value or len(value) > MAX_PROXY_BASE or not _is_plain_path(value):
        raise ValueError("Enter a plain path on your site such as /webinar-api (no scheme, host, '..' or '//').")
    return value.rstrip("/") or DEFAULT_PROXY_BASE


def validate_consent_key(raw):
    value = (raw or "").strip()
    if not CONSENT_KEY_RE.fullmatch(value) or value in RESERVED_KEYS:
        raise ValueError("Use a plain key name: letters, digits and underscores, starting with a letter (max 64), for example webinarConsent.")
    return value


def script_host(script_src):
    """Host name of a custom script URL, or ``"this site"`` for a same-origin path."""
    if script_src.startswith("/"):
        return "this site"
    return (urlsplit(script_src).hostname or "").lower()


def defaults():
    return {
        "script_src": sdk_script_url(),
        "proxy_base": DEFAULT_PROXY_BASE,
        "consent_mode": "cmp",
        "consent_key": DEFAULT_CONSENT_KEY,
    }


def clean(params):
    """Return ``(values, errors)``. Invalid fields fall back to their default so a tag is always shown."""
    values = defaults()
    errors = {}
    submitted = {k: params.get(k) for k in values if params.get(k) is not None}
    validators = {
        "script_src": validate_script_src,
        "proxy_base": validate_proxy_base,
        "consent_key": validate_consent_key,
    }
    for key, fn in validators.items():
        if key not in submitted:
            continue
        try:
            values[key] = fn(submitted[key])
        except ValueError as exc:
            errors[key] = [str(exc)]
    if "consent_mode" in submitted:
        mode = submitted["consent_mode"]
        if mode in CONSENT_MODES:
            values["consent_mode"] = mode
        else:
            errors["consent_mode"] = ["Choose one of the listed options."]
    return values, errors


@lru_cache(maxsize=1)
def loader_body():
    text = LOADER_PATH.read_text(encoding="utf-8")
    start = text.index(LOADER_START) + len(LOADER_START)
    end = text.index(LOADER_END)
    return text[start:end].strip("\n")


def script_json(value):
    """JSON that is safe inside a ``<script>`` element: ASCII only, no ``</`` or ``<!--`` sequences.

    Both replacements stay valid JSON and valid JavaScript string escapes.
    """
    text = json.dumps(value, ensure_ascii=True).replace("</", "<\\/").replace("<!--", "<\\u0021--")
    assert "</" not in text and "<!--" not in text and " " not in text and " " not in text
    return text


def tag_config(values):
    key = values["consent_key"] if values["consent_mode"] == "cmp" else None
    return {"src": values["script_src"], "proxyBase": values["proxy_base"], "consentKey": key}


def build_tag(values):
    """The Custom HTML tag as a plain ``str`` (not marked safe); the template escapes it for display."""
    return (
        "<!-- Webinar forms: site-wide install tag generated by Studio. Contains no keys. -->\n"
        "<script>\n"
        "(function () {\n"
        f"{loader_body()}\n"
        f"  webinarFormGtmInstall(window, document, {script_json(tag_config(values))});\n"
        "})();\n"
        "</script>"
    )


def build_bridge_example(values):
    """A copyable example of the dataLayer message the CMP must push. Literal values, no CMP wiring."""
    key = values["consent_key"]
    return (
        "<!-- Example only: run this from your consent platform's callback, once consent is resolved\n"
        "     and again on every change. Replace 'granted' / 'denied' with the visitor's actual choice. -->\n"
        "<script>\n"
        "  window.dataLayer = window.dataLayer || [];\n"
        "  window.dataLayer.push({\n"
        f"    event: {script_json(CONSENT_EVENT)},\n"
        f"    {key}: {{ analytics: 'granted', advertising: 'denied' }}\n"
        "  });\n"
        "</script>"
    )


@studio_view(perm="webinars.view_apikey", methods=("GET",))
def tracking(request):
    values, errors = clean(request.GET)
    summary = [{"anchor": f"f-{k}", "label": LABELS[k], "message": " ".join(v)} for k, v in errors.items()]
    default_src = sdk_script_url()
    return render(
        request,
        "studio/settings/tracking.html",
        {
            "values": values,
            "errors": errors,
            "summary": summary,
            "snippet": build_tag(values),
            "bridge_example": build_bridge_example(values),
            "consent_event": CONSENT_EVENT,
            "default_script_src": default_src,
            "custom_script_host": script_host(values["script_src"]) if values["script_src"] != default_src else "",
            "defaults": defaults(),
        },
    )
