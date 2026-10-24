"""MCP configuration tools, resources and prompts (registered by ``webinars.mcp``).

Least privilege: every tool names the scopes that may call it (any one suffices).

* ``config_read``    redacted readiness, catalogue listings, destinations (no secrets,
                     no URL paths), delivery counts, host provisioning instructions.
* ``publication``    publish/unpublish events, publish recordings.
* ``delivery_admin`` create/update destinations, set-only credentials and signing
                     secret, requeue dead-lettered deliveries, payload contract preview.
* ``keys_admin``     list/create/revoke API keys. Created keys are bounded to
                     ``DELEGABLE_SCOPES`` and to the caller's own scopes (no escalation).
* ``team_admin``     list team, invite/revoke and manage Editor/Analyst members only,
                     bounded by the key's own scopes (``invitations.ROLE_REQUIRED_SCOPES``:
                     Editor needs content_write + publication, Analyst needs reporting),
                     because those humans act in the studio without confirmation.

``trusted``, ``publication``, ``delivery_admin``, ``keys_admin`` and ``team_admin`` are
granted only by a superuser in the studio or ``manage.py create_api_key``
(``services.PRIVILEGED_SCOPES``).

Gated actions (publication, destructive, secret set/rotation, key creation/revocation,
invitations/member changes, test sends, requeue) are two-step: call with ``dryRun``
(the default) to get a side-effect-free preview and a ``confirmToken``; repeat with
``dryRun: false`` and that token within 10 minutes (``webinars.confirmation``: bound
to tool, key, exact arguments and target state; single use). It proves only that *the
holder of this API key* asked twice for the same change; it is NOT human approval.

Not supported (by design): Administrator/superuser management, process-environment
settings (read-only readiness + guidance), shell/file writes, arbitrary HTTP.
"""
import base64
import binascii
import hashlib
import json
from types import SimpleNamespace

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count

from webinars import confirmation
from webinars.api import management
from webinars.api.auth import has_scope
from webinars.audit import audit
from webinars.models import ApiKey, Destination, Event, FormVersion, Host, Occurrence, OutboxMessage, Series, Topic

S = ApiKey.Scope
CONFIG = (S.CONFIG_READ,)
CATALOG_READ = (S.CONFIG_READ, S.CONTENT_WRITE)
CONTENT = (S.CONTENT_WRITE,)
PUBLISH = (S.PUBLICATION,)
DELIVERY = (S.DELIVERY_ADMIN,)
KEYS = (S.KEYS_ADMIN,)
ATTEND = (S.ATTENDANCE_WRITE,)
TEAM = (S.TEAM_ADMIN,)

# Scopes a keys_admin key may grant (and only if it holds them itself). The privileged
# scopes (services.PRIVILEGED_SCOPES: publication, delivery_admin, keys_admin,
# team_admin) and ``trusted`` are never delegable: superuser studio or owner shell only.
DELEGABLE_SCOPES = (
    S.PUBLIC_READ, S.REGISTER, S.PROXY, S.REPORTING, S.CONTENT_WRITE, S.ATTENDANCE_WRITE, S.CONFIG_READ,
)
MAX_COVER_BYTES = 40 * 1024  # base64 must fit the 64 KB MCP body; larger covers: studio upload or heroImageUrl
MAX_BATCH_SESSIONS = 52
TEST_EVENT_TYPE = "webhook.test"

_UUID = {"type": "string", "format": "uuid"}
_GATE = {
    "dryRun": {"type": "boolean", "default": True, "description": "true (default): preview only, no side effects."},
    "confirmToken": {"type": "string", "maxLength": 2000, "description": "Token from the dry run; required with dryRun=false."},
}


class ToolError(Exception):
    def __init__(self, status, code, message, fields=None):
        super().__init__(message)
        self.status, self.code, self.message, self.fields = status, code, message, fields

    def body(self):
        error = {"code": self.code, "message": self.message}
        if self.fields:
            error["fields"] = self.fields
        return {"error": error}


def _schema(props, required=(), gated=False):
    if gated:
        props = {**props, **_GATE}
    return {"type": "object", "properties": props, "additionalProperties": False, "required": list(required)}


# ---------------------------------------------------------------------------
# Confirmation gate
# ---------------------------------------------------------------------------


def _gate(tool, key, args, preview, state=None):
    """Return a dry-run result, or None once a confirmToken for exactly this call was consumed.

    Delegates to ``webinars.confirmation`` (bound to tool, key, exact arguments and the
    target's ``state``; 10-minute expiry; single use). The dry run writes nothing.
    """
    material = {k: v for k, v in args.items() if k not in ("dryRun", "confirmToken")}
    if args.get("dryRun", True):
        token = confirmation.issue("mcp." + tool, key, material, state)
        return 200, {"dryRun": True, "preview": preview, "confirmToken": token, "note": confirmation.NOTE}
    try:
        confirmation.consume("mcp." + tool, key, material, args.get("confirmToken"), state)
    except confirmation.ConfirmationError as exc:
        raise ToolError(400, exc.code, exc.message)
    return None


# ---------------------------------------------------------------------------
# Redacted projections
# ---------------------------------------------------------------------------


def _url_host(url):
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url or "")
    except ValueError:
        return None
    return parts.hostname


def serialize_destination(dest):
    from webinars.services import credential_names

    return {
        "id": str(dest.pk),
        "name": dest.name,
        "kind": dest.kind,
        # Path/query may embed tokens: only the host is shown.
        "urlHost": _url_host(dest.url),
        "enabled": dest.enabled,
        "eventTypes": list(dest.event_types or []),
        "allowedGroups": list(dest.allowed_groups or []),
        "requiredConsents": list(dest.required_consents or []),
        "includePrivateLinks": dest.include_private_links,
        "fieldMapping": dict(dest.field_mapping or {}),
        "maxAttempts": dest.max_attempts,
        "hasSigningSecret": bool(dest.signing_secret_encrypted),
        "credentialNames": credential_names(dest),
    }


def serialize_key(k):
    return {"id": str(k.pk), "name": k.name, "prefix": k.prefix, "scopes": list(k.scopes or []),
            "createdAt": k.created_at.isoformat(), "revoked": k.revoked_at is not None}


def readiness():
    """Redacted server readiness: booleans/counts only, never values of secrets or hosts."""
    from webinars.mail import delivery_mode

    def has(name):
        return bool(getattr(settings, name, None))

    env_settings = {
        "PUBLIC_BASE_URL": {"set": has("PUBLIC_BASE_URL"), "https": str(getattr(settings, "PUBLIC_BASE_URL", "")).startswith("https://")},
        "DJANGO_ALLOWED_HOSTS": {"count": len(getattr(settings, "ALLOWED_HOSTS", []) or [])},
        "WEBINAR_ALLOWED_ORIGINS": {"count": len(getattr(settings, "WEBINAR_ALLOWED_ORIGINS", []) or [])},
        "THANK_YOU_REDIRECT_HOSTS": {"count": len(getattr(settings, "THANK_YOU_REDIRECT_HOSTS", []) or [])},
        "WEBHOOK_HOST_ALLOWLIST": {"count": len(getattr(settings, "WEBHOOK_HOST_ALLOWLIST", []) or [])},
        "INTEGRATION_MASTER_KEY": {"set": has("INTEGRATION_MASTER_KEY"),
                                   "previousKeys": len(getattr(settings, "INTEGRATION_MASTER_KEY_PREVIOUS", []) or [])},
        "EMAIL_DELIVERY": {"mode": delivery_mode()},
        "DEBUG": {"enabled": bool(settings.DEBUG)},
        "RATE_LIMIT_ENABLED": {"enabled": bool(getattr(settings, "RATE_LIMIT_ENABLED", True))},
        "REMINDER_OFFSETS_MINUTES": {"value": list(getattr(settings, "REMINDER_OFFSETS_MINUTES", []))},
    }
    problems = []
    if not env_settings["PUBLIC_BASE_URL"]["https"]:
        problems.append("PUBLIC_BASE_URL should be an https URL in production.")
    if not env_settings["INTEGRATION_MASTER_KEY"]["set"]:
        problems.append("INTEGRATION_MASTER_KEY is required to store destination secrets.")
    if settings.DEBUG:
        problems.append("DJANGO_DEBUG must be off in production.")
    if Destination.objects.filter(enabled=True).exists() and not env_settings["WEBHOOK_HOST_ALLOWLIST"]["count"]:
        problems.append("Enabled destinations need their hosts on WEBHOOK_HOST_ALLOWLIST.")
    return {
        "environmentSettings": env_settings,
        "environmentSettingsNote": "Process-environment settings are read-only here. Change them in the deployment "
                                   "environment and restart web and worker (see the implementation playbook).",
        "database": {
            "events": Event.objects.count(),
            "publicEvents": Event.objects.exclude(visibility=Event.Visibility.DRAFT).count(),
            "formVersions": FormVersion.objects.count(),
            "hosts": Host.objects.count(),
            "topics": Topic.objects.count(),
            "destinations": Destination.objects.count(),
            "enabledDestinations": Destination.objects.filter(enabled=True).count(),
            "activeApiKeys": ApiKey.objects.filter(revoked_at__isnull=True).count(),
        },
        "problems": problems,
    }


HOST_PROVISIONING = {
    "note": "Instructions only. This server never provisions hosts, runs shell commands or writes files.",
    "steps": [
        "Provision PostgreSQL 15+ and a container host; build the image from the repository Dockerfile.",
        "Set DJANGO_SECRET_KEY, DJANGO_ALLOWED_HOSTS, PUBLIC_BASE_URL (https), POSTGRES_* and INTEGRATION_MASTER_KEY "
        "in the process environment (secret manager), never in the database or via MCP.",
        "Run `manage.py migrate`, then `manage.py sync_roles`, then create the first superuser with "
        "`manage.py bootstrap_admin` from a trusted shell (never via an API key).",
        "Run the image twice: `web` behind a TLS-terminating proxy with rate limits, and `worker` for the outbox.",
        "Set WEBHOOK_HOST_ALLOWLIST before enabling destinations; set EMAIL_DELIVERY and SMTP settings for mail.",
        "Create scoped API keys in the studio (Settings → API keys); give agents the least scopes needed.",
        "Verify with get_configuration_readiness and the webinar://config/current resource.",
    ],
}


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


def _get(model, **lookup):
    obj = model.objects.filter(**lookup).first()
    if obj is None:
        raise ToolError(404, "not_found", "Not found.")
    return obj


def _validation(exc):
    if hasattr(exc, "error_dict"):
        fields = {k: str(v[0].messages[0]) if hasattr(v[0], "messages") else str(v[0]) for k, v in exc.error_dict.items()}
        return ToolError(400, "validation_error", "Request validation failed.", fields)
    return ToolError(400, "validation_error", "; ".join(exc.messages) or "Invalid request.")


def _mgmt(view, method, key, body=None, *, publication_confirmed=False, **kwargs):
    """Run management logic with this tool's (already checked) scopes instead of the view's.

    ``publication_confirmed`` is set only by gated publication tools after ``_gate``
    consumed a token for the same change; every other call hits the REST publication gate.
    """
    if method in ("POST", "PATCH") and body is None:
        body = {}
    request = SimpleNamespace(method=method, headers={}, publication_confirmed=publication_confirmed)
    return management._run(view.logic, request, key, body, (), kwargs)


def list_catalog(args, key):
    kind = args["kind"]
    if kind == "hosts":
        return 200, {"results": [management.serialize_host(h) for h in Host.objects.all()[:500]]}
    if kind == "topics":
        return 200, {"results": [management.serialize_topic(t) for t in Topic.objects.all()[:500]]}
    if kind == "formVersions":
        return 200, {"results": [management.serialize_form(f) for f in FormVersion.objects.all()[:500]]}
    return 200, {"results": [{"id": str(s.pk), "eventId": str(s.event_id), "name": s.name, "timezone": s.timezone}
                             for s in Series.objects.all()[:500]]}


def create_series(args, key):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    event = _get(Event, pk=args["eventId"])
    try:
        ZoneInfo(args["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolError(400, "validation_error", "Request validation failed.", {"timezone": "Unknown IANA timezone."})
    with transaction.atomic():
        series = Series.objects.create(event=event, name=args["name"], timezone=args["timezone"])
        audit("series.created", api_key=key, obj=series)
    return 201, {"id": str(series.pk), "eventId": str(event.pk), "name": series.name, "timezone": series.timezone}


def schedule_recurring_sessions(args, key):
    """Explicit recurrence: one session per listed wall-clock start (never RRULE-expanded)."""
    event = _get(Event, pk=args["eventId"])
    base = {k: args[k] for k in ("timezone", "durationMinutes", "seriesId", "registrationOpen") if k in args}
    if args.get("dryRun", False):
        return 200, {"dryRun": True, "preview": {"eventId": str(event.pk), "sessions": [
            {**base, "localStart": s} for s in args["localStarts"]]}}
    created = []
    with transaction.atomic():
        for start in args["localStarts"]:
            status, payload = _mgmt(management.event_occurrences, "POST", key, {**base, "localStart": start}, pk=event.pk)
            if status >= 400:
                transaction.set_rollback(True)
                return status, {**payload, "failedLocalStart": start}
            created.append(payload)
    return 201, {"results": created}


def set_event_cover(args, key):
    from webinars import covers
    from webinars.models import CoverImage
    from webinars.services import FormErrors, event_form_data, save_event

    event = _get(Event, pk=args["eventId"])
    if ("imageBase64" in args) == bool(args.get("remove")):
        raise ToolError(400, "validation_error", "Pass exactly one of imageBase64 or remove=true.")
    try:
        with transaction.atomic():
            cover = None
            if "imageBase64" in args:
                try:
                    raw = base64.b64decode(args["imageBase64"], validate=True)
                except (binascii.Error, ValueError):
                    raise ToolError(400, "validation_error", "imageBase64 is not valid base64.")
                if len(raw) > MAX_COVER_BYTES:
                    raise ToolError(400, "validation_error", "Use the studio upload for covers over 40 KB.")
                content_type, encoded, width, height = covers.process(raw)
                cover = CoverImage.objects.create(content_type=content_type, data=encoded, width=width, height=height,
                                                  byte_size=len(encoded), sha256=hashlib.sha256(encoded).hexdigest())
            data = event_form_data(event)
            if cover is not None:
                data["hero_image_url"] = ""
            save_event(data, api_key=key, instance=event, cover=cover)
    except FormErrors as exc:
        raise _validation(exc)
    except ValidationError as exc:
        raise _validation(exc)
    return 200, management.serialize_event(Event.objects.get(pk=event.pk))


def set_event_visibility(args, key):
    from webinars.services import FormErrors, publish_problems, set_visibility

    event = _get(Event, pk=args["eventId"])
    target = args["visibility"]
    problems = publish_problems(event) if target != Event.Visibility.DRAFT else []
    preview = {"eventId": str(event.pk), "from": event.visibility, "to": target, "problems": problems,
               "effect": "Public/unlisted events are served by the public API and embeds immediately."}
    gated = _gate("set_event_visibility", key, args, preview, state=event.revision)
    if gated:
        return gated
    if problems:
        raise ToolError(409, "not_ready", "The event is not ready to publish.", {"problems": "; ".join(problems)})
    try:
        set_visibility(event, target, api_key=key)
    except FormErrors as exc:
        raise _validation(exc)
    return 200, management.serialize_event(Event.objects.get(pk=event.pk))


def publish_recording(args, key):
    occ = _get(Occurrence, pk=args["sessionId"])
    preview = {"sessionId": str(occ.pk), "status": occ.status, "alreadyPublished": occ.recording_published,
               "effect": "Recording URL becomes public and recording.published is queued for destinations."}
    gated = _gate("publish_recording", key, args, preview, state=str(occ.recording_published_at))
    if gated:
        return gated
    return _mgmt(management.occurrence_recording, "POST", key, {"recordingUrl": args["recordingUrl"]},
                 publication_confirmed=True, pk=occ.pk)


def delete_draft_event(args, key):
    from webinars.models import Registration

    event = _get(Event, pk=args["eventId"])
    preview = {"eventId": str(event.pk), "visibility": event.visibility,
               "sessions": event.occurrences.count(),
               "hasRegistrations": Registration.objects.filter(occurrence__event=event).exists()}
    gated = _gate("delete_draft_event", key, args, preview, state=event.revision)
    if gated:
        return gated
    return _mgmt(management.event_detail, "DELETE", key, pk=event.pk)


def finalize_attendance(args, key):
    from webinars import waiting_room

    occ = _get(Occurrence, pk=args["sessionId"])
    state = waiting_room.room_state(occ).state if occ.waiting_room_enabled else None
    preview = {"sessionId": str(occ.pk), "waitingRoomEnabled": occ.waiting_room_enabled, "roomState": state,
               "alreadyFinalized": occ.attendance_finalized_at is not None,
               "effect": "Checked-in registrants become attended, the rest no_show (once, after the room ends)."}
    gated = _gate("finalize_attendance", key, args, preview, state=str(occ.attendance_finalized_at))
    if gated:
        return gated
    updated = waiting_room.finalize_attendance(occ)
    if updated is None:
        raise ToolError(409, "conflict", "Attendance cannot be finalized now (room not ended, disabled, or already done).")
    audit("attendance.finalized", api_key=key, obj=occ, updated=updated)
    return 200, {"sessionId": str(occ.pk), "updated": updated}


# ---- destinations -----------------------------------------------------------

DESTINATION_FIELDS = {
    "name": {"type": "string", "minLength": 1, "maxLength": 200},
    "kind": {"type": "string", "enum": list(Destination.Kind.values)},
    "url": {"type": "string", "maxLength": 1000, "format": "uri", "description": "https; host must be on WEBHOOK_HOST_ALLOWLIST."},
    "enabled": {"type": "boolean"},
    "eventTypes": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    "allowedGroups": {"type": "array", "items": {"type": "string", "enum": list(Destination.GROUPS)}, "maxItems": 10},
    "requiredConsents": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    "includePrivateLinks": {"type": "boolean"},
    "fieldMapping": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 200}},
    "maxAttempts": {"type": "integer", "minimum": 1, "maximum": 20},
}
_DEST_MODEL = {"name": "name", "kind": "kind", "url": "url", "enabled": "enabled", "eventTypes": "event_types",
               "allowedGroups": "allowed_groups", "requiredConsents": "required_consents",
               "includePrivateLinks": "include_private_links", "fieldMapping": "field_mapping", "maxAttempts": "max_attempts"}


def _destination_form(args, instance):
    from webinars.admin import DestinationForm

    data = {}
    if instance is not None:
        data = {m: getattr(instance, m) for m in _DEST_MODEL.values()}
    else:
        data = {"kind": Destination.Kind.GENERIC, "enabled": False, "event_types": [], "allowed_groups": [],
                "required_consents": [], "include_private_links": False, "field_mapping": {}, "max_attempts": 8}
    for camel, model_name in _DEST_MODEL.items():
        if camel in args:
            data[model_name] = args[camel]
    for name in ("event_types", "allowed_groups", "required_consents", "field_mapping"):
        data[name] = json.dumps(data[name])
    if not data.get("enabled"):
        data.pop("enabled", None)  # unchecked checkbox semantics
    if not data.get("include_private_links"):
        data.pop("include_private_links", None)
    return DestinationForm(data=data, instance=instance)


def save_destination(args, key):
    from webinars.audit import audit as _audit

    instance = _get(Destination, pk=args["destinationId"]) if args.get("destinationId") else None
    if instance is None and "name" not in args or instance is None and "url" not in args:
        raise ToolError(400, "validation_error", "name and url are required to create a destination.")
    form = _destination_form(args, instance)
    valid = form.is_valid()  # validation only; nothing saved before confirmation
    if not valid:
        raise ToolError(400, "validation_error", "Request validation failed.",
                        {k: str(v[0]) for k, v in form.errors.items()})
    preview = {"create": instance is None, "changedFields": sorted(form.changed_data),
               "urlHost": _url_host(form.cleaned_data.get("url")), "enabled": form.cleaned_data.get("enabled"),
               "includePrivateLinks": form.cleaned_data.get("include_private_links"),
               "effect": "Enabled destinations receive registrant data for their event types and groups."}
    gated = _gate("save_destination", key, args, preview, state=str(instance.updated_at) if instance else None)
    if gated:
        return gated
    with transaction.atomic():
        dest = form.save()
        _audit("destination.saved", api_key=key, obj=dest, created=instance is None, fields=sorted(form.changed_data))
    return 201 if instance is None else 200, serialize_destination(dest)


def set_destination_credentials(args, key):
    from webinars.services import set_credentials

    dest = _get(Destination, pk=args["destinationId"])
    creds = args["credentials"]
    preview = {"destinationId": str(dest.pk), "replaceNames": sorted(creds), "currentNames": serialize_destination(dest)["credentialNames"],
               "effect": "Replaces all stored credentials (encrypted at rest; values are never returned)."}
    gated = _gate("set_destination_credentials", key, args, preview, state=str(dest.updated_at))
    if gated:
        return gated
    try:
        set_credentials(dest, creds, user=None)
    except ValidationError as exc:
        raise _validation(exc)
    audit("destination.credentials_set", api_key=key, obj=dest, fields=len(creds))
    return 200, serialize_destination(Destination.objects.get(pk=dest.pk))


def set_destination_signing_secret(args, key):
    from webinars.services import set_signing_secret

    dest = _get(Destination, pk=args["destinationId"])
    preview = {"destinationId": str(dest.pk), "replacesExisting": bool(dest.signing_secret_encrypted),
               "effect": "Deliveries are signed with the new secret immediately; update the receiver first or together."}
    gated = _gate("set_destination_signing_secret", key, args, preview, state=str(dest.updated_at))
    if gated:
        return gated
    try:
        set_signing_secret(dest, user=None, secret=args["secret"])
    except ValidationError as exc:
        raise _validation(exc)
    audit("destination.signing_secret_set", api_key=key, obj=dest, generated=False)
    return 200, serialize_destination(Destination.objects.get(pk=dest.pk))


def delivery_status(args, key):
    qs = OutboxMessage.objects.all()
    if args.get("destinationId"):
        qs = qs.filter(destination_id=args["destinationId"])
    counts = {row["status"]: row["n"] for row in qs.values("status").annotate(n=Count("id"))}
    dead = qs.filter(status=OutboxMessage.Status.DEAD).order_by("-created_at")[:50]
    return 200, {"counts": counts, "dead": [
        {"id": str(m.pk), "destinationId": str(m.destination_id), "eventType": m.event_type, "attempts": m.attempts,
         "lastStatusCode": m.last_status_code, "createdAt": m.created_at.isoformat()} for m in dead]}


def requeue_dead_messages(args, key):
    from webinars.delivery import requeue_message

    ids = list(dict.fromkeys(args["messageIds"]))
    messages = list(OutboxMessage.objects.filter(pk__in=ids, status=OutboxMessage.Status.DEAD))
    preview = {"requested": len(ids), "deadAndEligible": [str(m.pk) for m in messages],
               "effect": "Eligible messages are re-sent to their destination by the worker."}
    gated = _gate("requeue_dead_messages", key, args, preview)
    if gated:
        return gated
    requeued = [str(m.pk) for m in messages if requeue_message(m, user=None)]
    audit("outbox.requeued", api_key=key, count=len(requeued))
    return 200, {"requeued": requeued, "skipped": len(ids) - len(requeued)}


def preview_destination_contract(args, key):
    """Dry-run 'test send': what this destination would receive. Nothing is sent or queued."""
    from webinars import outbox

    dest = _get(Destination, pk=args["destinationId"])
    groups = sorted(set(outbox.ALWAYS_GROUPS) | set(dest.allowed_groups or []))
    return 200, {"dryRun": True, "sent": False, "destination": serialize_destination(dest),
                 "eventTypes": list(dest.event_types or []) or list(outbox.EVENT_TYPES), "payloadGroups": groups,
                 "privateLinks": dest.include_private_links,
                 "note": "Live test sends are not available over MCP; use the studio integration test or a staging receiver."}


def test_fixture_payload(destination, delivery_id):
    """The only payload a test send may carry: fixed synthetic data, no person, no links."""
    from django.utils import timezone

    from webinars import outbox

    now = timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schemaVersion": outbox.SCHEMA_VERSION,
        "eventType": TEST_EVENT_TYPE,
        "occurredAt": now,
        "event": {
            "id": "00000000-0000-0000-0000-000000000000", "slug": "test-delivery", "title": "Test delivery (fixture)",
            "summary": "Synthetic test message. Contains no personal data.", "heroImageUrl": None,
            "aida": {"attention": "", "interest": "", "decision": "", "action": ""}, "learningOutcomes": [],
            "hosts": [], "topics": [], "thankYou": {"mode": "message", "message": ""}, "cta": {}, "revision": 0,
            "occurrence": {"id": None, "test": True},
        },
        "delivery_metadata": {"deliveryId": str(delivery_id), "destinationId": str(destination.pk),
                              "destinationKind": destination.kind, "eventType": TEST_EVENT_TYPE, "createdAt": now,
                              "test": True},
    }


def send_test_delivery(args, key):
    """Queue the fixed test fixture for one existing destination. The worker delivers it
    through the normal path (SSRF-pinned webhook or the destination's native provider);
    no URL, header or payload is accepted from the caller."""
    import uuid

    from webinars import outbox

    dest = _get(Destination, pk=args["destinationId"])
    if not dest.enabled:
        # The worker parks messages for disabled destinations; a test would fire later, unexpectedly.
        raise ToolError(409, "conflict", "Enable the destination before sending a test.")
    preview = {"destination": serialize_destination(dest),
               "payload": outbox.filter_for_destination(test_fixture_payload(dest, uuid.UUID(int=0)), dest),
               "effect": "One synthetic message is queued; the worker sends it to this destination's configured target.",
               "warnings": ["Destinations with requiredConsents may cancel the test (no person to consent)."]
               if dest.required_consents else []}
    gated = _gate("send_test_delivery", key, args, preview, state=str(dest.updated_at))
    if gated:
        return gated
    with transaction.atomic():
        delivery_id = uuid.uuid4()
        message = OutboxMessage.objects.create(
            id=delivery_id, destination=dest, event_type=TEST_EVENT_TYPE,
            # Canonical (unmapped) groups only; the worker filters and maps at send time.
            payload=test_fixture_payload(dest, delivery_id), person=None,
        )
        audit("destination.test_queued", api_key=key, obj=dest, message_id=str(message.pk))
    return 202, {"queued": True, "messageId": str(message.pk), "destinationId": str(dest.pk),
                 "next": "Check get_delivery_status for the result."}


# ---- team (team_admin: Editor/Analyst only) -----------------------------------------


def _member_row(user, key=None):
    from webinars.invitations import key_may_manage, role_of

    return {"id": user.pk, "username": user.username, "email": user.email, "role": role_of(user),
            "isActive": user.is_active, "keyManageable": key_may_manage(user, key)}


def _invitation_row(inv):
    return {"id": str(inv.pk), "email": inv.email, "role": inv.role, "status": inv.status,
            "emailSent": inv.email_sent, "createdAt": inv.created_at.isoformat(), "expiresAt": inv.expires_at.isoformat()}


def list_team(args, key):
    from django.contrib.auth import get_user_model

    from webinars.models import Invitation

    users = get_user_model().objects.order_by("username")[:500]
    return 200, {"members": [_member_row(u, key) for u in users],
                 "invitations": [_invitation_row(i) for i in Invitation.objects.all()[:200]],
                 "note": "Keys manage Editor and Analyst access only, and only roles whose scopes the key holds "
                         "(Editor: content_write + publication; Analyst: reporting). Administrators and superusers "
                         "are managed by a superuser."}


def _require_role_scopes(key, role):
    from webinars import invitations

    if role not in invitations.KEY_MANAGED_ROLES:
        raise ToolError(403, "forbidden", "API keys may only grant Editor or Analyst.")
    missing = invitations.missing_scopes_for_role(key, role)
    if missing:
        raise ToolError(403, "scope_escalation", f"Granting {role} needs this key to hold: {', '.join(missing)}.")


def invite_member(args, key):
    from webinars import invitations

    _require_role_scopes(key, args["role"])
    preview = {"email": args["email"], "role": args["role"],
               "effect": "An invitation email with a single-use link is sent. The link is never returned here."}
    gated = _gate("invite_member", key, args, preview)
    if gated:
        return gated
    try:
        invitation, _token = invitations.create_invitation(actor=None, api_key=key, email=args["email"], role=args["role"])
    except PermissionDenied as exc:
        raise ToolError(403, "forbidden", str(exc))
    except ValidationError as exc:
        raise _validation(exc)
    del _token  # delivered by email only; never returned or logged
    return 201, _invitation_row(invitation)


def revoke_invitation(args, key):
    from webinars import invitations
    from webinars.models import Invitation

    invitation = _get(Invitation, pk=args["invitationId"])
    if invitation.role not in invitations.KEY_MANAGED_ROLES:
        raise ToolError(403, "forbidden", "Administrator invitations are managed by a superuser.")
    _require_role_scopes(key, invitation.role)
    gated = _gate("revoke_invitation", key, args, _invitation_row(invitation), state=invitation.status)
    if gated:
        return gated
    invitations.revoke_invitation(actor=None, invitation=invitation, api_key=key)
    return 200, _invitation_row(Invitation.objects.get(pk=invitation.pk))


def update_member(args, key):
    from django.contrib.auth import get_user_model

    from webinars import invitations

    member = _get(get_user_model(), pk=args["memberId"])
    if not invitations.key_may_manage(member, key):
        raise ToolError(403, "forbidden", "API keys may only manage Editor/Analyst accounts without extra "
                                          "permissions, and only roles whose scopes the key holds.")
    if "role" in args:
        _require_role_scopes(key, args["role"])
    before = _member_row(member, key)
    preview = {"member": before, "changes": {k: args[k] for k in ("role", "email", "isActive") if k in args}}
    gated = _gate("update_member", key, args, preview, state=[before["role"], before["email"], before["isActive"]])
    if gated:
        return gated
    try:
        member, changed = invitations.update_member(actor=None, api_key=key, member=member, role=args.get("role"),
                                                    email=args.get("email"), is_active=args.get("isActive"))
    except PermissionDenied as exc:
        raise ToolError(403, "forbidden", str(exc))
    except ValidationError as exc:
        raise _validation(exc)
    return 200, {**_member_row(member, key), "changed": sorted(set(changed))}


# ---- API keys ------------------------------------------------------------------


def create_api_key(args, key):
    from webinars.api.auth import generate_api_key

    name = args["name"].strip()
    if not name:
        raise ToolError(400, "validation_error", "Request validation failed.", {"name": "Give the key a name."})
    requested = list(dict.fromkeys(args["scopes"]))
    held = set(key.scopes or [])
    denied = [s for s in requested if s not in DELEGABLE_SCOPES or s not in held]
    if denied or not requested:
        raise ToolError(403, "scope_escalation", "You may grant only delegable scopes that this key itself holds.",
                        {"scopes": ", ".join(denied) or "Choose at least one scope."})
    preview = {"name": name, "scopes": requested, "effect": "A new key is created; its secret is shown once."}
    gated = _gate("create_api_key", key, args, preview)
    if gated:
        return gated
    new_key, raw = generate_api_key(name, requested)
    audit("apikey.created", api_key=key, obj=new_key, scopes=requested)
    return 201, {**serialize_key(new_key), "secret": raw, "secretNote": "Shown once. Store it in a secret manager."}


def revoke_api_key(args, key):
    from django.utils import timezone

    target = _get(ApiKey, pk=args["keyId"])
    if S.KEYS_ADMIN in (target.scopes or []) or not set(target.scopes or []) <= set(key.scopes or []):
        raise ToolError(403, "forbidden", "Keys with scopes beyond yours or with keys_admin are revoked in the studio.")
    preview = {"key": serialize_key(target), "self": target.pk == key.pk}
    gated = _gate("revoke_api_key", key, args, preview, state=str(target.revoked_at))
    if gated:
        return gated
    if ApiKey.objects.filter(pk=target.pk, revoked_at__isnull=True).update(revoked_at=timezone.now()):
        audit("apikey.revoked", api_key=key, obj=target, prefix=target.prefix)
    return 200, serialize_key(ApiKey.objects.get(pk=target.pk))


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = [
    {"name": "get_configuration_readiness", "scopes": CONFIG, "readOnly": True, "handler": lambda a, k: (200, readiness()),
     "description": "Redacted readiness of environment settings (set/unset, counts) and database configuration. "
                    "Environment settings are read-only; the result explains what to change and where.",
     "inputSchema": _schema({})},
    {"name": "get_host_provisioning_instructions", "scopes": CONFIG, "readOnly": True,
     "handler": lambda a, k: (200, HOST_PROVISIONING),
     "description": "Step-by-step host provisioning instructions. Instructions only; nothing is executed.",
     "inputSchema": _schema({})},
    {"name": "list_catalog", "scopes": CATALOG_READ, "readOnly": True, "handler": list_catalog,
     "description": "List hosts, topics, form versions or series.",
     "inputSchema": _schema({"kind": {"type": "string", "enum": ["hosts", "topics", "formVersions", "series"]}}, ["kind"])},
    {"name": "update_host", "scopes": CONTENT,
     "handler": lambda a, k: _mgmt(management.host_detail, "PATCH", k, a["changes"], slug=a["slug"]),
     "description": "Update a host (speaker) by slug.",
     "inputSchema": _schema({"slug": {"type": "string", "maxLength": 120},
                             "changes": management.input_schema(management.HOST_FIELDS, Host)}, ["slug", "changes"])},
    {"name": "update_topic", "scopes": CONTENT,
     "handler": lambda a, k: _mgmt(management.topic_detail, "PATCH", k, a["changes"], slug=a["slug"]),
     "description": "Update a topic (tag) by slug.",
     "inputSchema": _schema({"slug": {"type": "string", "maxLength": 120},
                             "changes": management.input_schema(management.TOPIC_FIELDS, Topic)}, ["slug", "changes"])},
    {"name": "create_form_version", "scopes": CONTENT,
     "handler": lambda a, k: _mgmt(management.form_versions, "POST", k, dict(a)),
     "description": "Create an immutable registration form version (canonical keys; see webinar://docs/field-dictionary).",
     "inputSchema": management.input_schema(management.FORM_FIELDS, FormVersion, required=("version", "requiredFields"))},
    {"name": "update_form_version", "scopes": CONTENT,
     "handler": lambda a, k: _mgmt(management.form_version_detail, "PATCH", k, a["changes"], version=a["version"]),
     "description": "Change a form version's description or active flag (other fields are immutable).",
     "inputSchema": _schema({"version": {"type": "string", "maxLength": 64}, "changes": _schema(
         {"description": {"type": "string", "maxLength": 300}, "isActive": {"type": "boolean"}})}, ["version", "changes"])},
    {"name": "set_event_cta", "scopes": CONTENT,
     "handler": lambda a, k: _mgmt(management.event_detail, "PATCH", k,
                                   {c: a[c] for c in ("registerLabel", "confirmLabel") if c in a}, pk=a["eventId"]),
     "description": "Set the registration and confirm button labels (blank restores defaults).",
     "inputSchema": _schema({"eventId": _UUID, "registerLabel": {"type": "string", "maxLength": 60},
                             "confirmLabel": {"type": "string", "maxLength": 60}}, ["eventId"])},
    {"name": "set_event_cover", "scopes": CONTENT, "handler": set_event_cover,
     "description": "Attach an uploaded cover (PNG/JPEG/WebP, base64, ≤40 KB; re-encoded, metadata stripped) "
                    "or remove it (remove=true). Use heroImageUrl via update_event for hosted images.",
     "inputSchema": _schema({"eventId": _UUID, "imageBase64": {"type": "string", "maxLength": 56000},
                             "remove": {"type": "boolean"}}, ["eventId"])},
    {"name": "create_series", "scopes": CONTENT, "handler": create_series,
     "description": "Create a named series for an event (groups explicit recurring sessions).",
     "inputSchema": _schema({"eventId": _UUID, "name": {"type": "string", "minLength": 1, "maxLength": 200},
                             "timezone": {"type": "string", "maxLength": 64}}, ["eventId", "name", "timezone"])},
    {"name": "schedule_recurring_sessions", "scopes": CONTENT, "handler": schedule_recurring_sessions,
     "description": "Create up to 52 sessions from explicit wall-clock starts in one transaction (all or nothing). "
                    "dryRun=true previews without writing.",
     "inputSchema": _schema({
         "eventId": _UUID, "timezone": {"type": "string", "maxLength": 64},
         "localStarts": {"type": "array", "maxItems": MAX_BATCH_SESSIONS,
                         "items": {"type": "string", "pattern": management._LOCAL_START_RE.pattern}},
         "durationMinutes": {"type": "integer", "minimum": 1, "maximum": 1440},
         "seriesId": {"type": ["string", "null"], "maxLength": 20},
         "registrationOpen": {"type": "boolean"}, "dryRun": {"type": "boolean", "default": False},
     }, ["eventId", "timezone", "localStarts"])},
    {"name": "delete_draft_event", "scopes": CONTENT, "handler": delete_draft_event, "gated": True,
     "description": "Delete a draft event without registrations. Gated: dry run, then confirmToken.",
     "inputSchema": _schema({"eventId": _UUID}, ["eventId"], gated=True)},
    {"name": "set_event_visibility", "scopes": PUBLISH, "handler": set_event_visibility, "gated": True,
     "description": "Publish (public/unlisted) or unpublish (draft) an event after readiness checks. Gated.",
     "inputSchema": _schema({"eventId": _UUID, "visibility": {"type": "string", "enum": list(Event.Visibility.values)}},
                            ["eventId", "visibility"], gated=True)},
    {"name": "publish_recording", "scopes": PUBLISH, "handler": publish_recording, "gated": True,
     "description": "Publish a session recording URL (https) and notify destinations. Gated.",
     "inputSchema": _schema({"sessionId": _UUID, "recordingUrl": {"type": "string", "maxLength": 1000, "format": "uri"}},
                            ["sessionId", "recordingUrl"], gated=True)},
    {"name": "finalize_attendance", "scopes": ATTEND, "handler": finalize_attendance, "gated": True,
     "description": "Finalize waiting-room attendance for an ended session. Gated.",
     "inputSchema": _schema({"sessionId": _UUID}, ["sessionId"], gated=True)},
    {"name": "list_destinations", "scopes": (S.CONFIG_READ, S.DELIVERY_ADMIN), "readOnly": True,
     "handler": lambda a, k: (200, {"results": [serialize_destination(d) for d in Destination.objects.order_by("name")[:200]]}),
     "description": "Destinations with redacted URL (host only), credential names (never values) and secret presence.",
     "inputSchema": _schema({})},
    {"name": "get_delivery_status", "scopes": (S.CONFIG_READ, S.DELIVERY_ADMIN), "readOnly": True, "handler": delivery_status,
     "description": "Outbox counts by status and recent dead-lettered deliveries (no payloads or errors bodies).",
     "inputSchema": _schema({"destinationId": _UUID})},
    {"name": "save_destination", "scopes": DELIVERY, "handler": save_destination, "gated": True,
     "description": "Create (no destinationId) or update a generic webhook destination. Gated; the dry run validates.",
     "inputSchema": _schema({"destinationId": _UUID, **DESTINATION_FIELDS}, gated=True)},
    {"name": "set_destination_credentials", "scopes": DELIVERY, "handler": set_destination_credentials, "gated": True,
     "description": "Replace a destination's provider credentials {name: value}. Set-only: values are encrypted and "
                    "never returned. Gated.",
     "inputSchema": _schema({"destinationId": _UUID, "credentials": {
         "type": "object", "additionalProperties": {"type": "string", "maxLength": 4000}}}, ["destinationId", "credentials"], gated=True)},
    {"name": "set_destination_signing_secret", "scopes": DELIVERY, "handler": set_destination_signing_secret, "gated": True,
     "description": "Rotate the webhook signing secret to a caller-supplied value (≥32 chars). Set-only. Gated.",
     "inputSchema": _schema({"destinationId": _UUID, "secret": {"type": "string", "minLength": 32, "maxLength": 512}},
                            ["destinationId", "secret"], gated=True)},
    {"name": "preview_destination_contract", "scopes": DELIVERY, "readOnly": True, "handler": preview_destination_contract,
     "description": "Dry-run test send: the event types, payload groups and link policy this destination would receive.",
     "inputSchema": _schema({"destinationId": _UUID}, ["destinationId"])},
    {"name": "requeue_dead_messages", "scopes": DELIVERY, "handler": requeue_dead_messages, "gated": True,
     "description": "Retry dead-lettered deliveries by id. Gated.",
     "inputSchema": _schema({"messageIds": {"type": "array", "maxItems": 100, "items": _UUID}}, ["messageIds"], gated=True)},
    {"name": "send_test_delivery", "scopes": DELIVERY, "handler": send_test_delivery, "gated": True,
     "description": "Queue the fixed synthetic test payload (no personal data) for one existing enabled destination. "
                    "No URL, header or payload is accepted. Gated.",
     "inputSchema": _schema({"destinationId": _UUID}, ["destinationId"], gated=True)},
    {"name": "list_team", "scopes": TEAM, "readOnly": True, "handler": list_team,
     "description": "Team members (username, email, role, active) and invitations. No tokens or passwords.",
     "inputSchema": _schema({})},
    {"name": "invite_member", "scopes": TEAM, "handler": invite_member, "gated": True,
     "description": "Email an Editor or Analyst invitation. The key must hold the role's scopes (Editor: content_write + "
                    "publication; Analyst: reporting). The link is sent by email only, never returned. Gated.",
     "inputSchema": _schema({"email": {"type": "string", "minLength": 3, "maxLength": 254},
                             "role": {"type": "string", "enum": ["Editor", "Analyst"]}}, ["email", "role"], gated=True)},
    {"name": "revoke_invitation", "scopes": TEAM, "handler": revoke_invitation, "gated": True,
     "description": "Revoke a pending Editor/Analyst invitation. Gated.",
     "inputSchema": _schema({"invitationId": _UUID}, ["invitationId"], gated=True)},
    {"name": "update_member", "scopes": TEAM, "handler": update_member, "gated": True,
     "description": "Change an Editor/Analyst member's role (Editor/Analyst), email or active flag. Administrators, "
                    "staff and superusers are refused. Gated.",
     "inputSchema": _schema({"memberId": {"type": "integer", "minimum": 1},
                             "role": {"type": "string", "enum": ["Editor", "Analyst"]},
                             "email": {"type": "string", "minLength": 3, "maxLength": 254},
                             "isActive": {"type": "boolean"}}, ["memberId"], gated=True)},
    {"name": "list_api_keys", "scopes": KEYS, "readOnly": True,
     "handler": lambda a, k: (200, {"results": [serialize_key(x) for x in ApiKey.objects.order_by("-created_at")[:200]]}),
     "description": "API key metadata (name, prefix, scopes). Secrets are never stored or returned.",
     "inputSchema": _schema({})},
    {"name": "create_api_key", "scopes": KEYS, "handler": create_api_key, "gated": True,
     "description": "Create a key limited to delegable scopes you hold. Returns the secret once. Gated.",
     "inputSchema": _schema({"name": {"type": "string", "minLength": 1, "maxLength": 200},
                             "scopes": {"type": "array", "maxItems": 10, "items": {"type": "string", "enum": [str(s) for s in DELEGABLE_SCOPES]}}},
                            ["name", "scopes"], gated=True)},
    {"name": "revoke_api_key", "scopes": KEYS, "handler": revoke_api_key, "gated": True,
     "description": "Revoke a key whose scopes you hold (not keys_admin keys). Gated.",
     "inputSchema": _schema({"keyId": _UUID}, ["keyId"], gated=True)},
]


def call(tool, args, key):
    if not has_scope(key, *tool["scopes"]):
        return 403, {"error": {"code": "forbidden", "message": "This API key lacks the required scope."}}
    try:
        return tool["handler"](args, key)
    except ToolError as exc:
        return exc.status, exc.body()
