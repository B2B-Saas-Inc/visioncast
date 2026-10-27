"""Studio pages for hosting providers (docs/hosting.md).

* Hosting accounts (credentials): Administrators and superusers only.
* Session hosting panel: Editors (``webinars.change_occurrence``) may look and preview;
  confirming a create/update/attach, reconciling or saving manual links needs an
  Administrator or superuser, because it acts on a real provider account.
* Analysts never reach these pages. Secrets, join links, host start links and embed
  URLs are never rendered: only "Set" / "Not set" flags.
"""
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect

from webinars.hosting import ADAPTERS, get_adapter, service
from webinars.models import HostingAccount, Occurrence
from webinars.roles import ADMINISTRATOR

from .access import forbidden, render, studio_view

T = "studio/hosting/"


def is_hosting_admin(user):
    return bool(user.is_superuser or user.groups.filter(name=ADMINISTRATOR).exists())


def _admin_only(request):
    if not is_hosting_admin(request.user):
        return forbidden(request, "Only Administrators connect hosting providers.")
    return None


def _errors(exc):
    if hasattr(exc, "error_dict"):
        return {k: [m for e in v for m in e.messages] for k, v in exc.error_dict.items()}
    return {"__all__": exc.messages}


@studio_view(methods=("GET",))
def hosting_accounts(request):
    denied = _admin_only(request)
    if denied:
        return denied
    accounts = [service.serialize_account(a) for a in HostingAccount.objects.order_by("name")]
    return render(request, T + "accounts.html", {
        "accounts": accounts,
        "providers": [a.describe() for a in ADAPTERS.values()],
    })


def _account_form(request, account, provider, *, errors=None, state=None, warnings=None, status=200):
    adapter = get_adapter(provider)
    serialized = service.serialize_account(account) if account is not None else None
    state = state or {
        "name": account.name if account else "",
        "enabled": account.enabled if account else False,
        "config": dict(account.config or {}) if account else {},
    }
    config_fields = [{"name": f.name, "label": f.label, "help": f.help, "required": f.required,
                      "value": state["config"].get(f.name, "")} for f in adapter.config_fields]
    secret_fields = [{"name": f.name, "label": f.label, "help": f.help,
                      "is_set": bool(serialized and serialized["secretsSet"].get(f.name))} for f in adapter.credential_fields]
    return render(request, T + "account_form.html", {
        "account": account, "provider": provider, "adapter": adapter.describe(), "state": state,
        "config_fields": config_fields, "secret_fields": secret_fields, "errors": errors or {},
        "warnings": warnings if warnings is not None else (serialized["warnings"] if serialized else adapter.allowlist_problems()),
    }, status=status)


def _save_from_post(request, account, provider):
    adapter = get_adapter(provider)
    config = {f.name: request.POST.get(f"config-{f.name}", "") for f in adapter.config_fields}
    secrets = {f.name: request.POST.get(f"secret-{f.name}", "") for f in adapter.credential_fields}
    state = {"name": request.POST.get("name", ""), "enabled": request.POST.get("enabled") == "on", "config": config}
    try:
        account, warnings = service.save_account(
            account, provider=provider, name=state["name"], config=config, posted_secrets=secrets,
            enabled=state["enabled"], user=request.user,
        )
    except ValidationError as exc:
        return _account_form(request, account, provider, errors=_errors(exc), state=state, status=400)
    return redirect("studio:hosting-account-edit", pk=account.pk)


@studio_view()
def hosting_account_new(request):
    denied = _admin_only(request)
    if denied:
        return denied
    provider = request.POST.get("provider") or request.GET.get("provider") or "zoom"
    if provider not in ADAPTERS:
        provider = "zoom"
    if request.method == "POST" and request.POST.get("step") == "save":
        return _save_from_post(request, None, provider)
    return _account_form(request, None, provider)


@studio_view()
def hosting_account_edit(request, pk):
    denied = _admin_only(request)
    if denied:
        return denied
    account = get_object_or_404(HostingAccount, pk=pk)
    if request.method == "POST":
        return _save_from_post(request, account, account.provider)
    return _account_form(request, account, account.provider)


@studio_view(perm=("webinars.view_occurrence", "webinars.change_occurrence"))
def session_hosting(request, pk):
    occ = get_object_or_404(Occurrence.objects.select_related("event"), pk=pk)
    admin = is_hosting_admin(request.user)
    accounts = list(HostingAccount.objects.filter(enabled=True).order_by("name"))
    session = service.session_for(occ)
    ctx = {"occ": occ, "event": occ.event, "accounts": accounts, "can_execute": admin, "errors": {}, "preview": None,
           "notice": "", "selected_account": None, "external_id": "", "action": ""}
    if session is not None and session.account_id:
        ctx["selected_account"] = session.account
    if request.method == "POST":
        op = request.POST.get("op", "")
        account = None
        if request.POST.get("account"):
            account = HostingAccount.objects.filter(pk=request.POST["account"]).first() if _is_uuid(request.POST["account"]) else None
        ctx["selected_account"] = account or ctx["selected_account"]
        action = request.POST.get("action", "")
        external_id = request.POST.get("external_id", "").strip()
        ctx.update(action=action, external_id=external_id)
        try:
            if op in ("preview", "confirm"):
                if account is None:
                    raise ValidationError({"account": ["Choose a hosting account."]})
                if action not in service.ACTIONS:
                    raise ValidationError({"action": ["Choose an action."]})
                if op == "preview":
                    ctx["preview"] = service.preview(occ, account, action, external_id)
                    if not ctx["preview"]["problems"]:
                        ctx["confirm_token"] = _confirm_token(request, occ, account, action, external_id)
                else:
                    if not admin:
                        return forbidden(request, "Only Administrators can create, update or attach provider sessions.")
                    _check_confirm_token(request, occ, account, action, external_id)
                    service.execute(occ, account, action, external_id=external_id, user=request.user)
                    return redirect("studio:session-hosting", pk=occ.pk)
            elif op in ("reconcile", "manual", "detach"):
                if not admin:
                    return forbidden(request, "Only Administrators can change provider links.")
                if op == "reconcile":
                    service.reconcile(occ, request.POST.get("decision", ""), external_id=external_id, user=request.user)
                elif op == "manual":
                    if account is None:
                        raise ValidationError({"account": ["Choose a hosting account."]})
                    service.set_manual_link(occ, account, join_url=request.POST.get("join_url", ""),
                                            embed_url=request.POST.get("embed_url", ""), user=request.user)
                else:
                    service.detach(occ, user=request.user)
                return redirect("studio:session-hosting", pk=occ.pk)
            else:
                raise ValidationError({"op": ["Unknown action."]})
        except ValidationError as exc:
            ctx["errors"] = _errors(exc)
    session = service.session_for(occ)
    ctx["session"] = service.serialize_session(session) if session is not None else None
    ctx["account_cards"] = [
        {"account": a, "describe": get_adapter(a.provider).describe()} for a in accounts
    ]
    return render(request, T + "session_hosting.html", ctx, status=400 if ctx["errors"] else 200)


_CONFIRM_SALT = "webinars.studio.hosting.confirm"
CONFIRM_MAX_AGE_SECONDS = 600


def _confirm_material(request, occ, account, action, external_id):
    session = service.session_for(occ)
    occ_now = Occurrence.objects.only("schedule_generation").get(pk=occ.pk)
    return {
        "u": request.user.pk, "o": str(occ.pk), "a": str(account.pk), "x": action, "e": external_id or "",
        "s": session.state if session is not None else "none", "g": occ_now.schedule_generation,
    }


def _confirm_token(request, occ, account, action, external_id):
    """Signed proof that this user previewed exactly this request against the current state."""
    from django.core import signing

    return signing.dumps(_confirm_material(request, occ, account, action, external_id), salt=_CONFIRM_SALT)


def _check_confirm_token(request, occ, account, action, external_id):
    from django.core import signing

    try:
        data = signing.loads(request.POST.get("confirm_token", ""), salt=_CONFIRM_SALT, max_age=CONFIRM_MAX_AGE_SECONDS)
    except signing.BadSignature:  # includes SignatureExpired
        raise ValidationError({"action": ["Preview the request again before confirming."]})
    if data != _confirm_material(request, occ, account, action, external_id):
        raise ValidationError({"action": ["The session changed since the preview. Preview again before confirming."]})


_REGISTRANT_SALT = "webinars.studio.hosting.registrant"


def _registrant_token(request, occ, pr):
    """Signed proof that this user saw this registrant in exactly this attempt/state."""
    from django.core import signing

    from webinars.api import hosting as api

    return signing.dumps({"u": request.user.pk, "o": str(occ.pk), "r": str(pr.registration_id),
                          "s": api.registrant_state(pr)}, salt=_REGISTRANT_SALT)


def _mask_email(email):
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "—"


@studio_view(perm=("webinars.view_occurrence", "webinars.change_occurrence"))
def session_registrants(request, pk):
    """Stalled Riverside registrants of one session (Administrators only: shows masked emails).

    Read-only on GET: no provider calls, no writes. POST records a decision the person
    verified in Riverside; the pasted link is stored encrypted and never rendered."""
    from django.core import signing

    from webinars.api import hosting as api
    from webinars.api.management import _Invalid

    denied = _admin_only(request)
    if denied:
        return denied
    occ = get_object_or_404(Occurrence.objects.select_related("event"), pk=pk)
    errors = {}
    if request.method == "POST":
        rid = request.POST.get("registration", "")
        try:
            try:
                data = signing.loads(request.POST.get("state_token", ""), salt=_REGISTRANT_SALT,
                                     max_age=CONFIRM_MAX_AGE_SECONDS)
            except signing.BadSignature:
                raise _Invalid(None, "This page is out of date; reload it and try again.")
            pr = api.registrant_target(occ, rid)
            decision, join_url, external_id = api.registrant_values(pr, {
                "decision": request.POST.get("decision", ""), "joinUrl": request.POST.get("join_url", ""),
                "externalId": request.POST.get("external_id", "")})
            if data != {"u": request.user.pk, "o": str(occ.pk), "r": str(pr.registration_id), "s": api.registrant_state(pr)}:
                raise _Invalid(None, "The registrant changed since this page loaded; reload and check again.")
            api.apply_registrant_reconcile(pr, decision, join_url, external_id, user=request.user)
            return redirect("studio:session-registrants", pk=occ.pk)
        except _Invalid as exc:
            errors = {"__all__": [exc.message, *((exc.fields or {}).values())]}
    rows = [{"pr": pr, "row": api.serialize_registrant(pr), "token": _registrant_token(request, occ, pr),
             "email": _mask_email(pr.registration.person.email)}
            for pr in api.stalled_registrants(occ)]
    return render(request, T + "registrants.html", {"occ": occ, "event": occ.event, "rows": rows, "errors": errors},
                  status=400 if errors else 200)


_ATTENDANCE_SALT = "webinars.studio.attendance"


def _attendance_material(request, occ, imp, op):
    from webinars import attendance_import as pipeline

    return {"u": request.user.pk, "o": str(occ.pk), "i": str(imp.pk), "s": pipeline.import_state(imp), "x": op}


def _attendance_token(request, occ, imp, op):
    """Signed proof that this user saw this import in exactly this state, for action ``op``."""
    from django.core import signing

    return signing.dumps(_attendance_material(request, occ, imp, op), salt=_ATTENDANCE_SALT)


@studio_view(perm=("webinars.view_occurrence", "webinars.change_occurrence"))
def session_attendance(request, pk):
    """Attendance imports of one session (Administrators / superusers only).

    GET lists imports and the selected import's rows with masked emails; it never calls a
    provider. POST ops: ``pull`` (one read-only provider call), ``upload`` (CSV parsed here,
    never stored), ``apply`` / ``no_shows`` / ``resolve`` / ``ignore`` (signed state token)."""
    from django.core import signing

    from webinars import attendance_import as pipeline
    from webinars.api import attendance_imports as api
    from webinars.api.management import _Invalid
    from webinars.models import AttendanceImport, Registration

    denied = _admin_only(request)
    if denied:
        return denied
    occ = get_object_or_404(Occurrence.objects.select_related("event"), pk=pk)
    pipeline.recover_stale_imports()
    errors = {}
    selected_id = request.GET.get("import", "")
    if request.method == "POST":
        op = request.POST.get("op", "")
        selected_id = request.POST.get("import", "")
        try:
            if op == "pull":
                imp = pipeline.pull_from_provider(occ, user=request.user)
            elif op == "upload":
                upload = request.FILES.get("file")
                if upload is None:
                    raise ValidationError({"file": ["Choose a CSV file."]})
                if upload.size > pipeline.MAX_FILE_BYTES:
                    raise ValidationError({"file": ["The file is larger than 1 MB."]})
                rows = pipeline.parse_csv(upload.read(pipeline.MAX_FILE_BYTES + 1))
                imp = pipeline.create_file_import(occ, rows, user=request.user)
            elif op in ("apply", "no_shows", "resolve", "ignore"):
                imp = api.import_target(occ, selected_id)
                try:
                    data = signing.loads(request.POST.get("state_token", ""), salt=_ATTENDANCE_SALT,
                                         max_age=CONFIRM_MAX_AGE_SECONDS)
                except signing.BadSignature:
                    raise _Invalid(None, "This page is out of date; reload it and try again.")
                token_op = op if op in ("apply", "no_shows") else f"resolve:{request.POST.get('row', '')}"
                if data != _attendance_material(request, occ, imp, token_op):
                    raise _Invalid(None, "The import changed since this page loaded; reload and check again.")
                state = pipeline.import_state(imp)
                if op == "apply":
                    pipeline.apply_import(imp, user=request.user, expected_state=state)
                elif op == "no_shows":
                    pipeline.mark_no_shows(imp, user=request.user, expected_state=state)
                else:
                    row = api.row_target(imp, request.POST.get("row", ""))
                    registration = None
                    if op == "resolve":
                        rid = request.POST.get("registration", "")
                        registration = Registration.objects.filter(pk=rid, occurrence_id=occ.pk).first() if _is_uuid(rid) else None
                        if registration is None:
                            raise ValidationError({"registration": ["Choose a registration of this session."]})
                    pipeline.resolve_row(imp, row, registration=registration, ignore=(op == "ignore"), user=request.user,
                                         expected_state=state)
            else:
                raise ValidationError({"op": ["Unknown action."]})
            return redirect(f"{request.path}?import={imp.pk}")
        except ValidationError as exc:
            errors = _errors(exc)
        except _Invalid as exc:
            errors = {"__all__": [exc.message, *((exc.fields or {}).values())]}
    imports = list(AttendanceImport.objects.filter(occurrence_id=occ.pk).order_by("-created_at")[:50])
    selected = next((i for i in imports if str(i.pk) == selected_id), imports[0] if imports else None)
    pull = pipeline.pull_preview(occ)
    ctx = {"occ": occ, "event": occ.event, "errors": errors, "pull": pull,
           "imports": [pipeline.serialize_import(i) for i in imports], "selected": None}
    if selected is not None:
        registrations = [
            {"id": str(reg.pk), "label": _mask_email(reg.person.email)}
            for reg in Registration.objects.filter(occurrence_id=occ.pk, status=Registration.Status.REGISTERED)
            .select_related("person").order_by("created_at")[:2000]
        ]
        rows = []
        for row in selected.rows.all()[:pipeline.MAX_ROWS]:
            rows.append({"row": pipeline.serialize_row(row), "reviewable": row.match in pipeline.REVIEWABLE,
                         "token": _attendance_token(request, occ, selected, f"resolve:{row.pk}")})
        ctx["selected"] = {
            "import": pipeline.serialize_import(selected), "rows": rows, "registrations": registrations,
            "apply": pipeline.apply_preview(selected), "no_shows": pipeline.no_show_preview(selected),
            "apply_token": _attendance_token(request, occ, selected, "apply"),
            "no_shows_token": _attendance_token(request, occ, selected, "no_shows"),
        }
    return render(request, T + "attendance.html", ctx, status=400 if errors else 200)


def _is_uuid(value):
    import uuid

    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False
