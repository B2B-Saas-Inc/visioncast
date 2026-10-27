"""MCP resources and prompts.

Resources come from a fixed allowlist of URIs, each generated from the canonical
in-code source (``normalization``, ``forms_schema``, ``openapi.build_spec``,
``outbox``) or read from an allowlisted guide file (``GUIDES``: fixed file names
under ``BASE_DIR/docs`` plus ``README.md``; the Dockerfile copies exactly these).
No caller-supplied path is ever joined to the filesystem. Each resource has its own
scopes; the redacted configuration needs ``config_read``.
"""
import json
import re

from django.conf import settings

from webinars.api.auth import has_scope
from webinars.models import ApiKey

S = ApiKey.Scope
ANY = tuple(S.values)
CATALOG = (S.PUBLIC_READ, S.CONTENT_WRITE, S.CONFIG_READ)

PLAYBOOK = """# Implementation playbook (webinar platform)

The full deployment, operations and website-integration guides follow this checklist.
Every guide is also available as webinar://guides/<name> (see resources/list); start with
webinar://guides/implementation-guide for the end-to-end website rollout.

0. Deploy (owner, trusted shell; MCP cannot do this): build the image from the
   Dockerfile; set DJANGO_SECRET_KEY, DJANGO_ALLOWED_HOSTS, PUBLIC_BASE_URL (https),
   POSTGRES_HOST/DB/USER/PASSWORD, INTEGRATION_MASTER_KEY, WEBHOOK_HOST_ALLOWLIST,
   WEBINAR_ALLOWED_ORIGINS, EMAIL_DELIVERY and SMTP settings in the process environment;
   run `manage.py migrate`, `manage.py sync_roles`, `manage.py bootstrap_admin`; start
   `web` (behind TLS + rate limits) and `worker`; create agent keys with
   `manage.py create_api_key --name agent --scope config_read ...` or in the studio as a
   superuser. Privileged scopes (publication, delivery_admin, keys_admin, team_admin)
   are superuser/owner-granted only.
1. Readiness: call get_configuration_readiness (config_read). Environment settings are
   read-only over MCP: change them in the deployment environment and restart web+worker.
2. Forms: read webinar://docs/field-dictionary, then create_form_version with canonical keys.
3. Catalogue: create_host / create_topic (tags) / update_host / update_topic.
4. Event: create_event (draft, AIDA copy), set_event_cta, set_event_cover or heroImageUrl.
5. Schedule: schedule_session, or create_series + schedule_recurring_sessions (explicit starts,
   never RRULE expansion). Join links are write-only.
6. Waiting room: configure_waiting_room (enabled, lead minutes, admission mode).
7. Publish: set_event_visibility (publication scope) — dry run shows readiness problems and
   returns a confirmToken; repeat with dryRun=false and the token.
8. Website: embed with the SDK (/static/webinar-sdk) or call GET /api/v1/events (see
   webinar://openapi). Registration posts go through the first-party proxy.
9. Delivery: save_destination (delivery_admin) disabled first, set_destination_signing_secret,
   set_destination_credentials, preview_destination_contract, then enable and
   send_test_delivery (fixed synthetic payload). Monitor with get_delivery_status;
   requeue_dead_messages after fixing the receiver.
10. After the session: publish_recording, finalize_attendance or record_attendance,
    get_report_summary (aggregates only).
11. Team (team_admin): list_team, invite_member / update_member / revoke_invitation for
    Editors and Analysts. Administrators and superusers are managed by a superuser only.
12. Tickets (webinar://guides/tickets): set_session_tickets (capacity: content_write;
    prices: payments_admin), set_event_price, get/set_payment_account (payments_admin,
    set-only Stripe secrets). Paid seats need SMTP and an enabled waiting room, and the
    buyer must prove their mailbox (202 verification_required). Refunds are never
    automated: refund in Stripe, then mark_order_refunded; release_unpaid_order only for
    reconciling orders Stripe confirms unpaid.
13. Hosting (webinar://guides/hosting): list/create/update_hosting_account (hosting_admin,
    set-only credentials), execute_session_hosting, reconcile_session_hosting,
    set_session_manual_link (hosting_provision). Capabilities differ by provider; read
    list_hosting_providers before promising provisioning.

Gated tools: dry run (default) returns a preview and a single-use confirmToken bound to
the tool, key, exact arguments and target state, valid 10 minutes. It shows the same key
asked twice; it is not independent human approval. REST publication uses the same
mechanism (428 + Webinar-Confirm-Token header).
"""


# Guide name -> path relative to BASE_DIR. Keep in sync with the Dockerfile COPY list and
# .dockerignore allowlist (tests/test_mcp_config.py checks both).
GUIDES = {
    "readme": "README.md",
    "implementation-guide": "docs/implementation-guide.md",
    "operations": "docs/operations.md",
    "integration": "docs/integration.md",
    "mcp": "docs/mcp.md",
    "management-api": "docs/management-api.md",
    "api-reference": "docs/api-reference.md",
    "webhooks": "docs/webhooks.md",
    "field-dictionary": "docs/field-dictionary.md",
    "field-mapping": "docs/field-mapping.md",
    "waiting-room": "docs/waiting-room.md",
    "studio": "docs/studio.md",
    "email": "docs/email.md",
    "browser-integration": "docs/browser-integration.md",
    "native-integrations": "docs/native-integrations.md",
    "security": "docs/security.md",
    "tickets": "docs/tickets.md",
    "hosting": "docs/hosting.md",
    "kits": "docs/kits.md",
}
GUIDE_PREFIX = "webinar://guides/"
MAX_GUIDE_BYTES = 512 * 1024
# Defence in depth: never serve anything shaped like a live key or private key.
_SECRET_PATTERNS = (
    re.compile(r"wbk_[0-9a-f]{8}_[A-Za-z0-9_\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def read_guide(name):
    """Text of an allowlisted guide, or None when it is not packaged in this build."""
    from pathlib import Path

    rel = GUIDES.get(name)
    if rel is None:
        return None
    base = Path(settings.BASE_DIR).resolve()
    path = (base / rel).resolve()
    if base not in path.parents or not path.is_file() or path.stat().st_size > MAX_GUIDE_BYTES:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def playbook():
    parts = [PLAYBOOK]
    for name, title in (("readme", "Deployment guide (README.md)"), ("operations", "Operations (docs/operations.md)"),
                        ("integration", "Website integration (docs/integration.md)")):
        text = read_guide(name)
        parts.append(f"\n\n---\n\n# {title}\n\n" + (text if text is not None else "_Not packaged in this build._"))
    return "".join(parts)


def field_dictionary():
    from webinars.forms_schema import AUTOCOMPLETE, CANONICAL_TYPES, CUSTOM_TYPES, DEFAULT_LABELS
    from webinars.normalization import CANONICAL_FIELDS

    return {
        "canonicalFields": [
            {"key": k, "label": DEFAULT_LABELS.get(k, k), "type": CANONICAL_TYPES.get(k, "text"),
             "autocomplete": AUTOCOMPLETE.get(k)}
            for k in CANONICAL_FIELDS
        ],
        "customFieldTypes": list(CUSTOM_TYPES),
        "customFieldKeys": "Simple identifiers stored as custom.<key>.",
        "fieldMapping": "Per-form alias -> canonical key, e.g. {\"work_email\": \"email\"}.",
    }


def webhook_contracts():
    from webinars import outbox
    from webinars.crypto import SIGNATURE_VERSION
    from webinars.models import Destination

    return {
        "schemaVersion": outbox.SCHEMA_VERSION,
        "eventTypes": list(outbox.EVENT_TYPES),
        "alwaysGroups": list(outbox.ALWAYS_GROUPS),
        "optionalGroups": list(Destination.GROUPS),
        "personGroups": list(outbox.PERSON_GROUPS),
        "privateLinkKeys": list(outbox.PRIVATE_LINK_KEYS),
        "signature": {"version": SIGNATURE_VERSION,
                      "description": "HMAC-SHA256 over delivery id, timestamp and body with the destination signing "
                                     "secret; verify with examples/verify_webhook.py logic and reject stale timestamps."},
    }


def _openapi():
    from webinars.openapi import build_spec

    return build_spec()


def _config():
    from webinars.mcp_config import readiness

    return readiness()


def _tool_matrix():
    from webinars import mcp

    return {"tools": [{"name": t["name"], "scopes": [str(s) for s in t["scopes"]], "gated": bool(t.get("gated")),
                       "readOnly": bool(t.get("readOnly"))} for t in mcp.TOOLS]}


RESOURCES = [
    {"uri": "webinar://docs/field-dictionary", "name": "field-dictionary", "title": "Canonical field dictionary",
     "mimeType": "application/json", "scopes": ANY, "read": field_dictionary},
    {"uri": "webinar://openapi", "name": "openapi", "title": "Complete OpenAPI 3.1 document",
     "mimeType": "application/json", "scopes": ANY, "read": _openapi},
    {"uri": "webinar://contracts/webhooks", "name": "webhook-contracts", "title": "Webhook delivery contract",
     "mimeType": "application/json", "scopes": ANY, "read": webhook_contracts},
    {"uri": "webinar://playbook/implementation", "name": "implementation-playbook", "title": "Implementation playbook",
     "mimeType": "text/markdown", "scopes": ANY, "read": playbook},
    {"uri": "webinar://mcp/tools", "name": "tool-matrix", "title": "MCP tool and scope matrix",
     "mimeType": "application/json", "scopes": ANY, "read": _tool_matrix},
    {"uri": "webinar://config/current", "name": "current-configuration", "title": "Current configuration (redacted)",
     "mimeType": "application/json", "scopes": (S.CONFIG_READ,), "read": _config},
]
RESOURCES_BY_URI = {r["uri"]: r for r in RESOURCES}
FORM_PREFIX = "webinar://forms/"


def list_resources(key):
    out = [{k: r[k] for k in ("uri", "name", "title", "mimeType")} for r in RESOURCES if has_scope(key, *r["scopes"])]
    for name, rel in GUIDES.items():
        out.append({"uri": GUIDE_PREFIX + name, "name": f"guide-{name}", "title": f"Guide: {rel}", "mimeType": "text/markdown"})
    if has_scope(key, *CATALOG):
        from webinars.models import FormVersion

        for fv in FormVersion.objects.filter(is_active=True).order_by("version")[:100]:
            out.append({"uri": FORM_PREFIX + fv.version, "name": f"form-{fv.version}",
                        "title": f"Form schema and context: {fv.version}", "mimeType": "application/json"})
    return out


def read_resource(uri, key):
    """Return ``contents`` for an allowlisted URI, or None (unknown or not permitted: indistinguishable)."""
    if not isinstance(uri, str) or len(uri) > 200:
        return None
    resource = RESOURCES_BY_URI.get(uri)
    if resource is not None:
        if not has_scope(key, *resource["scopes"]):
            return None
        data = resource["read"]()
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
        return [{"uri": uri, "mimeType": resource["mimeType"], "text": text}]
    if uri.startswith(GUIDE_PREFIX):
        # Dict lookup only: the suffix is never used as a path.
        text = read_guide(uri[len(GUIDE_PREFIX):])
        return None if text is None else [{"uri": uri, "mimeType": "text/markdown", "text": text}]
    if uri.startswith(FORM_PREFIX) and has_scope(key, *CATALOG):
        from webinars.api import queries
        from webinars.forms_schema import public_schema

        fv = queries.public_form_version(uri[len(FORM_PREFIX):])
        if fv is None:
            return None
        data = {"schema": public_schema(fv),
                "context": {"fieldDictionary": "webinar://docs/field-dictionary",
                            "usage": "Embed via the SDK or POST registrations through the proxy with these keys."}}
        return [{"uri": uri, "mimeType": "application/json", "text": json.dumps(data, ensure_ascii=False, default=str)}]
    return None


PROMPTS = [
    {"name": "guided_deployment", "title": "Guided deployment",
     "description": "Walk through provisioning, environment readiness and first API keys (instructions only).",
     "arguments": [{"name": "environment", "description": "e.g. staging or production", "required": False}],
     "text": "Help me deploy the webinar platform to {environment}. Read webinar://playbook/implementation, call "
             "get_host_provisioning_instructions and get_configuration_readiness, then list the exact environment "
             "variables I must set and verify. Do not attempt to run commands; environment settings are read-only here."},
    {"name": "website_launch", "title": "Website launch",
     "description": "Create, schedule and publish a webinar and embed it on a site.",
     "arguments": [{"name": "topic", "description": "What the webinar is about", "required": True}],
     "text": "Launch a webinar about {topic}. Use webinar://docs/field-dictionary for the form, create hosts/topics, "
             "create_event with AIDA copy, set_event_cta, schedule sessions, configure the waiting room, then dry-run "
             "set_event_visibility and show me the preview before confirming. Finish with embed instructions from "
             "webinar://openapi."},
    {"name": "delivery_diagnosis", "title": "Delivery diagnosis",
     "description": "Diagnose webhook/CRM delivery problems without exposing secrets.",
     "arguments": [{"name": "destination", "description": "Destination id or name", "required": False}],
     "text": "Diagnose deliveries for {destination}. Call list_destinations, get_delivery_status and "
             "preview_destination_contract, compare with webinar://contracts/webhooks and the WEBHOOK_HOST_ALLOWLIST "
             "readiness, and propose fixes. Only dry-run requeue_dead_messages until I confirm."},
]
PROMPTS_BY_NAME = {p["name"]: p for p in PROMPTS}


def list_prompts():
    return [{k: p[k] for k in ("name", "title", "description", "arguments")} for p in PROMPTS]


def get_prompt(name, arguments):
    prompt = PROMPTS_BY_NAME.get(name) if isinstance(name, str) else None
    if prompt is None:
        return None
    arguments = arguments if isinstance(arguments, dict) else {}
    values = {}
    for arg in prompt["arguments"]:
        value = arguments.get(arg["name"], "")
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError(f"argument {arg['name']} must be a string of at most 200 characters")
        if arg.get("required") and not value.strip():
            raise ValueError(f"missing argument: {arg['name']}")
        values[arg["name"]] = value.strip() or "(not specified)"
    return {"description": prompt["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": prompt["text"].format(**values)}}]}
