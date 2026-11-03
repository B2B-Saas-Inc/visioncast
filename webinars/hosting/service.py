"""Hosting provisioning state machine (docs/hosting.md).

* Credentials are a JSON dict Fernet-encrypted with INTEGRATION_MASTER_KEY, set-only:
  a blank posted secret keeps the stored value; values are never returned or rendered.
* Nothing happens on publish. Every outbound action is an explicit preview -> confirm ->
  ``execute``. ``execute`` commits a ``ProvisioningOperation`` (in flight) *before* the
  call, makes the call outside any transaction or row lock, then records the outcome.
* Unknown outcomes (timeout, 5xx, 429, truncated or unparsable 2xx) put the session in
  ``needs_reconciliation``: providers cannot dedupe a create, so it is never re-sent
  until a person records what happened (``reconcile``).
* External sessions are never deleted: cancel/detach only change local state.
* Private links: the attendee join link goes to ``Occurrence.join_url`` (existing private
  field, used only by waiting-room admission); the host/start link is stored encrypted
  and only ever reported as ``hasHostUrl``.
"""
import json
import logging
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from webinars import crypto
from webinars.audit import audit
from webinars.models import (
    HostedSession,
    HostingAccount,
    Occurrence,
    ProviderRegistrant,
    ProvisioningOperation,
    Registration,
)

from . import ADAPTERS, base, get_adapter

logger = logging.getLogger("webinars.hosting")

ACTIONS = ("create", "update", "attach")
MAX_REGISTRANT_ATTEMPTS = 5
DOOR_RETRY_SECONDS = 30
BLOCKING_STATES = (HostedSession.State.IN_FLIGHT, HostedSession.State.NEEDS_RECONCILIATION)
_CAPABILITY_FLAG = {"create": "create", "update": "update", "attach": "attach", "register": "register_attendees"}


def adapter_for(session_or_provider):
    provider = getattr(session_or_provider, "provider", session_or_provider)
    adapter = get_adapter(provider)
    if adapter is None:
        raise ValidationError({"provider": "Unknown hosting provider."})
    return adapter


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def load_secrets(account):
    if not account.credentials_encrypted:
        return {}
    data = json.loads(crypto.decrypt_secret(account.credentials_encrypted))
    return data if isinstance(data, dict) else {}


def _secrets_or_empty(account):
    try:
        return load_secrets(account)
    except Exception:  # DecryptionError, bad JSON, missing/rotated master key (ImproperlyConfigured)
        return {}


def account_warnings(account):
    adapter = adapter_for(account)
    warnings = list(adapter.allowlist_problems()) + list(adapter.config_problems(account.config or {}))
    missing = adapter.validate_credentials(_secrets_or_empty(account))
    if missing:
        warnings.append("Some credentials are not set.")
    return warnings


def save_account(account=None, *, provider, name, config, posted_secrets, enabled, auto_provision=None, user=None,
                 api_key=None):
    """Create or update an account. Returns ``(account, warnings)``; raises ValidationError(dict).

    ``auto_provision`` (None keeps the stored value) makes this the account every new session is
    created on, see ``auto_provision``; only one account can have it, and only an enabled one
    whose provider can create sessions.
    """
    adapter = adapter_for(provider)
    errors = {}
    if auto_provision is None:
        auto_provision = bool(account.auto_provision) if account is not None else False
    auto_provision = bool(auto_provision)
    if auto_provision and not adapter.capabilities.create:
        errors["auto_provision"] = ["This provider can't create sessions, so it can't do it automatically."]
    elif auto_provision and not enabled:
        errors["auto_provision"] = ["Enable the account to create sessions automatically."]
    if account is not None and account.provider != provider:
        errors["provider"] = ["The provider of an existing account cannot change."]
    name = (name or "").strip()
    if not name or len(name) > 200:
        errors["name"] = ["Give the account a name (at most 200 characters)."]
    config = {k: (v.strip() if isinstance(v, str) else v) for k, v in (config or {}).items()}
    errors.update(adapter.validate_config(config))
    allowed = {f.name for f in adapter.credential_fields}
    posted = posted_secrets or {}
    for k in posted:
        if k not in allowed:
            errors.setdefault(k, []).append("Unknown credential.")
    existing = _secrets_or_empty(account) if account is not None and account.credentials_encrypted else {}
    merged = dict(existing)
    for k, v in posted.items():
        if k in allowed and isinstance(v, str) and v.strip():
            merged[k] = v.strip()
    if enabled:
        errors.update(adapter.validate_credentials(merged))
    if errors:
        raise ValidationError(errors)
    created = account is None
    if created:
        account = HostingAccount(provider=provider)
    account.name = name
    account.config = {f.name: config.get(f.name, "") for f in adapter.config_fields}
    account.enabled = bool(enabled)
    account.auto_provision = auto_provision
    changed_secrets = sorted(k for k in merged if merged.get(k) != existing.get(k))
    if merged:
        account.credentials_encrypted = crypto.encrypt_secret(json.dumps(merged, separators=(",", ":")))
    with transaction.atomic():
        account.save()
        if auto_provision:
            HostingAccount.objects.filter(auto_provision=True).exclude(pk=account.pk).update(auto_provision=False)
    audit("hosting.account_saved", user=user, api_key=api_key, obj=account, provider=provider, created=created,
          enabled=account.enabled, auto_provision=account.auto_provision, credentials_changed=changed_secrets)
    return account, account_warnings(account)


def serialize_account(account):
    adapter = adapter_for(account)
    secrets = _secrets_or_empty(account)
    return {
        "id": str(account.pk),
        "provider": account.provider,
        "name": account.name,
        "enabled": account.enabled,
        "autoProvision": account.auto_provision,
        "config": {f.name: (account.config or {}).get(f.name, "") for f in adapter.config_fields},
        "secretsSet": {f.name: bool(secrets.get(f.name)) for f in adapter.credential_fields},
        "capabilities": adapter.capabilities.as_dict(),
        "contract": adapter.contract,
        "warnings": account_warnings(account),
    }


# ---------------------------------------------------------------------------
# Automatic provisioning (the account marked auto_provision)
# ---------------------------------------------------------------------------


def auto_account():
    """The enabled account new sessions are created on, or None."""
    return HostingAccount.objects.filter(enabled=True, auto_provision=True).order_by("name").first()


def _auto_log():
    return logging.getLogger("webinars.hosting")


def auto_provision(occurrence_id, *, transport=None):
    """Create the provider session for a new occurrence on the auto account, and open its lobby.

    Runs after the occurrence's transaction commits (``admin.persist_occurrence``). Does nothing
    without an auto account, for a cancelled session, or when the session is already linked or
    has a join link. A provider failure is recorded on the hosted session like any other
    operation, never raised: the session page shows it and a person retries from there.
    Returns the serialized hosted session, or None when nothing was done.
    """
    from webinars import waiting_room

    account = auto_account()
    if account is None:
        return None
    occ = Occurrence.objects.select_related("event").filter(pk=occurrence_id).first()
    if occ is None or occ.status != Occurrence.Status.SCHEDULED or occ.join_url or session_for(occ) is not None:
        return None
    if not adapter_for(account).capabilities.create:
        return None
    try:
        if not occ.waiting_room_enabled:
            waiting_room.configure(occ, enabled=True)
            occ.refresh_from_db()
        return execute(occ, account, "create", transport=transport)
    except ValidationError as exc:
        _auto_log().warning("auto-provision skipped for %s: %s", occ.pk, getattr(exc, "message_dict", exc))
        return None


def auto_update(occurrence_id, *, transport=None):
    """After a reschedule, send the new time to the provider when the session was created
    automatically (the auto account owns it and can update). Same error handling as
    ``auto_provision``. Returns the serialized hosted session, or None."""
    occ = Occurrence.objects.select_related("event").filter(pk=occurrence_id).first()
    if occ is None or occ.status != Occurrence.Status.SCHEDULED:
        return None
    session = session_for(occ)
    if session is None or session.account is None or not session.account.auto_provision or not session.account.enabled:
        return None
    if session.state != HostedSession.State.OUT_OF_DATE or session.mode != HostedSession.Mode.PROVISIONED:
        return None
    if not adapter_for(session.account).capabilities.update:
        return None
    try:
        return execute(occ, session.account, "update", transport=transport)
    except ValidationError as exc:
        _auto_log().warning("auto-update skipped for %s: %s", occ.pk, getattr(exc, "message_dict", exc))
        return None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def session_for(occ):
    return HostedSession.objects.filter(occurrence_id=occ.pk).select_related("account").first()


def get_or_create_session(occ, account):
    session = session_for(occ)
    if session is not None:
        return session
    mode = HostedSession.Mode.MANUAL if not adapter_for(account).capabilities.provisioning else HostedSession.Mode.PROVISIONED
    try:
        with transaction.atomic():
            return HostedSession.objects.create(occurrence=occ, account=account, provider=account.provider, mode=mode)
    except IntegrityError:
        return session_for(occ)


def serialize_session(session, private=False):
    """Management projection: flags only, never join/host/embed URLs."""
    if session is None:
        return {"state": "none", "provider": None, "capabilities": None, "operations": []}
    occ = session.occurrence
    adapter = get_adapter(session.provider)
    linked = session.state in (HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE)
    return {
        "provider": session.provider,
        "accountId": str(session.account_id) if session.account_id else None,
        "mode": session.mode,
        "state": session.state,
        "externalId": session.external_id or None,
        "hasJoinUrl": bool(occ.join_url),
        "hasHostUrl": bool(session.host_url_encrypted),
        "embedUrlSet": bool(session.embed_url),
        "lastError": session.last_error or None,
        "lastSyncedAt": _iso(session.last_synced_at),
        "outOfDate": bool(linked and session.synced_schedule_generation is not None
                          and session.synced_schedule_generation != occ.schedule_generation),
        "capabilities": adapter.capabilities.as_dict() if adapter else None,
        "contract": adapter.contract if adapter else None,
        "operations": [
            {"id": str(op.pk), "action": op.action, "status": op.status, "statusCode": op.status_code,
             "error": op.error or None, "createdAt": _iso(op.created_at), "finishedAt": _iso(op.finished_at)}
            for op in session.operations.all()[:10]
        ],
    }


def _valid_external_id(adapter, value):
    check = getattr(adapter, "valid_external_id", None)
    if check is not None:
        return check(value)
    return bool(isinstance(value, str) and base.EXTERNAL_ID.match(value))


def preview(occ, account, action, external_id=""):
    """Description of what ``execute`` would send (no secrets). Its only write is turning
    lost in-flight operations into reconcilable ones (``recover_stale_operations``)."""
    recover_stale_operations()
    problems = []
    adapter = adapter_for(account)
    if action not in ACTIONS:
        return {"action": action, "provider": account.provider, "plans": [], "problems": ["Unknown action."]}
    if not account.enabled:
        problems.append("This hosting account is disabled.")
    if not getattr(adapter.capabilities, _CAPABILITY_FLAG[action]):
        problems.append(f"{adapter.label} does not support '{action}' here.")
    problems.extend(adapter.allowlist_problems())
    problems.extend(adapter.config_problems(account.config or {}))
    problems.extend(adapter.occurrence_problems(occ, action))
    if occ.status == Occurrence.Status.CANCELLED:
        problems.append("The session is cancelled.")
    session = session_for(occ)
    if session is not None:
        if session.account_id and session.account_id != account.pk and session.state != HostedSession.State.DETACHED:
            problems.append("This session is linked to another hosting account.")
        if session.state in BLOCKING_STATES:
            problems.append("A previous request has an unknown outcome or is still in flight: reconcile it first.")
        if action in ("create", "attach") and session.state in (HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE):
            problems.append("The session is already linked to a provider session.")
    if action == "update":
        external_id = session.external_id if session is not None else ""
        if session is None or session.state not in (HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE, HostedSession.State.FAILED) or not external_id:
            problems.append("Only a linked provider session can be updated.")
    if action == "attach" and not _valid_external_id(adapter, external_id or ""):
        problems.append("Enter a valid provider session/event ID.")
    plans = []
    try:
        plans = [p.as_preview() for p in adapter.plan(action, occ, account.config or {}, external_id or "")]
    except base.Unsupported:
        pass
    return {"action": action, "provider": account.provider, "externalId": external_id or None, "plans": plans,
            "problems": list(dict.fromkeys(problems))}


def execute(occ, account, action, *, external_id="", user=None, api_key=None, transport=None):
    result = preview(occ, account, action, external_id)
    if result["problems"]:
        raise ValidationError({"action": result["problems"]})
    external_id = result["externalId"] or ""
    adapter = adapter_for(account)
    try:
        secrets = load_secrets(account)
    except (crypto.DecryptionError, ValueError):
        raise ValidationError({"credentials": ["Stored credentials could not be decrypted; set them again."]})
    if adapter.validate_credentials(secrets):
        raise ValidationError({"credentials": ["Some credentials are not set."]})

    # Step A: durable in-flight record, committed before any outbound call.
    with transaction.atomic():
        locked_occ = Occurrence.objects.select_for_update().get(pk=occ.pk)
        session = get_or_create_session(locked_occ, account)
        session = HostedSession.objects.select_for_update().get(pk=session.pk)
        if session.state in BLOCKING_STATES:
            raise ValidationError({"action": ["A previous request has an unknown outcome or is still in flight."]})
        if action in ("create", "attach") and session.state in (HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE):
            raise ValidationError({"action": ["The session is already linked."]})
        previous_state = session.state
        op = ProvisioningOperation.objects.create(
            session=session, action=action, status=ProvisioningOperation.Status.IN_FLIGHT,
            idempotency_key=f"{session.pk}:{action}:{uuid.uuid4()}", schedule_generation=locked_occ.schedule_generation,
            actor_user=user if (user is not None and getattr(user, "is_authenticated", False)) else None,
            actor_api_key=api_key,
        )
        session.account = account
        session.provider = account.provider
        session.state = HostedSession.State.IN_FLIGHT
        session.save(update_fields=["account", "provider", "state", "updated_at"])
        # ``audit``'s first positional parameter is named ``action``: the provider action
        # goes in metadata as ``operation_action``.
        audit("hosting.operation_started", user=user, api_key=api_key, obj=session, operation_action=action,
              operation=str(op.pk))
        locked_occ.event = occ.event

    # Step B: the call, outside any transaction or lock.
    try:
        outcome = adapter.run(action, locked_occ, account.config or {}, secrets,
                              transport=transport or base.default_transport, external_id=external_id)
    except base.Unsupported:
        outcome = base.Outcome("failed", f"{action}: unsupported")
    except Exception as exc:  # defensive: the request may have been sent
        outcome = base.Outcome("unknown", f"{action}: internal_error: {type(exc).__name__}")

    # Step C: record the outcome.
    return _record(occ, session.pk, op.pk, action, outcome, previous_state, user=user, api_key=api_key)


def _owns_session(session, op):
    """True while ``op`` still owns ``session``: the op is in flight, the session is in
    flight, and no later operation exists. Once ``recover_stale_operations``, ``reconcile``
    or a newer operation has moved on, a late outcome must not rewrite session state."""
    return (
        op.status == ProvisioningOperation.Status.IN_FLIGHT
        and session.state == HostedSession.State.IN_FLIGHT
        and not session.operations.filter(created_at__gt=op.created_at).exclude(pk=op.pk).exists()
    )


def _record_late(session, op, action, outcome, now, *, user, api_key):
    """A fenced (late) outcome: note it on the op, never copy ids/links onto the session.

    A late success of a create/attach means a provider session may exist that this system
    thinks was never made (e.g. reconciled as ``not_created``). If nothing else owns the
    session and it sits in ``failed``, put it back in ``needs_reconciliation`` with this op
    as the unknown one, so a retry cannot silently create a duplicate."""
    op.status_code = outcome.status_code
    op.error = (f"{op.error} [late outcome: {outcome.kind}]".strip())[:300]
    if op.finished_at is None:
        op.finished_at = now
    flagged = False
    if (
        outcome.kind == "succeeded"
        and action in ("create", "attach")
        and session.state == HostedSession.State.FAILED
        and not session.operations.filter(status=ProvisioningOperation.Status.IN_FLIGHT).exclude(pk=op.pk).exists()
    ):
        op.status = ProvisioningOperation.Status.UNKNOWN
        session.state = HostedSession.State.NEEDS_RECONCILIATION
        session.last_error = "A late provider response reported success after this request was given up: check the provider and reconcile."
        session.save(update_fields=["state", "last_error", "updated_at"])
        flagged = True
    op.save(update_fields=["status", "status_code", "error", "finished_at"])
    audit("hosting.operation_late_outcome", user=user, api_key=api_key, obj=session, operation_action=action,
          operation=str(op.pk), outcome=outcome.kind, status_code=outcome.status_code, flagged=flagged)


def _record(occ, session_id, op_id, action, outcome, previous_state, *, user, api_key):
    now = timezone.now()
    with transaction.atomic():
        session = HostedSession.objects.select_for_update().get(pk=session_id)
        op = ProvisioningOperation.objects.select_for_update().get(pk=op_id)
        if not _owns_session(session, op):
            _record_late(session, op, action, outcome, now, user=user, api_key=api_key)
            session.occurrence = occ
            return serialize_session(session)
        op.status_code = outcome.status_code
        op.error = (outcome.error or "")[:300]
        op.finished_at = now
        session.last_error = (outcome.error or "")[:300]
        if outcome.kind == "succeeded":
            op.status = ProvisioningOperation.Status.SUCCEEDED
            session.state = HostedSession.State.LINKED
            if outcome.external_id:
                session.external_id = outcome.external_id[:255]
            if action == "create":
                session.mode = HostedSession.Mode.PROVISIONED
            elif action == "attach":
                session.mode = HostedSession.Mode.ATTACHED
            if outcome.host_url:
                session.host_url_encrypted = crypto.encrypt_secret(outcome.host_url)
            session.synced_schedule_generation = op.schedule_generation
            session.last_synced_at = now
            session.last_error = ""
            if outcome.join_url:
                Occurrence.objects.filter(pk=session.occurrence_id).update(join_url=outcome.join_url[:1000], updated_at=now)
                occ.join_url = outcome.join_url[:1000]
            if op.schedule_generation is not None:
                current = Occurrence.objects.filter(pk=session.occurrence_id).values_list("schedule_generation", flat=True).first()
                if current is not None and current != op.schedule_generation:
                    session.state = HostedSession.State.OUT_OF_DATE
        elif outcome.kind == "failed":
            op.status = ProvisioningOperation.Status.FAILED
            if action == "update" and previous_state in (HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE):
                session.state = HostedSession.State.OUT_OF_DATE
            elif previous_state in (HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE):
                session.state = previous_state
            else:
                session.state = HostedSession.State.FAILED
        else:
            op.status = ProvisioningOperation.Status.UNKNOWN
            if action == "update":
                # PATCH of the full schedule is idempotent: the link stays, retry is allowed.
                session.state = HostedSession.State.OUT_OF_DATE
            else:
                session.state = HostedSession.State.NEEDS_RECONCILIATION
        op.save()
        session.save()
        audit("hosting.operation_finished", user=user, api_key=api_key, obj=session, operation_action=action,
              operation=str(op.pk), outcome=outcome.kind, status_code=outcome.status_code)
    session.occurrence = occ
    return serialize_session(session)


# An operation still in flight after this long was lost (request timeout, worker kill,
# deploy, or an error while recording): every call is bounded by TOTAL_TIMEOUT_SECONDS
# per step (at most two steps) plus process start-up.
STALE_IN_FLIGHT_SECONDS = 5 * 60


def recover_stale_operations(now=None):
    """Turn lost in-flight operations into ``unknown`` so a person can reconcile them.

    A lost create/attach may have reached the provider, so the session goes to
    ``needs_reconciliation`` (never back to a retryable state). A lost update keeps the
    link and is marked out of date (re-sending a full-schedule PATCH is safe). Stale
    pending registrants (an attempt started, no outcome recorded) go to
    ``needs_reconciliation`` too. Returns the number of records changed."""
    from datetime import timedelta

    from webinars import attendance_import

    now = now or timezone.now()
    cutoff = now - timedelta(seconds=STALE_IN_FLIGHT_SECONDS)
    # Read-only attendance pulls are recovered by their own module (they become ``failed``,
    # never ``unknown``: there is nothing to reconcile) and are excluded below.
    changed = attendance_import.recover_stale_imports(now)
    for op_id in list(ProvisioningOperation.objects.filter(status=ProvisioningOperation.Status.IN_FLIGHT,
                                                           created_at__lt=cutoff)
                      .exclude(action=ProvisioningOperation.Action.ATTENDANCE).values_list("pk", flat=True)[:50]):
        with transaction.atomic():
            op = ProvisioningOperation.objects.select_for_update().filter(
                pk=op_id, status=ProvisioningOperation.Status.IN_FLIGHT).first()
            if op is None:
                continue
            session = HostedSession.objects.select_for_update().get(pk=op.session_id)
            op.status = ProvisioningOperation.Status.UNKNOWN
            op.error = (f"{op.action}: no outcome recorded (lost in flight)")[:300]
            op.finished_at = now
            op.save(update_fields=["status", "error", "finished_at"])
            if session.state == HostedSession.State.IN_FLIGHT:
                if op.action == ProvisioningOperation.Action.UPDATE and session.external_id:
                    session.state = HostedSession.State.OUT_OF_DATE
                else:
                    session.state = HostedSession.State.NEEDS_RECONCILIATION
                session.last_error = op.error
                session.save(update_fields=["state", "last_error", "updated_at"])
            audit("hosting.operation_lost", obj=session, operation_action=op.action, operation=str(op.pk))
            changed += 1
    # A lost attempt at a repeatable request is simply tried again.
    changed += ProviderRegistrant.objects.filter(
        state=ProviderRegistrant.State.PENDING, attempts__gt=0, updated_at__lt=cutoff,
        session__provider__in=_idempotent_providers(),
    ).update(state=ProviderRegistrant.State.FAILED, last_error="register: no outcome recorded", updated_at=now)
    changed += ProviderRegistrant.objects.filter(
        state=ProviderRegistrant.State.PENDING, attempts__gt=0, updated_at__lt=cutoff
    ).update(state=ProviderRegistrant.State.NEEDS_RECONCILIATION, last_error="register: no outcome recorded",
             updated_at=now)
    changed += flag_unbound_registrants(now)
    return changed


def flag_unbound_registrants(now=None):
    """Registrants whose binding is not the session's current account + event go to
    ``needs_reconciliation`` (their old link is wiped). Covers re-attach to another event or
    account and legacy rows with no binding. Never backfills the binding and never calls the
    provider, so a stale row cannot be auto re-sent. In-flight attempts are left to the
    attempt fence and lost-attempt recovery. Returns the number of rows changed."""
    from django.db.models import F, Q

    pks = list(ProviderRegistrant.objects.filter(
        session__provider__in=_per_registrant_providers(), session__mode__in=PER_REGISTRANT_MODES,
        session__state__in=(HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE),
        session__account__isnull=False,
    ).exclude(session__external_id="").filter(
        Q(state__in=(ProviderRegistrant.State.LINKED, ProviderRegistrant.State.FAILED))
        | Q(state=ProviderRegistrant.State.PENDING, attempts=0)
    ).filter(
        Q(event_external_id="") | Q(event_account__isnull=True)
        | ~Q(event_external_id=F("session__external_id")) | ~Q(event_account_id=F("session__account_id"))
    ).values_list("pk", "session_id")[:500])
    changed = 0
    for pk, session_id in pks:
        with transaction.atomic():
            # Same lock order as ensure_registrant / reconcile_registrant: session, then row.
            session = HostedSession.objects.select_for_update().filter(pk=session_id).first()
            pr = ProviderRegistrant.objects.select_for_update().filter(pk=pk, session_id=session_id).first()
            if session is None or pr is None or current_binding(session) is None or registrant_bound(pr, session):
                continue
            if pr.state not in (ProviderRegistrant.State.LINKED, ProviderRegistrant.State.FAILED) and not (
                    pr.state == ProviderRegistrant.State.PENDING and pr.attempts == 0):
                continue
            if adapter_for(session).register_idempotent:
                # Nothing to check by hand: bind it to the current event and let the sweep
                # ask for the link again.
                pr.state = ProviderRegistrant.State.FAILED
                pr.attempts = 0
                pr.event_account_id, pr.event_external_id = current_binding(session)
                pr.join_url_encrypted = ""
                pr.last_error = "Bound to a different provider event or account: asking for a new link."
                pr.save(update_fields=["state", "attempts", "event_account", "event_external_id", "join_url_encrypted",
                                       "last_error", "updated_at"])
            else:
                pr.state = ProviderRegistrant.State.NEEDS_RECONCILIATION
                pr.join_url_encrypted = ""
                pr.last_error = "Bound to a different provider event or account (or unbound): check Riverside and reconcile."
                pr.save(update_fields=["state", "join_url_encrypted", "last_error", "updated_at"])
            audit("hosting.registrant_unbound", obj=pr.registration, session=str(session.pk))
            changed += 1
    return changed


def reconcile(occ, decision, *, external_id="", user=None, api_key=None):
    """A person records what happened to an unknown-outcome request."""
    recover_stale_operations()
    with transaction.atomic():
        session = HostedSession.objects.select_for_update().filter(occurrence_id=occ.pk).first()
        if session is None or session.state != HostedSession.State.NEEDS_RECONCILIATION:
            raise ValidationError({"decision": ["Nothing to reconcile for this session."]})
        op = session.operations.filter(status=ProvisioningOperation.Status.UNKNOWN).order_by("-created_at").first()
        if decision == "created":
            adapter = adapter_for(session)
            if not _valid_external_id(adapter, external_id or ""):
                raise ValidationError({"externalId": ["Enter the provider session ID you found."]})
            session.external_id = external_id
            session.state = HostedSession.State.LINKED
            if op is not None and op.action == "attach":
                session.mode = HostedSession.Mode.ATTACHED
            else:
                session.mode = HostedSession.Mode.PROVISIONED
            session.synced_schedule_generation = op.schedule_generation if op is not None else None
            session.last_synced_at = timezone.now()
            session.last_error = "Reconciled by hand: add the attendee join link in session details if it is missing."
            if op is not None:
                op.status = ProvisioningOperation.Status.SUCCEEDED
        elif decision == "not_created":
            session.state = HostedSession.State.FAILED
            session.last_error = "Reconciled by hand: nothing was created; safe to retry."
            if op is not None:
                op.status = ProvisioningOperation.Status.FAILED
        else:
            raise ValidationError({"decision": ["Choose 'created' or 'not_created'."]})
        if op is not None:
            op.error = (op.error + " [reconciled]")[:300]
            op.save(update_fields=["status", "error"])
        session.save()
        audit("hosting.reconciled", user=user, api_key=api_key, obj=session, decision=decision)
    session.occurrence = occ
    return serialize_session(session)


def set_manual_link(occ, account, *, join_url, embed_url="", user=None, api_key=None):
    """Manual providers (StreamYard): store pasted links. No outbound call of any kind."""
    adapter = adapter_for(account)
    if not adapter.capabilities.manual_link:
        raise ValidationError({"provider": [f"{adapter.label} does not use manual links."]})
    if not account.enabled:
        raise ValidationError({"accountId": ["This hosting account is disabled."]})
    errors = {}
    try:
        join_url = adapter.clean_link(join_url, "Attendee link")
    except ValidationError as exc:
        errors["joinUrl"] = exc.messages
    try:
        embed_url = adapter.clean_link(embed_url, "Embed URL")
    except ValidationError as exc:
        errors["embedUrl"] = exc.messages
    if not join_url and "joinUrl" not in errors:
        errors["joinUrl"] = ["Paste the attendee link."]
    if errors:
        raise ValidationError(errors)
    now = timezone.now()
    with transaction.atomic():
        Occurrence.objects.select_for_update().filter(pk=occ.pk).update(join_url=join_url, updated_at=now)
        session = get_or_create_session(occ, account)
        session = HostedSession.objects.select_for_update().get(pk=session.pk)
        session.account = account
        session.provider = account.provider
        session.mode = HostedSession.Mode.MANUAL
        session.state = HostedSession.State.LINKED
        session.embed_url = embed_url
        session.synced_schedule_generation = Occurrence.objects.values_list("schedule_generation", flat=True).get(pk=occ.pk)
        session.last_synced_at = now
        session.last_error = ""
        session.save()
        audit("hosting.manual_link_set", user=user, api_key=api_key, obj=session, embed=bool(embed_url))
    occ.join_url = join_url
    session.occurrence = occ
    return serialize_session(session)


def detach(occ, *, user=None, api_key=None):
    """Forget the link locally. The external session is left untouched (never deleted).

    ``Occurrence.join_url`` is kept (it may have been pasted by hand); clear or replace it
    in session details if the provider link must stop working for attendees."""
    recover_stale_operations()
    with transaction.atomic():
        session = HostedSession.objects.select_for_update().filter(occurrence_id=occ.pk).first()
        if session is None:
            raise ValidationError({"session": ["No provider session is linked."]})
        if session.state == HostedSession.State.IN_FLIGHT:
            raise ValidationError({"session": ["A request is in flight; wait for it to finish."]})
        session.state = HostedSession.State.DETACHED
        session.save(update_fields=["state", "updated_at"])
        audit("hosting.detached", user=user, api_key=api_key, obj=session)
    session.occurrence = occ
    return serialize_session(session)


def mark_out_of_date(occ):
    """Called after a reschedule: a linked session whose schedule we last sent is stale."""
    session = session_for(occ)
    if session is None or session.state != HostedSession.State.LINKED:
        return False
    if session.synced_schedule_generation == occ.schedule_generation:
        return False
    # A session run again has new engagement to bring back.
    updated = HostedSession.objects.filter(pk=session.pk, state=HostedSession.State.LINKED).update(
        state=HostedSession.State.OUT_OF_DATE, updated_at=timezone.now(), engagement_synced_at=None, engagement_ended_at=None,
        engagement_next_at=None, engagement_attempts=0,
    )
    if updated:
        audit("hosting.out_of_date", obj=session, schedule_generation=occ.schedule_generation)
    return bool(updated)


def note_cancelled(occ):
    """Cancelling never deletes or cancels the provider session; record a note only."""
    session = session_for(occ)
    if session is None or session.state == HostedSession.State.DETACHED:
        return False
    HostedSession.objects.filter(pk=session.pk).update(
        last_error="Session cancelled here; cancel it in the provider yourself if needed.", updated_at=timezone.now()
    )
    audit("hosting.occurrence_cancelled_note", obj=session)
    return True


# ---------------------------------------------------------------------------
# Attendee links and per-registrant provider records (Riverside)
# ---------------------------------------------------------------------------


PER_REGISTRANT_MODES = (HostedSession.Mode.ATTACHED, HostedSession.Mode.PROVISIONED)


def _per_registrant_providers():
    """Providers that give each registrant their own link (Riverside, LiveKit)."""
    return [provider for provider, adapter in ADAPTERS.items() if adapter.capabilities.register_attendees]


def _idempotent_providers():
    return [provider for provider, adapter in ADAPTERS.items() if adapter.register_idempotent]


def _per_registrant_links(session):
    """Linked sessions of these providers hand out per-registrant links only."""
    return (
        session is not None
        and session.provider in _per_registrant_providers()
        and session.mode in PER_REGISTRANT_MODES
        and session.state in (HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE)
    )


def current_binding(session):
    """``(account_id, external_id)`` personal links must be bound to, or None when the
    session does not hand out per-registrant links (detached, other provider, no ID)."""
    if not _per_registrant_links(session) or not session.account_id or not session.external_id:
        return None
    return (session.account_id, session.external_id)


def registrant_bound(pr, session):
    """True only when ``pr`` was made for ``session``'s current account + provider event.
    A blank (legacy) binding never matches."""
    binding = current_binding(session)
    return bool(
        binding is not None
        and pr.session_id == session.pk
        and pr.event_external_id
        and pr.event_account_id is not None
        and (pr.event_account_id, pr.event_external_id) == binding
    )


def has_join_destination(occ):
    """Whether admitted registrants have somewhere to go, without reading any secret.

    * Linked sessions with personal links: the hosting account is enabled. Each registrant's own
      link is checked at admission; the shared ``join_url`` neither opens nor closes
      the doors.
    * Everything else: the occurrence join link is set.
    Lookup errors return False (fail closed)."""
    try:
        session = session_for(occ)
    except Exception as exc:
        logger.warning("join destination lookup failed: %s", type(exc).__name__)
        return False
    if _per_registrant_links(session):
        return session.account is not None and session.account.enabled
    return bool(occ.join_url)


def attendee_join_url(occ, registration):
    """Private link for one admitted registrant. Never raises.

    * Linked sessions with personal links: that registrant's own link, or None (not admitted yet:
      the shared ``join_url`` is never a fallback, so links are not shareable and paid
      seats cannot be passed on). None too when the account is disabled, the
      registration belongs to another occurrence or is not active, or the stored link
      is not an https link on the provider's hosts. Lookup errors also return None (fail closed).
    * Everything else (including detached sessions): the occurrence join link."""
    try:
        session = session_for(occ)
    except Exception as exc:
        logger.warning("attendee link lookup failed: %s", type(exc).__name__)
        return None
    if not _per_registrant_links(session):
        return occ.join_url or None
    if (
        registration is None
        or registration.occurrence_id != occ.pk
        or registration.status != Registration.Status.REGISTERED
        or session.account is None
        or not session.account.enabled
    ):
        return None
    try:
        pr = ProviderRegistrant.objects.filter(registration_id=registration.pk, session=session,
                                               state=ProviderRegistrant.State.LINKED).first()
        # The link must belong to the event the session is attached to now: after a
        # detach and re-attach to another event (same session row) the old link is refused.
        if pr is not None and pr.join_url_encrypted and registrant_bound(pr, session):
            link = crypto.decrypt_secret(pr.join_url_encrypted)
            return base.https_link(link, adapter_for(session).join_link_hosts(session.account.config or {})) or None
    except Exception as exc:  # decryption or DB error: fail closed
        logger.warning("attendee link lookup failed: %s", type(exc).__name__)
    return None


def prepare_attendee_link(occ, registration, transport=None):
    """Ask now for a personal link the worker hasn't got yet, so nobody waits at the door for a
    background job. Only where asking is safe to repeat (LiveKit). Makes a network call, so
    call it outside any transaction. Attempts are spaced out: during an outage a refreshing
    attendee doesn't start a new request every time."""
    session = session_for(occ)
    if not _per_registrant_links(session) or not adapter_for(session).register_idempotent:
        return
    if registration.occurrence_id != occ.pk:
        return
    pr = ProviderRegistrant.objects.filter(registration_id=registration.pk).first()
    if pr is not None:
        if pr.state == ProviderRegistrant.State.LINKED and registrant_bound(pr, session):
            return
        if pr.updated_at > timezone.now() - timedelta(seconds=DOOR_RETRY_SECONDS):
            return
    ensure_registrant(registration, transport=transport, at_door=True)


def _registrant_session(registration):
    session = HostedSession.objects.select_related("account").filter(occurrence_id=registration.occurrence_id).first()
    if (
        session is None
        or not _per_registrant_links(session)
        or session.account is None
        or not session.account.enabled
        or not session.external_id
    ):
        return None
    return session


def ensure_registrant(registration, transport=None, at_door=False):
    """Create this registrant's provider link once. Returns the ProviderRegistrant state or None.

    ``at_door``: the attendee is waiting, and the request is safe to repeat, so the worker's
    attempt limit doesn't lock them out after an outage."""
    registration = Registration.objects.select_related("person").get(pk=registration.pk)
    if registration.status != Registration.Status.REGISTERED or not registration.person.email:
        return None
    session = _registrant_session(registration)
    if session is None:
        return None
    adapter = adapter_for(session)
    with transaction.atomic():
        # Lock order: session, then registrant (same as reconcile_registrant and
        # flag_unbound_registrants). The binding is captured here, before any provider call.
        session = HostedSession.objects.select_for_update().filter(pk=session.pk).first()
        binding = current_binding(session) if session is not None else None
        if binding is None or session.account is None or not session.account.enabled:
            return None
        pr = ProviderRegistrant.objects.select_for_update().filter(registration=registration).first()
        if pr is None:
            try:
                with transaction.atomic():
                    pr = ProviderRegistrant.objects.create(registration=registration, session=session,
                                                           event_account_id=binding[0], event_external_id=binding[1])
            except IntegrityError:
                return None  # another worker created it first and owns this attempt
        elif pr.state == ProviderRegistrant.State.PENDING and pr.attempts > 0:
            # A previous attempt started and never recorded an outcome: do not re-send.
            return pr.state
        elif not registrant_bound(pr, session) and adapter.register_idempotent:
            # Nothing for a person to check: ask for a link on the current event.
            pr.session = session
            pr.event_account_id, pr.event_external_id = binding
            pr.join_url_encrypted = ""
            pr.attempts = 0
        elif not registrant_bound(pr, session):
            # Made for another event/account, or a legacy row with no binding: never
            # re-send automatically (a registrant may already exist there) and never
            # backfill. A person reconciles it against the current event.
            if pr.state != ProviderRegistrant.State.NEEDS_RECONCILIATION:
                pr.state = ProviderRegistrant.State.NEEDS_RECONCILIATION
                pr.join_url_encrypted = ""
                pr.last_error = "Bound to a different provider event or account (or unbound): check Riverside and reconcile."
                pr.save(update_fields=["state", "join_url_encrypted", "last_error", "updated_at"])
                audit("hosting.registrant_unbound", obj=registration, session=str(session.pk))
            return pr.state
        elif pr.state in (ProviderRegistrant.State.LINKED, ProviderRegistrant.State.NEEDS_RECONCILIATION):
            return pr.state
        elif pr.attempts >= MAX_REGISTRANT_ATTEMPTS and not (at_door and adapter.register_idempotent):
            return pr.state
        pr.state = ProviderRegistrant.State.PENDING
        pr.attempts += 1
        pr.save(update_fields=["state", "attempts", "session", "event_account", "event_external_id",
                               "join_url_encrypted", "updated_at"])
        attempt = pr.attempts
        account = session.account
    try:
        secrets = load_secrets(account)
        outcome = adapter.run("register", registration.occurrence, account.config or {}, secrets,
                              transport=transport or base.default_transport, external_id=binding[1],
                              registration=registration)
    except (crypto.DecryptionError, ValueError, KeyError):
        outcome = base.Outcome("failed", "register: credentials_unavailable")
    except Exception as exc:
        outcome = base.Outcome("unknown", f"register: internal_error: {type(exc).__name__}")
    with transaction.atomic():
        session = HostedSession.objects.select_for_update().get(pk=session.pk)
        pr = ProviderRegistrant.objects.select_for_update().get(pk=pr.pk)
        if pr.state != ProviderRegistrant.State.PENDING or pr.attempts != attempt:
            # Fenced: recovered as lost, reconciled by a person, or a newer attempt owns it.
            pr.last_error = (f"{pr.last_error} [late outcome: {outcome.kind}]".strip())[:300]
            pr.save(update_fields=["last_error", "updated_at"])
            audit("hosting.registrant_late_outcome", obj=registration, outcome=outcome.kind,
                  status_code=outcome.status_code)
            return pr.state
        if (pr.event_account_id, pr.event_external_id) != binding or current_binding(session) != binding:
            # The session was detached / re-attached to another event or account while this
            # call was out: the outcome is about the old event. Keep nothing from it (no link)
            # and leave the row for a person (or, if asking again is safe, for the next
            # attempt, which binds it to the new event); the attempt count is not reset.
            pr.state = ProviderRegistrant.State.FAILED if adapter.register_idempotent else ProviderRegistrant.State.NEEDS_RECONCILIATION
            pr.join_url_encrypted = ""
            pr.last_error = (f"The provider event changed during this attempt [late outcome: {outcome.kind}]: "
                             "check Riverside and reconcile.")[:300]
            pr.save(update_fields=["state", "join_url_encrypted", "last_error", "updated_at"])
            audit("hosting.registrant_late_outcome", obj=registration, outcome=outcome.kind,
                  status_code=outcome.status_code, binding_changed=True)
            return pr.state
        pr.last_error = (outcome.error or "")[:300]
        if outcome.kind == "succeeded":
            pr.state = ProviderRegistrant.State.LINKED
            pr.external_id = (outcome.external_id or "")[:255]
            pr.join_url_encrypted = crypto.encrypt_secret(outcome.join_url)
        elif outcome.kind == "failed":
            pr.state = ProviderRegistrant.State.FAILED
        elif adapter.register_idempotent:
            pr.state = ProviderRegistrant.State.FAILED  # safe to ask again
        else:
            pr.state = ProviderRegistrant.State.NEEDS_RECONCILIATION
        pr.save()
        audit("hosting.registrant", obj=registration, outcome=outcome.kind, status_code=outcome.status_code)
    return pr.state


def reconcile_registrant(registration, decision, *, join_url="", external_id="", user=None, api_key=None,
                         expected_attempts=None, expected_binding=None):
    """A person records what happened to an unknown-outcome registrant request.

    Both decisions bind the row to the session's *current* account + provider event
    (under the session lock); the attempt count is never reset, so a late outcome of an
    earlier attempt (for any event) stays fenced.

    * ``not_created``: back to ``failed`` so the worker sweep may retry it against the
      current event (still bounded by ``MAX_REGISTRANT_ATTEMPTS``; at the cap it stays failed).
    * ``created``: the person found the registrant in the current Riverside event and
      pastes that registrant's personal join link (https, Riverside hosts only); it is
      stored encrypted.

    ``expected_attempts`` and ``expected_binding`` (routes pass what their confirmation was
    issued for) are checked under the locks, so neither a newer attempt nor a re-attach to
    another event is resolved by an old confirmation. Refused when the session is not
    linked to a Riverside event. Called by the REST, MCP and studio registrant routes
    (webinars/api/hosting.py)."""
    recover_stale_operations()
    with transaction.atomic():
        session = HostedSession.objects.select_for_update().filter(occurrence_id=registration.occurrence_id).first()
        pr = ProviderRegistrant.objects.select_for_update().filter(registration_id=registration.pk).first()
        if pr is None or session is None or pr.session_id != session.pk or pr.state != ProviderRegistrant.State.NEEDS_RECONCILIATION:
            raise ValidationError({"decision": ["Nothing to reconcile for this registrant."]})
        if expected_attempts is not None and pr.attempts != expected_attempts:
            raise ValidationError({"decision": ["The registrant changed since the preview; preview again."]})
        binding = current_binding(session)
        if binding is None:
            raise ValidationError({"decision": ["The session is not linked to a Riverside event; attach it first."]})
        if expected_binding is not None and tuple(expected_binding) != binding:
            raise ValidationError({"decision": ["The provider event changed since the preview; preview again."]})
        pr.session = session
        pr.event_account_id, pr.event_external_id = binding
        if decision == "created":
            adapter = adapter_for(session)
            link = base.https_link((join_url or "").strip(), getattr(adapter, "link_hosts", ()))
            if not link:
                raise ValidationError({"joinUrl": ["Paste this registrant's Riverside join link (https)."]})
            if external_id and not _valid_external_id(adapter, external_id):
                raise ValidationError({"externalId": ["Enter a valid provider registrant ID."]})
            pr.state = ProviderRegistrant.State.LINKED
            pr.external_id = (external_id or "")[:255]
            pr.join_url_encrypted = crypto.encrypt_secret(link)
            pr.last_error = "Reconciled by hand."
        elif decision == "not_created":
            pr.state = ProviderRegistrant.State.FAILED
            pr.join_url_encrypted = ""
            pr.last_error = "Reconciled by hand: nothing was created; the sweep may retry."
        else:
            raise ValidationError({"decision": ["Choose 'created' or 'not_created'."]})
        pr.save()
        audit("hosting.registrant_reconciled", user=user, api_key=api_key, obj=registration, decision=decision,
              session=str(session.pk), attempts=pr.attempts)
    return pr.state


def sweep_registrants(limit=20, transport=None):
    """Worker hook: create missing per-registrant links. Returns the number attempted."""
    from django.db.models import Q

    recover_stale_operations()
    sessions = HostedSession.objects.filter(
        provider__in=_per_registrant_providers(),
        mode__in=PER_REGISTRANT_MODES,
        state__in=(HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE),
        account__enabled=True,
    ).values_list("occurrence_id", flat=True)
    candidates = (
        Registration.objects.filter(occurrence_id__in=list(sessions), status=Registration.Status.REGISTERED,
                                    occurrence__status=Occurrence.Status.SCHEDULED)
        .filter(Q(provider_registrant__isnull=True)
                | Q(provider_registrant__state=ProviderRegistrant.State.FAILED,
                    provider_registrant__attempts__lt=MAX_REGISTRANT_ATTEMPTS))
        .order_by("created_at")[:limit]
    )
    count = 0
    for registration in candidates:
        try:
            ensure_registrant(registration, transport=transport)
        except Exception as exc:  # one registrant must never stop the sweep
            logger.warning("registrant sweep error: %s", type(exc).__name__)
        count += 1
    return count


# ---------------------------------------------------------------------------
# The host's room and recordings (providers with host_room / recordings)
# ---------------------------------------------------------------------------


def _crew_session(occ):
    session = session_for(occ)
    adapter = adapter_for(session) if session is not None else None
    if (
        adapter is None
        or not adapter.capabilities.host_room
        or session.state not in (HostedSession.State.LINKED, HostedSession.State.OUT_OF_DATE)
        or session.account is None
        or not session.account.enabled
        or not session.external_id
    ):
        raise ValidationError({"action": ["This session has no room yet: create it first."]})
    if occ.status == Occurrence.Status.CANCELLED:
        raise ValidationError({"action": ["The session is cancelled."]})
    try:
        return session, adapter, load_secrets(session.account)
    except (crypto.DecryptionError, ValueError):
        raise ValidationError({"credentials": ["Stored credentials could not be decrypted; set them again."]})


def _crew_outcome(outcome):
    if outcome.kind != "succeeded":
        raise ValidationError({"action": [f"Your live server didn't answer ({outcome.error})."]})
    return outcome


def _display_name(user):
    return (user.get_full_name() or user.get_username() or "Host")[:80]


def host_room_link(occ, *, user, transport=None):
    """This person's own host link. Everyone who opens the room from the studio is themselves in
    it, on the record. Audited."""
    session, adapter, secrets = _crew_session(occ)
    outcome = _crew_outcome(adapter.crew_add(session.account.config or {}, secrets, session.external_id, role="host",
                                             name=_display_name(user), email=user.email or "", key=f"studio-user-{user.pk}",
                                             transport=transport or base.default_transport))
    audit("hosting.host_link_used", user=user, obj=session)
    return outcome.join_url


def crew_list(occ, *, transport=None):
    session, adapter, secrets = _crew_session(occ)
    return _crew_outcome(adapter.crew_list(session.account.config or {}, secrets, session.external_id,
                                           transport=transport or base.default_transport)).data["crew"]


def crew_add(occ, *, role, name, title="", user=None, api_key=None, transport=None):
    """Adds someone to the crew and returns their own link. Raises ``ValidationError``."""
    if role not in ("host", "producer", "guest"):
        raise ValidationError({"role": ["Choose host, producer or guest."]})
    name = (name or "").strip()
    if not name:
        raise ValidationError({"name": ["Give their name."]})
    session, adapter, secrets = _crew_session(occ)
    outcome = _crew_outcome(adapter.crew_add(session.account.config or {}, secrets, session.external_id, role=role, name=name[:80],
                                             title=(title or "").strip()[:80], transport=transport or base.default_transport))
    audit("hosting.crew_added", user=user, api_key=api_key, obj=session, role=role, member=outcome.external_id)
    return {"id": outcome.external_id, "url": outcome.join_url}


def crew_new_link(occ, member_id, *, user=None, api_key=None, transport=None):
    session, adapter, secrets = _crew_session(occ)
    if not adapter.valid_external_id(member_id):
        raise ValidationError({"member": ["Unknown crew member."]})
    outcome = _crew_outcome(adapter.crew_new_link(session.account.config or {}, secrets, session.external_id, member_id,
                                                  transport=transport or base.default_transport))
    audit("hosting.crew_new_link", user=user, api_key=api_key, obj=session, member=member_id)
    return {"id": member_id, "url": outcome.join_url}


def crew_remove(occ, member_id, *, user=None, api_key=None, transport=None):
    session, adapter, secrets = _crew_session(occ)
    if not adapter.valid_external_id(member_id):
        raise ValidationError({"member": ["Unknown crew member."]})
    _crew_outcome(adapter.crew_remove(session.account.config or {}, secrets, session.external_id, member_id,
                                      transport=transport or base.default_transport))
    audit("hosting.crew_removed", user=user, api_key=api_key, obj=session, member=member_id)


def _live_session(occ):
    session, adapter, secrets = _crew_session(occ)
    if not adapter.capabilities.live_settings:
        raise ValidationError({"action": ["This provider has no settings of its own."]})
    return session, adapter, secrets


def live_settings(occ, *, transport=None):
    session, adapter, secrets = _live_session(occ)
    return _crew_outcome(adapter.live_api(session.account.config or {}, secrets, "settings", "GET", f"/rooms/{session.external_id}/settings",
                                          transport=transport or base.default_transport)).data


def update_live_settings(occ, changes, *, user=None, api_key=None, transport=None):
    """``changes`` may hold ``settings``, ``theme`` and ``survey``. The service checks each."""
    session, adapter, secrets = _live_session(occ)
    outcome = _crew_outcome(adapter.live_api(session.account.config or {}, secrets, "settings", "PATCH", f"/rooms/{session.external_id}/settings", changes,
                                             transport=transport or base.default_transport))
    audit("hosting.live_settings_changed", user=user, api_key=api_key, obj=session, fields=sorted(changes))
    return outcome.data


def live_destinations(occ, *, transport=None):
    session, adapter, secrets = _live_session(occ)
    return _crew_outcome(adapter.live_api(session.account.config or {}, secrets, "destinations", "GET", f"/rooms/{session.external_id}/destinations",
                                          transport=transport or base.default_transport)).data


def update_live_destinations(occ, destinations, *, user=None, api_key=None, transport=None):
    """Stream keys are set here and never read back. Audited without them."""
    session, adapter, secrets = _live_session(occ)
    outcome = _crew_outcome(adapter.live_api(session.account.config or {}, secrets, "destinations", "PUT", f"/rooms/{session.external_id}/destinations",
                                             {"destinations": destinations}, transport=transport or base.default_transport))
    audit("hosting.live_destinations_changed", user=user, api_key=api_key, obj=session, platforms=sorted(str(d.get("platform")) for d in destinations))
    return outcome.data


def _account_live(account):
    adapter = adapter_for(account)
    if not adapter.capabilities.live_settings:
        raise ValidationError({"action": ["This provider has no settings of its own."]})
    try:
        secrets = load_secrets(account)
    except (crypto.DecryptionError, ValueError):
        raise ValidationError({"credentials": ["Stored credentials could not be decrypted; set them again."]})
    if adapter.validate_credentials(secrets):
        raise ValidationError({"credentials": ["Set the service secret first."]})
    return adapter, secrets


def live_brand(account, *, transport=None):
    adapter, secrets = _account_live(account)
    return _crew_outcome(adapter.live_api(account.config or {}, secrets, "brand", "GET", "/brand", transport=transport or base.default_transport)).data


def update_live_brand(account, theme, *, user=None, api_key=None, transport=None):
    adapter, secrets = _account_live(account)
    # The service replaces the whole theme, and a caller may send only the colour.
    current = _crew_outcome(adapter.live_api(account.config or {}, secrets, "brand", "GET", "/brand", transport=transport or base.default_transport)).data
    merged = {k: theme.get(k, current.get(k) or "") for k in ("accent", "stage", "mode", "font")}
    outcome = _crew_outcome(adapter.live_api(account.config or {}, secrets, "brand", "PUT", "/brand", merged, transport=transport or base.default_transport))
    audit("hosting.live_brand_changed", user=user, api_key=api_key, obj=account)
    return outcome.data


def fetch_recordings(occ, *, transport=None):
    """Read-only: what the provider recorded for this session. Raises ``ValidationError``."""
    session = session_for(occ)
    adapter = adapter_for(session) if session is not None else None
    if adapter is None or not adapter.capabilities.recordings or not session.external_id or session.account is None:
        raise ValidationError({"action": ["This session has no provider recordings to check."]})
    try:
        secrets = load_secrets(session.account)
    except (crypto.DecryptionError, ValueError):
        raise ValidationError({"credentials": ["Stored credentials could not be decrypted; set them again."]})
    if adapter.validate_credentials(secrets):
        raise ValidationError({"credentials": ["Some credentials are not set."]})
    outcome = adapter.fetch_recordings(session.account.config or {}, secrets, session.external_id,
                                       transport=transport or base.default_transport)
    if outcome.kind != "succeeded":
        raise ValidationError({"action": [f"The recordings could not be fetched ({outcome.error})."]})
    return outcome.data["recordings"]


def live_settings_link(account, *, user, room="", transport=None):
    """A one-time link that signs this Administrator in to the live server's own settings, as
    themselves, where brand and stream keys are kept. Raises ``ValidationError``."""
    if not user.email:
        raise ValidationError({"action": ["Your account has no email address, which the live server signs you in by. Add one first."]})
    adapter = adapter_for(account)
    if not adapter.capabilities.live_settings:
        raise ValidationError({"action": ["This provider has no settings of its own."]})
    try:
        secrets = load_secrets(account)
    except (crypto.DecryptionError, ValueError):
        raise ValidationError({"credentials": ["Stored credentials could not be decrypted; set them again."]})
    if adapter.validate_credentials(secrets):
        raise ValidationError({"credentials": ["Set the service secret first."]})
    outcome = adapter.fetch_settings_link(account.config or {}, secrets, room, email=user.email, name=_display_name(user),
                                          transport=transport or base.default_transport)
    if outcome.kind != "succeeded":
        raise ValidationError({"action": [f"Your live server didn't answer ({outcome.error})."]})
    audit("hosting.live_settings_opened", user=user, obj=account)
    return outcome.data["url"]
