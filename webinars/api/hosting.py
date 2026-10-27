"""Hosting providers over the management API (docs/hosting.md).

Scopes: ``hosting_admin`` (accounts and their set-only credentials), ``hosting_provision``
(preview / confirm / reconcile / manual links), ``config_read`` (GET of providers and an
occurrence's hosting state only). Every scope here is superuser-granted
(``services.PRIVILEGED_SCOPES``). Responses never contain credentials, join links, host
start links or embed URLs: only ``secretsSet`` / ``hasJoinUrl`` / ``hasHostUrl`` flags.

Creating, updating or attaching a provider session is two steps, like publication: the
first ``execute`` returns 428 with the preview and a single-use ``confirmToken``; repeat
the identical request with header ``Webinar-Confirm-Token``.
"""
from django.core.exceptions import ValidationError

from webinars import confirmation
from webinars.hosting import ADAPTERS, base, service
from webinars.models import ApiKey, HostingAccount, Occurrence, ProviderRegistrant

from .management import CONFIRM_HEADER, _ConfirmationRequired, _endpoint, _Invalid, _parse

ADMIN = (ApiKey.Scope.HOSTING_ADMIN,)
PROVISION = (ApiKey.Scope.HOSTING_PROVISION,)
PROVISION_READ = PROVISION + (ApiKey.Scope.CONFIG_READ,)
ADMIN_READ = ADMIN + (ApiKey.Scope.CONFIG_READ,)

PROVIDERS = [str(p) for p in HostingAccount.Provider.values]
ACTIONS = list(service.ACTIONS)

ACCOUNT_FIELDS = {
    "provider": ("provider", "str"),
    "name": ("name", "str"),
    "enabled": ("enabled", "bool"),
    "config": ("config", "dict"),
    "credentials": ("credentials", "dict"),
}
PREVIEW_FIELDS = {"accountId": ("account", "str"), "action": ("action", "str"), "externalId": ("external_id", "str")}
RECONCILE_FIELDS = {"decision": ("decision", "str"), "externalId": ("external_id", "str")}
MANUAL_LINK_FIELDS = {"accountId": ("account", "str"), "joinUrl": ("join_url", "str"), "embedUrl": ("embed_url", "str")}

_STR_MAP = {"type": "object", "additionalProperties": {"type": "string", "maxLength": 512}}
ACCOUNT_INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["provider", "name"],
    "properties": {
        "provider": {"type": "string", "enum": PROVIDERS},
        "name": {"type": "string", "minLength": 1, "maxLength": 200},
        "enabled": {"type": "boolean"},
        "config": {**_STR_MAP, "description": "Non-secret settings named by the provider's configFields."},
        "credentials": {**_STR_MAP, "writeOnly": True,
                        "description": "Set-only secrets named by credentialFields; blank keeps the stored value. Never returned."},
    },
}
ACCOUNT_UPDATE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {k: v for k, v in ACCOUNT_INPUT_SCHEMA["properties"].items() if k != "provider"},
}
PREVIEW_INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["accountId", "action"],
    "properties": {
        "accountId": {"type": "string", "format": "uuid"},
        "action": {"type": "string", "enum": ACTIONS},
        "externalId": {"type": "string", "maxLength": 128, "description": "Existing provider session/event ID (attach only)."},
    },
}
RECONCILE_INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["decision"],
    "properties": {
        "decision": {"type": "string", "enum": ["created", "not_created"],
                     "description": "What you found in the provider after an unknown outcome."},
        "externalId": {"type": "string", "maxLength": 128, "description": "Required with decision=created."},
    },
}
MANUAL_LINK_INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["accountId", "joinUrl"],
    "properties": {
        "accountId": {"type": "string", "format": "uuid"},
        "joinUrl": {"type": "string", "maxLength": 1000, "format": "uri", "writeOnly": True},
        "embedUrl": {"type": "string", "maxLength": 1000, "format": "uri", "writeOnly": True},
    },
}


def _validation(exc):
    if hasattr(exc, "error_dict"):
        return _Invalid({k: "; ".join(m for e in v for m in e.messages) for k, v in exc.error_dict.items()})
    return _Invalid(None, "; ".join(exc.messages) or "Invalid request.")


def _account(value, field="accountId"):
    import uuid

    try:
        pk = uuid.UUID(str(value))
    except ValueError:
        raise _Invalid({field: "Must be a UUID."})
    account = HostingAccount.objects.filter(pk=pk).first()
    if account is None:
        raise _Invalid({field: "Unknown hosting account."}, "Not found.", status=404, code="not_found")
    return account


def _occurrence(pk):
    return Occurrence.objects.select_related("event").get(pk=pk)


@_endpoint(PROVISION, ("GET",), read_scopes=PROVISION_READ)
def hosting_providers(request, key, body):
    return 200, {"results": [adapter.describe() for adapter in ADAPTERS.values()]}


@_endpoint(ADMIN, ("GET", "POST"), read_scopes=ADMIN_READ)
def hosting_accounts(request, key, body):
    if request.method == "GET":
        return 200, {"results": [service.serialize_account(a) for a in HostingAccount.objects.order_by("name")[:200]]}
    values = _parse(body, ACCOUNT_FIELDS, required=("provider", "name"))
    if values["provider"] not in PROVIDERS:
        raise _Invalid({"provider": f"Must be one of: {', '.join(PROVIDERS)}."})
    try:
        account, _warnings = service.save_account(
            provider=values["provider"], name=values["name"], config=values.get("config") or {},
            posted_secrets=values.get("credentials") or {}, enabled=values.get("enabled", False), api_key=key,
        )
    except ValidationError as exc:
        raise _validation(exc)
    return 201, service.serialize_account(account)


@_endpoint(ADMIN, ("GET", "PATCH"), read_scopes=ADMIN_READ)
def hosting_account_detail(request, key, body, pk):
    account = _account(pk, "id")
    if request.method == "GET":
        return 200, service.serialize_account(account)
    fields = {k: v for k, v in ACCOUNT_FIELDS.items() if k != "provider"}
    values = _parse(body, fields)
    try:
        account, _warnings = service.save_account(
            account, provider=account.provider, name=values.get("name", account.name),
            config=values.get("config", account.config or {}), posted_secrets=values.get("credentials") or {},
            enabled=values.get("enabled", account.enabled), api_key=key,
        )
    except ValidationError as exc:
        raise _validation(exc)
    return 200, service.serialize_account(account)


@_endpoint(PROVISION, ("GET",), read_scopes=PROVISION_READ)
def occurrence_hosting(request, key, body, pk):
    occ = _occurrence(pk)
    return 200, service.serialize_session(service.session_for(occ))


def _preview_args(body):
    values = _parse(body, PREVIEW_FIELDS, required=("accountId", "action"))
    if values["action"] not in ACTIONS:
        raise _Invalid({"action": f"Must be one of: {', '.join(ACTIONS)}."})
    return _account(values["accountId"]), values["action"], values.get("externalId", "")


@_endpoint(PROVISION, ("POST",))
def occurrence_hosting_preview(request, key, body, pk):
    occ = _occurrence(pk)
    account, action, external_id = _preview_args(body)
    return 200, service.preview(occ, account, action, external_id)


@_endpoint(PROVISION, ("POST",))
def occurrence_hosting_execute(request, key, body, pk):
    occ = _occurrence(pk)
    account, action, external_id = _preview_args(body)
    preview = service.preview(occ, account, action, external_id)
    if preview["problems"]:
        raise _Invalid({"action": "; ".join(preview["problems"])}, "This action cannot run yet.", status=409, code="conflict")
    if not getattr(request, "hosting_confirmed", False):
        material = {"occurrenceId": str(occ.pk), "accountId": str(account.pk), "action": action, "externalId": external_id}
        session = service.session_for(occ)
        state = f"{session.state}:{occ.schedule_generation}" if session is not None else f"none:{occ.schedule_generation}"
        headers = getattr(request, "headers", None) or {}
        token = headers.get(CONFIRM_HEADER, "")
        if not token:
            raise _ConfirmationRequired(preview, confirmation.issue(f"hosting.{action}", key, material, state))
        try:
            confirmation.consume(f"hosting.{action}", key, material, token, state)
        except confirmation.ConfirmationError as exc:
            raise _Invalid(None, exc.message, status=400, code=exc.code)
    try:
        result = service.execute(occ, account, action, external_id=external_id, api_key=key)
    except ValidationError as exc:
        raise _validation(exc)
    return 200, result


@_endpoint(PROVISION, ("POST",))
def occurrence_hosting_reconcile(request, key, body, pk):
    occ = _occurrence(pk)
    values = _parse(body, RECONCILE_FIELDS, required=("decision",))
    try:
        return 200, service.reconcile(occ, values["decision"], external_id=values.get("externalId", ""), api_key=key)
    except ValidationError as exc:
        raise _validation(exc)


# ---------------------------------------------------------------------------
# Riverside registrant reconciliation (shared by REST, MCP and studio)
#
# Lists are read-only (no writes, no provider calls) and carry identifiers and state
# only: no names, emails or links. Reconciling records what a person verified in
# Riverside; nothing is sent to Riverside. The pasted join link is set-only.
# ---------------------------------------------------------------------------

REGISTRANT_ACTION = "hosting.registrant_reconcile"
REGISTRANT_RECONCILE_FIELDS = {"registrationId": ("registration", "str"), "decision": ("decision", "str"),
                               "joinUrl": ("join_url", "str"), "externalId": ("external_id", "str")}
REGISTRANT_RECONCILE_INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["registrationId", "decision"],
    "properties": {
        "registrationId": {"type": "string", "format": "uuid",
                           "description": "From GET .../hosting/registrants; must belong to this occurrence."},
        "decision": {"type": "string", "enum": ["created", "not_created"],
                     "description": "What you verified in Riverside for this registrant."},
        "joinUrl": {"type": "string", "maxLength": 1000, "format": "uri", "writeOnly": True,
                    "description": "Required with decision=created: this registrant's own https Riverside link. "
                                   "Stored encrypted; never returned."},
        "externalId": {"type": "string", "maxLength": 128, "description": "Optional Riverside registrant ID (created only)."},
    },
}
REGISTRANT_SCHEMA = {
    "type": "object",
    "properties": {
        "registrationId": {"type": "string", "format": "uuid"},
        "state": {"type": "string", "enum": ["needs_reconciliation", "pending", "linked", "failed"]},
        "reconcilable": {"type": "boolean"},
        "attempts": {"type": "integer"},
        "boundToCurrentEvent": {"type": "boolean",
                                "description": "Whether this record was made for the session's current Riverside "
                                               "account and event. False for legacy rows and after a re-attach."},
        "updatedAt": {"type": ["string", "null"]},
    },
}


def _stalled_q():
    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    cutoff = timezone.now() - timedelta(seconds=service.STALE_IN_FLIGHT_SECONDS)
    return (Q(state=ProviderRegistrant.State.NEEDS_RECONCILIATION)
            | Q(state=ProviderRegistrant.State.PENDING, attempts__gt=0, updated_at__lt=cutoff))


def _unbound_q():
    """Rows not bound to their session's current account + event (incl. legacy blanks)."""
    from django.db.models import F, Q

    from webinars.models import HostedSession

    per_registrant = Q(session__mode=HostedSession.Mode.ATTACHED,
                       session__state__in=(HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE))
    return per_registrant & (
        Q(event_external_id="") | Q(event_account__isnull=True)
        | ~Q(event_external_id=F("session__external_id")) | ~Q(event_account_id=F("session__account_id")))


def stalled_registrants(occ, limit=200):
    """Riverside registrants of this occurrence whose outcome is unknown, or whose record
    belongs to another (or no) provider event. No writes: unbound rows are moved to
    ``needs_reconciliation`` by ``service.recover_stale_operations`` (run on every
    reconcile request) and are never admitted meanwhile."""
    return list(ProviderRegistrant.objects.select_related("session", "registration__person").filter(
        _stalled_q() | _unbound_q(), registration__occurrence_id=occ.pk, session__occurrence_id=occ.pk,
        session__provider=HostingAccount.Provider.RIVERSIDE,
    ).order_by("updated_at")[:limit])


def serialize_registrant(pr):
    return {"registrationId": str(pr.registration_id), "state": pr.state, "reconcilable": True,
            "attempts": pr.attempts, "boundToCurrentEvent": service.registrant_bound(pr, pr.session),
            "updatedAt": service._iso(pr.updated_at)}


def registrant_state(pr):
    """Confirmation state: any new attempt, outcome or reconciliation, and any change of the
    row's binding or of the session's account / provider event / link state (detach,
    re-attach) invalidates tokens."""
    s = pr.session
    return (f"{pr.state}:{pr.attempts}:{pr.updated_at.isoformat()}:{pr.session_id}"
            f":{pr.event_account_id or ''}:{pr.event_external_id}"
            f":{s.account_id or ''}:{s.external_id}:{s.mode}:{s.state}:{s.updated_at.isoformat()}")


def _session_binding(pr):
    return service.current_binding(pr.session)


def registrant_target(occ, registration_id):
    """The reconcilable registrant ``registration_id`` of ``occ`` (recovering a lost attempt first).

    Unknown and other-occurrence IDs get the same 404."""
    import uuid

    try:
        rid = uuid.UUID(str(registration_id))
    except ValueError:
        raise _Invalid({"registrationId": "Must be a UUID."})
    service.recover_stale_operations()
    pr = ProviderRegistrant.objects.select_related("session", "registration").filter(registration_id=rid).first()
    if pr is None or pr.registration.occurrence_id != occ.pk or pr.session.occurrence_id != occ.pk:
        raise _Invalid(None, "Not found.", status=404, code="not_found")
    if pr.session.provider != HostingAccount.Provider.RIVERSIDE:
        raise _Invalid(None, "Only Riverside registrants are reconciled here.", status=409, code="conflict")
    if pr.state != ProviderRegistrant.State.NEEDS_RECONCILIATION:
        raise _Invalid(None, "This registrant does not need reconciliation.", status=409, code="conflict")
    if _session_binding(pr) is None:
        raise _Invalid(None, "The session is not linked to a Riverside event; attach it first.", status=409,
                       code="conflict")
    return pr


def registrant_values(pr, values):
    """Validate before any token is issued or burned. Returns ``(decision, join_url, external_id)``."""
    decision = values.get("decision", "")
    join_url = (values.get("joinUrl") or "").strip()
    external_id = (values.get("externalId") or "").strip()
    adapter = service.adapter_for(pr.session)
    if decision == "created":
        if not base.https_link(join_url, getattr(adapter, "link_hosts", ())):
            raise _Invalid({"joinUrl": "Paste this registrant's own Riverside join link (https)."})
        if external_id and not service._valid_external_id(adapter, external_id):
            raise _Invalid({"externalId": "Enter a valid Riverside registrant ID."})
    elif decision == "not_created":
        if join_url or external_id:
            raise _Invalid({"decision": "joinUrl and externalId are only accepted with decision=created."})
    else:
        raise _Invalid({"decision": "Choose 'created' or 'not_created'."})
    return decision, join_url, external_id


def registrant_preview(occ, pr, decision, join_url, external_id):
    """What confirming records. Never contains the link (only whether one was given)."""
    return {
        "occurrenceId": str(occ.pk), "registrationId": str(pr.registration_id), "decision": decision,
        "currentState": pr.state, "attempts": pr.attempts, "joinUrlProvided": bool(join_url),
        "externalIdProvided": bool(external_id),
        "boundToCurrentEvent": service.registrant_bound(pr, pr.session),
        "providerAccountId": str(pr.session.account_id) if pr.session.account_id else None,
        "providerEventId": pr.session.external_id or None,
        "effect": ("Binds this registrant to the Riverside event above, stores their own link from that event "
                   "encrypted and admits them with it."
                   if decision == "created" else
                   "Binds this registrant to the Riverside event above and marks it failed so the worker sweep "
                   f"may retry there (attempts are not reset; at most {service.MAX_REGISTRANT_ATTEMPTS})."
                   ) + " Nothing is sent to Riverside here.",
    }


def apply_registrant_reconcile(pr, decision, join_url, external_id, *, user=None, api_key=None):
    try:
        service.reconcile_registrant(pr.registration, decision, join_url=join_url, external_id=external_id,
                                     user=user, api_key=api_key, expected_attempts=pr.attempts,
                                     expected_binding=_session_binding(pr))
    except ValidationError as exc:
        raise _Invalid(None, "; ".join(exc.messages) or "The registrant changed; preview again.", status=409,
                       code="conflict")
    pr = ProviderRegistrant.objects.get(pk=pr.pk)
    return {**serialize_registrant(pr), "reconcilable": False}


@_endpoint(PROVISION, ("GET",), read_scopes=PROVISION_READ)
def occurrence_registrants(request, key, body, pk):
    occ = _occurrence(pk)
    return 200, {"occurrenceId": str(occ.pk), "results": [serialize_registrant(pr) for pr in stalled_registrants(occ)],
                 "note": "Identifiers and state only. Look the registrant up in the studio or Riverside, then reconcile."}


@_endpoint(PROVISION, ("POST",))
def occurrence_registrant_reconcile(request, key, body, pk):
    occ = _occurrence(pk)
    values = _parse(body, REGISTRANT_RECONCILE_FIELDS, required=("registrationId", "decision"))
    pr = registrant_target(occ, values["registrationId"])
    decision, join_url, external_id = registrant_values(pr, values)
    expected = getattr(request, "hosting_expected_state", None)
    if expected is not None and expected != registrant_state(pr):
        raise _Invalid(None, "The registrant changed since the preview; preview again.", status=409, code="conflict")
    if not getattr(request, "hosting_confirmed", False):
        material = {"occurrenceId": str(occ.pk), "registrationId": str(pr.registration_id), "decision": decision,
                    "joinUrl": join_url, "externalId": external_id}
        state = registrant_state(pr)
        headers = getattr(request, "headers", None) or {}
        token = headers.get(CONFIRM_HEADER, "")
        if not token:
            raise _ConfirmationRequired(registrant_preview(occ, pr, decision, join_url, external_id),
                                        confirmation.issue(REGISTRANT_ACTION, key, material, state))
        try:
            confirmation.consume(REGISTRANT_ACTION, key, material, token, state)
        except confirmation.ConfirmationError as exc:
            raise _Invalid(None, exc.message, status=400, code=exc.code)
    return 200, apply_registrant_reconcile(pr, decision, join_url, external_id, api_key=key)


@_endpoint(PROVISION, ("POST",))
def occurrence_hosting_manual_link(request, key, body, pk):
    occ = _occurrence(pk)
    values = _parse(body, MANUAL_LINK_FIELDS, required=("accountId", "joinUrl"))
    account = _account(values["accountId"])
    try:
        return 200, service.set_manual_link(occ, account, join_url=values["joinUrl"],
                                            embed_url=values.get("embedUrl", ""), api_key=key)
    except ValidationError as exc:
        raise _validation(exc)
