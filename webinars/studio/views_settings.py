"""Studio settings area: integrations, deliveries, API keys, team, invitations,
preview mailbox and the signed-in user's account.

Security notes
* Secrets (signing secrets, credential values, API keys, invitation tokens) are
  never rendered except on the single reveal-once response, which is ``no_store``.
* Encrypted destination fields are never decrypted for display; only
  "Signing secret set" and credential *names* are shown.
* Outbox payloads are never loaded for the deliveries list.
"""
import json

from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse

from webinars import invitations, mail, ratelimit, services
from webinars import native_integrations as native
from webinars import webhook_presets as presets
from webinars.admin import EVENT_TYPES
from webinars.audit import audit
from webinars.delivery import requeue_message
from webinars.models import ApiKey, ConsentRecord, Destination, Invitation, OutboxMessage, PreviewMessage
from webinars.normalization import FieldError, normalize_email
from webinars.studio.access import forbidden, no_store, render, studio_view, superuser_only

T = "studio/settings/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_dict(exc):
    """``{field: [messages]}`` from any ValidationError."""
    if hasattr(exc, "error_dict"):
        return {k: [str(m) for m in v] for k, v in exc.message_dict.items()}
    return {"_": [str(m) for m in exc.messages]}


def _summary(errors, labels):
    """Rows for the error summary: ``[{anchor, label, message}]``."""
    rows = []
    for key, msgs in errors.items():
        for msg in msgs:
            rows.append(
                {
                    "anchor": f"f-{key}" if key in labels else "",
                    "label": labels.get(key, ""),
                    "message": msg,
                }
            )
    return rows


def _options(choices, selected):
    """``choices``: iterable of (value, label, help). Returns template-ready dicts."""
    selected = set(selected or [])
    return [{"value": v, "label": label, "help": help_text, "checked": v in selected} for v, label, help_text in choices]


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------

EVENT_TYPE_HELP = {
    "registration.created": ("New registration", "Someone registers for a session."),
    "registration.updated": ("Registration updated", "A registrant changes their details or registers again."),
    "occurrence.cancelled": ("Session cancelled", "A session is cancelled."),
    "occurrence.rescheduled": ("Session rescheduled", "A session's start time changes."),
    "reminder.due": ("Reminder due", "A reminder should go out before a session starts."),
    "recording.published": ("Recording published", "A recording link is added to a session."),
    "attendance.recorded": ("Attendance recorded", "Someone is marked attended or no-show."),
    "attendance.checked_in": ("Checked in", "Someone enters the waiting room and is admitted."),
    "follow_up.requested": ("Follow-up requested", "A teammate flags a registrant for follow-up."),
}

GROUP_HELP = {
    "identity": ("Identity", "Email address and person id."),
    "profile": ("Profile", "Name, phone and job title."),
    "organization": ("Organization", "Company name and size."),
    "consents": ("Consents", "What the person agreed to, and when."),
    "attribution": ("Attribution", "UTM parameters and referrer."),
    "registration": ("Registration", "Registration id, status and answers to custom questions."),
    "event": ("Event", "Event title, session start time and time zone."),
}

CONSENT_HELP = {
    "sms": "Only send people who agreed to SMS messages.",
    "analytics": "Only send people who agreed to analytics tracking.",
    "advertising": "Only send people who agreed to advertising use (ad audiences).",
}

DEST_LABELS = {
    "name": "Name",
    "provider": "Provider",
    "preset": "Platform preset",
    "kind": "Destination type",
    "url": "Endpoint URL",
    "enabled": "Enabled",
    "event_types": "Events to send",
    "allowed_groups": "Data to include",
    "required_consents": "Required consent",
    "include_private_links": "Private links",
    "field_mapping": "Field mapping",
    "max_attempts": "Maximum attempts",
}

MAX_MAPPING_ROWS = 100


def _stored_credentials(dest):
    """``(credentials, readable)``. Values stay server-side; only names/flags are rendered."""
    if dest is None:
        return {}, True
    try:
        return native.load_credentials(dest), True
    except native.CredentialsUnavailable:
        return {}, False


def _native_state(dest):
    creds, _ = _stored_credentials(dest)
    provider = native.provider_of(creds)
    return provider, native.config_of(creds, provider)


def _apply_preset(state, data, creds, merged, errors):
    """Workflow-automation preset (Zapier / Make / n8n): validate the URL shape, pin the
    defaults and record ``webhook_preset`` in the credential dict.

    ``merged`` is ``_apply_native``'s result (None when the provider settings are unchanged).
    Returns the credential dict to store, or None when nothing changes.
    """
    preset = state["preset"]
    if preset and preset not in presets.PRESETS:
        errors["preset"] = ["Choose a preset from the list."]
        return merged
    base = creds if merged is None else merged
    if preset and (state["provider"] or native.provider_of(base)):
        errors["preset"] = ["Choose either a native provider or a platform preset, not both."]
        return merged
    if preset:
        problem = presets.url_problem(preset, data.get("url", ""))
        if problem:
            errors["url"] = [problem]
        presets.pin_defaults(data)
        state["kind"] = data["kind"]
        state["include_private_links"] = False
    out = presets.with_preset(base, preset)
    return None if out == creds else out


def _native_from_post(post, provider):
    return {f.name: (post.get(f"native-{f.name}") or "").strip() for f in native.FIELDS.get(provider, ()) if not f.secret}


def _secrets_from_post(post, provider):
    """Provider secrets typed into the create form (``secret-<name>``); blanks are dropped."""
    out = {}
    for f in native.FIELDS.get(provider, ()):
        if f.secret:
            value = (post.get(f"secret-{f.name}") or "").strip()
            if value:
                out[f.name] = value
    return out


def _native_fields(provider, config, creds):
    """Template rows for the provider's non-secret settings."""
    flags = native.secrets_set(creds, provider)
    rows = []
    for f in native.FIELDS.get(provider, ()):
        rows.append(
            {
                "name": f.name,
                "label": f.label,
                "help": f.help,
                "secret": f.secret,
                "required": f.required,
                "choices": f.choices,
                "multiline": f.multiline,
                "value": "" if f.secret else config.get(f.name, ""),
                "is_set": flags.get(f.name, False),
            }
        )
    return rows


def _apply_native(dest, state, data, creds, errors):
    """Merge the posted provider settings into the credential dict (secrets untouched).

    Returns ``(new_credentials or None when unchanged, notes)``. Fills ``errors`` with
    validation problems; enabling is refused unless the merged configuration validates.
    """
    provider = state["provider"]
    if provider not in native.FIELDS and provider:
        errors["provider"] = ["Choose a provider from the list."]
        return None, []
    previous = native.provider_of(creds)
    if not provider and not previous:
        return None, []
    # Non-secret settings, plus secrets typed into the create form (the edit page changes
    # secrets through the separate credential form; blank values keep what is stored).
    posted = dict(state["native"])
    secrets = state.get("secrets") or {}
    if provider and secrets:
        for f in native.FIELDS[provider]:
            if f.secret and secrets.get(f.name):
                if f.pattern is not None and not f.pattern.match(secrets[f.name]):
                    errors[f"secret-{f.name}"] = [f"{f.label} looks invalid (no spaces, 6–512 characters)."]
                else:
                    posted[f.name] = secrets[f.name]
    if provider and not posted.get("region"):
        posted["region"] = native.FIELDS[provider][0].choices[0][0]
    merged, notes = native.merge_credentials(creds, provider, posted)
    if provider:
        # The platform's own rules (analytics tools gate on analytics consent) are applied, not asked.
        for purpose in native.mandatory_consents(provider):
            if purpose not in state["required_consents"]:
                state["required_consents"].append(purpose)
        data["required_consents"] = json.dumps(state["required_consents"])
        url = native.base_url(provider, merged)
        if url:
            data["url"] = url
            state["url"] = url
        if state["enabled"]:
            problems = native.validate(
                provider,
                merged,
                event_types=state["event_types"],
                allowed_groups=state["allowed_groups"],
                required_consents=state["required_consents"],
            )
            if problems:
                errors.update({k: v for k, v in problems.items()})
                errors.setdefault("enabled", []).append("Fix the problems above before enabling this integration.")
    return merged, notes


def _dest_state(dest):
    if dest is None:
        return {
            "provider": "",
            "native": {},
            "secrets": {},
            "preset": "",
            "name": "",
            "kind": Destination.Kind.GENERIC,
            "url": "",
            "enabled": False,
            "event_types": ["registration.created"],
            "allowed_groups": ["identity", "profile", "registration", "event"],
            "required_consents": [],
            "include_private_links": False,
            "mapping": [],
            "max_attempts": 8,
        }
    provider, config = _native_state(dest)
    return {
        "provider": provider,
        "native": config,
        "secrets": {},
        "preset": presets.destination_preset(dest),
        "name": dest.name,
        "kind": dest.kind,
        "url": dest.url,
        "enabled": dest.enabled,
        "event_types": list(dest.event_types or []),
        "allowed_groups": list(dest.allowed_groups or []),
        "required_consents": list(dest.required_consents or []),
        "include_private_links": dest.include_private_links,
        "mapping": [{"source": k, "target": v} for k, v in (dest.field_mapping or {}).items()],
        "max_attempts": dest.max_attempts,
    }


def _dest_state_from_post(post):
    mapping = []
    for i in range(MAX_MAPPING_ROWS):
        source = (post.get(f"map-{i}-source") or "").strip()
        target = (post.get(f"map-{i}-target") or "").strip()
        if source or target:
            mapping.append({"source": source, "target": target})
    provider = (post.get("provider") or "").strip()
    return {
        "provider": provider,
        "native": _native_from_post(post, provider),
        "secrets": _secrets_from_post(post, provider),
        "preset": (post.get("preset") or "").strip(),
        "name": post.get("name", ""),
        "kind": post.get("kind", ""),
        "url": post.get("url", ""),
        "enabled": bool(post.get("enabled")),
        "event_types": post.getlist("event_types"),
        "allowed_groups": post.getlist("allowed_groups"),
        "required_consents": post.getlist("required_consents"),
        "include_private_links": bool(post.get("include_private_links")),
        "mapping": mapping,
        "max_attempts": post.get("max_attempts", ""),
    }


def _dest_data(state):
    """Convert UI state into DestinationForm data (JSON strings for list/dict fields)."""
    errors = {}
    mapping = {}
    for row in state["mapping"]:
        if not row["source"] or not row["target"]:
            errors["field_mapping"] = ["Each mapping row needs both our field name and your field name."]
            continue
        mapping[row["source"]] = row["target"]
    data = {
        "name": state["name"],
        "kind": state["kind"],
        "url": state["url"],
        "event_types": json.dumps(state["event_types"]),
        "allowed_groups": json.dumps(state["allowed_groups"]),
        "required_consents": json.dumps(state["required_consents"]),
        "field_mapping": json.dumps(mapping),
        "max_attempts": state["max_attempts"],
    }
    # Checkbox semantics: absent key == False for the ModelForm's BooleanField.
    if state["enabled"]:
        data["enabled"] = "on"
    if state["include_private_links"]:
        data["include_private_links"] = "on"
    return data, errors


def _dest_context(request, dest, state, errors=None):
    errors = errors or {}
    rows = list(state["mapping"]) + [{"source": "", "target": ""} for _ in range(3)]
    creds, readable = _stored_credentials(dest)
    stored_provider = native.provider_of(creds)
    readiness = []
    if stored_provider:
        problems = native.validate(
            stored_provider,
            creds,
            event_types=dest.event_types,
            allowed_groups=dest.allowed_groups,
            required_consents=dest.required_consents,
        )
        readiness = [m for msgs in problems.values() for m in msgs]
        # Bounded code + fixed guidance only; never the sample payload or provider text.
        refusal = native.preview_refusal(stored_provider, creds)
        if refusal:
            readiness.append(f"Preview unavailable ({refusal[0]}): {refusal[1]}")
    context_native_fields = _native_fields(state["provider"], state["native"], creds if state["provider"] == stored_provider else {})
    context = {
        "provider_choices": native.PROVIDER_CHOICES,
        "provider_label": native.PROVIDER_LABELS.get(state["provider"], ""),
        "preset_choices": presets.CHOICES,
        "preset_info": presets.describe(state["preset"]),
        "stored_preset_info": presets.describe(presets.preset_of(creds)),
        "native_fields": _native_fields(state["provider"], state["native"], creds if state["provider"] == stored_provider else {}),
        # Create page only: secret boxes live in the main form so the key is stored at creation.
        "new_secret_fields": [r for r in _native_fields(state["provider"], {}, {}) if r["secret"]] if dest is None else [],
        # Required settings stay in view; optional ones sit behind a disclosure, open when one is set or wrong.
        "native_fields_basic": [r for r in context_native_fields if not r["secret"] and r["required"]],
        "native_fields_advanced": [r for r in context_native_fields if not r["secret"] and not r["required"]],
        "native_advanced_open": any(r["value"] for r in context_native_fields if not r["secret"] and not r["required"])
        or any(k.startswith("native-") and k[len("native-"):] in {r["name"] for r in context_native_fields if not r["required"]} for k in errors),
        "stored_provider": stored_provider,
        "stored_provider_label": native.PROVIDER_LABELS.get(stored_provider, ""),
        "secret_fields": [r for r in _native_fields(stored_provider, {}, creds) if r["secret"]],
        "native_preview": native.preview(stored_provider, creds) if stored_provider else [],
        "native_readiness": readiness,
        "credentials_readable": readable,
        # Per provider: a module may declare its own EVENT_TYPES (transactional email sends
        # reminders and changes; CRM adapters only the person-linked lifecycle events).
        "native_event_types": native.supported_event_types(state["provider"]),
        "mandatory_consents": [label for value, label in ConsentRecord.Purpose.choices if value in native.mandatory_consents(state["provider"])],
        "generic_credential_names": [n for n in (services.credential_names(dest) if dest else []) if not n.startswith(native.NATIVE_PREFIX)],
        **_base_context(request, dest, state, errors, rows),
    }
    if state["provider"] in native.FIELDS:
        # Only offer what the selected provider can send; validate() refuses anything else anyway.
        supported = set(context["native_event_types"])
        context["event_type_options"] = [o for o in context["event_type_options"] if o["value"] in supported]
    return context


def _base_context(request, dest, state, errors, rows):
    return {
        "destination": dest,
        "state": state,
        "errors": errors,
        "summary": _summary(errors, DEST_LABELS),
        "kinds": Destination.Kind.choices,
        "event_type_options": _options(
            [(e, *EVENT_TYPE_HELP.get(e, (e, ""))) for e in EVENT_TYPES], state["event_types"]
        ),
        "group_options": _options(
            [(g, *GROUP_HELP.get(g, (g.title(), ""))) for g in Destination.GROUPS], state["allowed_groups"]
        ),
        "consent_options": _options(
            [(v, label, CONSENT_HELP.get(v, "")) for v, label in ConsentRecord.Purpose.choices],
            state["required_consents"],
        ),
        "mapping_rows": rows,
        "can_edit": request.user.has_perm("webinars.change_destination") if dest else True,
        "can_delete": request.user.has_perm("webinars.delete_destination"),
        "has_secret": bool(dest and dest.signing_secret_encrypted),
        "credential_names": services.credential_names(dest) if dest else [],
    }


@studio_view(perm="webinars.view_destination", methods=("GET",))
def integrations(request):
    destinations = Destination.objects.order_by("name")
    rows = [
        {
            "obj": d,
            "has_secret": bool(d.signing_secret_encrypted),
            "event_count": len(d.event_types or []),
        }
        for d in destinations
    ]
    return render(
        request,
        T + "integrations.html",
        {"rows": rows, "can_add": request.user.has_perm("webinars.add_destination")},
    )


@studio_view(perm=("webinars.view_destination", "webinars.add_destination"))
def integration_new(request):
    if request.method == "POST":
        state = _dest_state_from_post(request.POST)
        data, errors = _dest_data(state)
        merged, _notes = _apply_native(None, state, data, {}, errors)
        merged = _apply_preset(state, data, {}, merged, errors)
        if not errors:
            try:
                with transaction.atomic():
                    dest = services.save_destination(
                        data, user=request.user, pinned_hosts=native.allowed_hosts(state["provider"], merged or {})
                    )
                    if merged:
                        services.set_credentials(dest, merged, user=request.user)
            except ValidationError as exc:
                errors = _error_dict(exc)
            else:
                if merged:
                    messages.success(
                        request,
                        f"“{dest.name}” was created. Add the {native.PROVIDER_LABELS[state['provider']]} credentials, "
                        "check the preview, then enable it.",
                    )
                else:
                    messages.success(
                        request,
                        f"“{dest.name}” was created. Add a signing secret so the receiver can verify our requests.",
                    )
                return redirect("studio:integration-edit", pk=dest.pk)
        return render(request, T + "integration_form.html", _dest_context(request, None, state, errors), status=400)
    # ?provider= / ?preset= / ?name= let the page show the chosen provider's settings and the
    # credential boxes before anything is saved (studio.js reloads with these on change).
    state = _dest_state(None)
    provider = (request.GET.get("provider") or "").strip()
    preset = (request.GET.get("preset") or "").strip()
    if provider in native.FIELDS:
        state["provider"] = provider
        state["native"] = {"region": native.FIELDS[provider][0].choices[0][0]}
        state["url"] = native.base_url(provider, native.merge_credentials({}, provider, state["native"])[0]) or ""
        state["event_types"] = [e for e in state["event_types"] if e in native.supported_event_types(provider)] or list(
            native.supported_event_types(provider)[:1]
        )
    elif preset in presets.PRESETS:
        state["preset"] = preset
        state["kind"] = Destination.Kind.GENERIC
    state["name"] = (request.GET.get("name") or "")[:200]
    return render(request, T + "integration_form.html", _dest_context(request, None, state))


@studio_view(perm="webinars.view_destination", write_perm="webinars.change_destination")
def integration_edit(request, pk):
    dest = get_object_or_404(Destination, pk=pk)
    if request.method == "POST":
        state = _dest_state_from_post(request.POST)
        data, errors = _dest_data(state)
        creds, readable = _stored_credentials(dest)
        merged, notes = None, []
        if not readable:
            if state["provider"]:
                errors.setdefault("provider", []).append(
                    "Stored credentials can't be read (encryption key changed?). Re-enter them before changing the provider."
                )
            if state["preset"]:
                errors.setdefault("preset", []).append(
                    "Stored credentials can't be read (encryption key changed?). Re-enter them before setting a preset."
                )
        else:
            merged, notes = _apply_native(dest, state, data, creds, errors)
            merged = _apply_preset(state, data, creds, merged, errors)
        if not errors:
            try:
                with transaction.atomic():
                    dest = services.save_destination(
                        data, user=request.user, instance=dest,
                        pinned_hosts=native.allowed_hosts(state["provider"], merged if merged is not None else creds),
                    )
                    if merged is not None and merged != creds:
                        services.set_credentials(dest, merged, user=request.user)
            except ValidationError as exc:
                errors = _error_dict(exc)
                dest = Destination.objects.get(pk=pk)
            else:
                messages.success(request, "Integration saved.")
                for note in notes:
                    messages.warning(request, note)
                return redirect("studio:integration-edit", pk=dest.pk)
        return render(request, T + "integration_form.html", _dest_context(request, dest, state, errors), status=400)
    return render(request, T + "integration_form.html", _dest_context(request, dest, _dest_state(dest)))


@studio_view(perm=("webinars.view_destination", "webinars.change_destination"), methods=("POST",))
def integration_secret(request, pk):
    dest = get_object_or_404(Destination, pk=pk)
    action = request.POST.get("action")
    if action == "generate":
        secret = services.set_signing_secret(dest, user=request.user)
        response = render(request, T + "secret_reveal.html", {"destination": dest, "secret": secret})
        return no_store(response)
    if action == "set":
        try:
            services.set_signing_secret(dest, user=request.user, secret=request.POST.get("signing_secret") or "")
        except ValidationError as exc:
            for msgs in _error_dict(exc).values():
                for msg in msgs:
                    messages.error(request, msg)
        else:
            messages.success(request, "Signing secret saved. It won't be shown again.")
        return redirect("studio:integration-edit", pk=dest.pk)
    messages.error(request, "Choose whether to generate a secret or paste your own.")
    return redirect("studio:integration-edit", pk=dest.pk)


@studio_view(perm=("webinars.view_destination", "webinars.change_destination"), methods=("POST",))
def integration_credentials(request, pk):
    dest = get_object_or_404(Destination, pk=pk)
    existing, readable = _stored_credentials(dest)
    if request.POST.get("action") == "native":
        return _native_secrets(request, dest, existing, readable)
    credentials, skipped = {}, []
    for i in range(MAX_MAPPING_ROWS):
        name = (request.POST.get(f"cred-{i}-name") or "").strip()
        value = request.POST.get(f"cred-{i}-value") or ""
        if not name:
            continue
        if name.startswith(native.NATIVE_PREFIX) or name == presets.PRESET_KEY:
            messages.error(request, f"“{name}” is reserved for provider settings; use the provider form.")
            return redirect("studio:integration-edit", pk=dest.pk)
        if not value.strip():
            skipped.append(name)
            continue
        credentials[name] = value.strip()
    generic_count = len(credentials)
    # The generic set replaces only generic credentials; provider settings, secrets and the preset are kept.
    credentials.update({k: v for k, v in existing.items() if k.startswith(native.NATIVE_PREFIX) or k == presets.PRESET_KEY})
    try:
        services.set_credentials(dest, credentials, user=request.user)
    except ValidationError as exc:
        for msgs in _error_dict(exc).values():
            for msg in msgs:
                messages.error(request, msg)
        return redirect("studio:integration-edit", pk=dest.pk)
    if generic_count:
        messages.success(request, f"Saved {generic_count} credential(s). Values are encrypted and never shown again.")
    else:
        messages.success(request, "Credentials cleared.")
    if skipped:
        messages.warning(request, "Not saved because no value was entered: " + ", ".join(skipped) + ".")
    return redirect("studio:integration-edit", pk=dest.pk)


def _native_secrets(request, dest, existing, readable):
    """Explicit provider-secret submit. Blank fields keep the stored value; values are never echoed."""
    provider = native.provider_of(existing)
    if not readable or not provider:
        messages.error(request, "Choose a provider and save the integration before adding its credentials.")
        return redirect("studio:integration-edit", pk=dest.pk)
    secret_fields = [f for f in native.FIELDS[provider] if f.secret]
    posted = {f.name: (request.POST.get(f"secret-{f.name}") or "").strip() for f in secret_fields}
    posted.update(native.config_of(existing, provider))
    merged, _notes = native.merge_credentials(existing, provider, posted)
    changed = [f.label for f in secret_fields if posted.get(f.name)]
    if not changed:
        messages.info(request, "No new values entered; stored credentials are unchanged.")
        return redirect("studio:integration-edit", pk=dest.pk)
    bad = [
        f.label
        for f in secret_fields
        if posted.get(f.name) and f.pattern is not None and not f.pattern.match(posted[f.name])
    ]
    if bad:
        messages.error(request, "Not saved: " + ", ".join(bad) + " looks invalid (no spaces, 6–512 characters).")
        return redirect("studio:integration-edit", pk=dest.pk)
    services.set_credentials(dest, merged, user=request.user)
    messages.success(request, "Saved " + ", ".join(changed) + ". Values are encrypted and never shown again.")
    return redirect("studio:integration-edit", pk=dest.pk)


@studio_view(perm=("webinars.view_destination", "webinars.delete_destination"), methods=("POST",))
def integration_delete(request, pk):
    dest = get_object_or_404(Destination, pk=pk)
    name = dest.name
    audit("destination.deleted", user=request.user, obj=dest)
    dest.delete()
    messages.success(request, f"“{name}” was deleted, along with its queued deliveries.")
    return redirect("studio:integrations")


# ---------------------------------------------------------------------------
# Deliveries
# ---------------------------------------------------------------------------

STATUS_BADGE = {
    "pending": "waiting",
    "in_flight": "hold",
    "delivered": "success",
    "dead": "danger",
    "cancelled": "draft",
}


@studio_view(perm="webinars.view_outboxmessage", methods=("GET",))
def deliveries(request):
    status = request.GET.get("status", "")
    qs = OutboxMessage.objects.select_related("destination").defer("payload").order_by("-created_at")
    if status in OutboxMessage.Status.values:
        qs = qs.filter(status=status)
    else:
        status = ""
    rows = [{"obj": m, "badge": STATUS_BADGE.get(m.status, "draft")} for m in qs[:100]]
    return render(
        request,
        T + "deliveries.html",
        {
            "rows": rows,
            "status": status,
            "statuses": OutboxMessage.Status.choices,
            "can_retry": request.user.has_perm("webinars.change_outboxmessage"),
        },
    )


@studio_view(perm=("webinars.view_outboxmessage", "webinars.change_outboxmessage"), methods=("POST",))
def delivery_retry(request, pk):
    message = get_object_or_404(OutboxMessage, pk=pk)
    if requeue_message(message, user=request.user):
        messages.success(request, "Delivery queued for another attempt.")
    else:
        messages.error(request, "Only dead-lettered deliveries with their data still available can be retried.")
    target = reverse("studio:deliveries")
    status = request.POST.get("status", "")
    if status in OutboxMessage.Status.values:
        target += f"?status={status}"
    return redirect(target)


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

SCOPE_HELP = {
    "public_read": "Read published events, sessions, hosts and topics. Safe for websites and the MCP server.",
    "register": "Submit registrations from a browser or form on your own site.",
    "proxy": "For your own server that forwards visitors' registrations (first-party proxy).",
    "trusted": "For trusted back-end systems that send already-verified person ids. Keep this key on a server.",
    "reporting": "Read registration and attendance reports. Includes personal data, so share carefully.",
    "content_write": "Create and edit events, sessions, hosts, topics and registration forms.",
    "attendance_write": "Mark registrants as attended, no-show or needing follow-up.",
    "config_read": "Read redacted configuration and delivery status, and read (never change) events and sessions, "
                   "drafts included, over the management API. No secrets, join links or personal data.",
    "publication": "Publish events and recordings (confirmation required). Superuser-granted.",
    "delivery_admin": "Manage integrations, set-only secrets, retries and test sends. Superuser-granted.",
    "keys_admin": "Create and revoke API keys within the scopes this key holds. Superuser-granted.",
    "team_admin": "Invite and manage Editors and Analysts. Superuser-granted.",
    "payments_admin": "Configure Stripe (set-only keys) and record refunds made in Stripe. Superuser-granted.",
    "hosting_admin": "Connect Zoom, Wistia, Sequel, Riverside or StreamYard accounts (set-only credentials). Superuser-granted.",
    "hosting_provision": "Preview and confirm creating, updating or attaching provider sessions. Superuser-granted.",
}


def _mcp_scopes():
    """Scopes that unlock at least one MCP tool, derived from the live tool registry."""
    from webinars.mcp import TOOLS

    return {str(s) for tool in TOOLS for s in tool["scopes"]}


def _scope_options(selected=()):
    selected = set(selected)
    mcp_scopes = _mcp_scopes()
    return [
        {
            "value": value,
            "label": label,
            "help": SCOPE_HELP.get(value, ""),
            "mcp": value in mcp_scopes,
            "checked": value in selected,
            "admin": "superuser-granted" in (label + SCOPE_HELP.get(value, "")).lower(),
        }
        for value, label in ApiKey.Scope.choices
    ]


def _api_keys_page(request, *, name="", scopes=(), errors=None, status=200):
    errors = errors or {}
    keys = ApiKey.objects.order_by("revoked_at", "-created_at")
    scope_labels = dict(ApiKey.Scope.choices)
    rows = [{"obj": k, "scope_labels": [scope_labels.get(s, s) for s in (k.scopes or [])]} for k in keys]
    return render(
        request,
        T + "api_keys.html",
        {
            "rows": rows,
            "scope_options": _scope_options(scopes),
            "admin_scope_checked": any(o["checked"] and o["admin"] for o in _scope_options(scopes)),
            "name": name,
            "errors": errors,
            "summary": _summary(errors, {"name": "Key name", "scopes": "Scopes"}),
            "can_add": request.user.has_perm("webinars.add_apikey"),
            "can_revoke": request.user.has_perm("webinars.change_apikey"),
        },
        status=status,
    )


@studio_view(perm="webinars.view_apikey", methods=("GET",))
def api_keys(request):
    return _api_keys_page(request)


@studio_view(perm=("webinars.view_apikey", "webinars.add_apikey"), methods=("POST",))
def api_key_new(request):
    name = request.POST.get("name", "")
    scopes = request.POST.getlist("scopes")
    try:
        key, raw = services.create_api_key(name, scopes, user=request.user)
    except ValidationError as exc:
        return _api_keys_page(request, name=name, scopes=scopes, errors=_error_dict(exc), status=400)
    scope_labels = dict(ApiKey.Scope.choices)
    response = render(
        request,
        T + "api_key_reveal.html",
        {
            "key": key,
            "raw_key": raw,
            "scope_labels": [scope_labels.get(s, s) for s in key.scopes],
            "mcp_ready": bool(set(key.scopes) & _mcp_scopes()),
        },
    )
    return no_store(response)


@studio_view(perm=("webinars.view_apikey", "webinars.change_apikey"), methods=("POST",))
def api_key_revoke(request, pk):
    key = get_object_or_404(ApiKey, pk=pk)
    if services.revoke_api_key(key, user=request.user):
        messages.success(request, f"“{key.name}” was revoked. Requests using it now fail.")
    else:
        messages.info(request, f"“{key.name}” was already revoked.")
    return redirect("studio:api-keys")


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------

# Keep in step with webinars/roles.py.
ROLE_HELP = {
    "Administrator": (
        "Everything in the studio, including integrations, API keys and deliveries. "
        "Can't manage the team or open the preview mailbox."
    ),
    "Editor": (
        "Creates and edits events, sessions, forms, hosts and topics, and records attendance. "
        "Can't see registrations."
    ),
    "Analyst": "Read-only access to events, sessions, registrations and reports. Can't change anything.",
}
INVITE_STATUS_BADGE = {"pending": "waiting", "accepted": "success", "revoked": "revoked", "expired": "expired"}


def _role_options(selected=None):
    return [{"value": r, "label": r, "help": ROLE_HELP.get(r, ""), "checked": r == selected} for r in invitations.ROLES]


def _can_view_team(user):
    return user.is_superuser or user.has_perm("auth.view_user")


def _team_page(request, *, email="", role=None, errors=None, status=200):
    errors = errors or {}
    User = get_user_model()
    members = [
        {"obj": u, "role": invitations.role_of(u) or "No role"}
        for u in User.objects.order_by("username").prefetch_related("groups")
    ]
    invites = [
        {"obj": inv, "status": inv.status, "badge": INVITE_STATUS_BADGE.get(inv.status, "draft")}
        for inv in Invitation.objects.select_related("invited_by").order_by("-created_at")[:100]
    ]
    return render(
        request,
        T + "team.html",
        {
            "members": members,
            "invites": invites,
            "is_superuser": request.user.is_superuser,
            "role_options": _role_options(role or "Editor"),
            "email": email,
            "errors": errors,
            "summary": _summary(errors, {"email": "Email address", "role": "Role"}),
            "mail_mode": mail.delivery_mode(),
        },
        status=status,
    )


@studio_view(methods=("GET",))
def team(request):
    if not _can_view_team(request.user):
        return forbidden(request)
    return _team_page(request)


@superuser_only
def team_invite(request):
    if request.method != "POST":
        return redirect("studio:team")
    email = request.POST.get("email", "")
    role = request.POST.get("role", "")
    try:
        invitation, token = invitations.create_invitation(actor=request.user, email=email, role=role)
    except PermissionDenied:
        return forbidden(request, "Only superusers manage the team.")
    except ValidationError as exc:
        return _team_page(request, email=email, role=role, errors=_error_dict(exc), status=400)
    response = render(
        request,
        T + "invite_reveal.html",
        {
            "invitation": invitation,
            "accept_url": invitations.accept_url(token),
            "email_sent": invitation.email_sent,
            "mail_mode": mail.delivery_mode(),
        },
    )
    return no_store(response)


@superuser_only
def invitation_revoke(request, pk):
    if request.method != "POST":
        return redirect("studio:team")
    invitation = get_object_or_404(Invitation, pk=pk)
    try:
        revoked = invitations.revoke_invitation(actor=request.user, invitation=invitation)
    except PermissionDenied:
        return forbidden(request, "Only superusers manage the team.")
    if revoked:
        messages.success(request, f"The invitation for {invitation.email} was revoked. Its link no longer works.")
    else:
        messages.info(request, "That invitation was already used or revoked.")
    return redirect("studio:team")


@superuser_only
def member_edit(request, pk):
    User = get_user_model()
    member = get_object_or_404(User, pk=pk)
    current_role = invitations.role_of(member)
    errors = {}
    state = {"email": member.email, "role": current_role, "is_active": member.is_active}
    if request.method == "POST":
        state = {
            "email": request.POST.get("email", ""),
            "role": request.POST.get("role", ""),
            "is_active": bool(request.POST.get("is_active")),
        }
        try:
            member, changed = invitations.update_member(
                actor=request.user,
                member=member,
                role=state["role"] or None,
                email=state["email"],
                is_active=state["is_active"],
            )
        except PermissionDenied:
            return forbidden(request, "Only superusers manage the team.")
        except ValidationError as exc:
            errors = _error_dict(exc)
        else:
            messages.success(request, "Member updated." if changed else "No changes to save.")
            return redirect("studio:team")
    editable = not member.is_superuser and member.pk != request.user.pk
    return render(
        request,
        T + "member_edit.html",
        {
            "member": member,
            "current_role": current_role,
            "state": state,
            "role_options": _role_options(state["role"]),
            "errors": errors,
            "summary": _summary(errors, {"email": "Email address", "role": "Role", "is_active": "Access"}),
            "editable": editable,
        },
        status=400 if errors else 200,
    )


INVITE_SESSION_KEY = "studio_invite_token"


def invite_accept(request, token=None):
    """Public: set a username and password to join the studio.

    The emailed link ``/studio/invite/<token>/`` is exchanged on GET: the token moves
    into the session and the browser is redirected to the token-less
    ``/studio/invite/``, so the token is not kept in the address bar or history and
    is not sent again by the form POST (it still appears once in access logs; redact
    ``/studio/invite/*`` at the proxy). A POST to the token URL keeps working.
    Acceptance itself (hash lookup, ``select_for_update``) is in ``webinars.invitations``.
    """
    if token is None:
        token = request.session.get(INVITE_SESSION_KEY, "")
    elif request.method == "GET":
        if invitations.find_pending(token) is None:
            return _private(TemplateResponse(request, T + "invite_invalid.html", {}, status=404))
        request.session[INVITE_SESSION_KEY] = token
        return _private(redirect("studio:invite-accept-pending"))
    invitation = invitations.find_pending(token)
    errors = {}
    username = ""
    if request.method == "POST":
        if not ratelimit.hit(request, "invite_accept"):
            response = TemplateResponse(
                request, T + "invite_invalid.html", {"rate_limited": True}, status=429
            )
            response["Retry-After"] = "60"
            return _private(response)
        if invitation is None:
            return _private(TemplateResponse(request, T + "invite_invalid.html", {}, status=404))
        username = request.POST.get("username", "")
        try:
            user = invitations.accept_invitation(
                token,
                username=username,
                password=request.POST.get("password", ""),
                password_confirm=request.POST.get("password_confirm", ""),
            )
        except ValidationError as exc:
            errors = _error_dict(exc)
            if invitations.find_pending(token) is None:
                return _private(TemplateResponse(request, T + "invite_invalid.html", {}, status=404))
        else:
            request.session.pop(INVITE_SESSION_KEY, None)
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(request, f"Welcome to the studio, {user.get_username()}.")
            return redirect("/studio/")
    elif request.method != "GET":
        return _private(TemplateResponse(request, T + "invite_invalid.html", {}, status=405))
    if invitation is None:
        return _private(TemplateResponse(request, T + "invite_invalid.html", {}, status=404))
    response = TemplateResponse(
        request,
        T + "invite_accept.html",
        {
            "invitation": invitation,
            "role_help": ROLE_HELP.get(invitation.role, ""),
            "username": username,
            "errors": errors,
            "summary": _summary(
                errors, {"username": "Username", "password": "Password", "password_confirm": "Confirm password"}
            ),
        },
        status=400 if errors else 200,
    )
    return _private(response)


def _private(response):
    """Invitation pages carry a token in the URL: never cache, never leak it as a referrer."""
    no_store(response)
    response["Referrer-Policy"] = "same-origin"
    return response


# ---------------------------------------------------------------------------
# Preview mailbox
# ---------------------------------------------------------------------------


def _mailbox_allowed(user):
    """Superusers only: the mailbox holds invitation accept links and registrants' codes/magic links,
    so any other reader could accept an invitation (team is superuser-only) or pose as a registrant."""
    return mail.preview_mailbox_enabled() and user.is_superuser


@studio_view(methods=("GET",))
def mailbox(request):
    if not _mailbox_allowed(request.user):
        raise Http404("Mailbox not available")
    items = PreviewMessage.objects.order_by("-created_at").only("id", "to", "subject", "created_at")[:200]
    return render(request, T + "mailbox.html", {"items": items})


@studio_view(methods=("GET",))
def mailbox_message(request, pk):
    if not _mailbox_allowed(request.user):
        raise Http404("Mailbox not available")
    message = get_object_or_404(PreviewMessage, pk=pk)
    response = render(request, T + "mailbox_message.html", {"item": message, "body": mail.read_preview_body(message)})
    return no_store(response)


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


@studio_view()
def account(request):
    user = request.user
    email = user.email
    email_errors = {}
    password_form = PasswordChangeForm(user)
    status = 200
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "email":
            email = request.POST.get("email", "")
            normalized = None
            try:
                normalized = normalize_email(email)
            except FieldError as exc:
                email_errors["email"] = [exc.message]
            # Re-authenticate: the email is used for password resets, so a hijacked
            # session must not be able to redirect them.
            if not user.check_password(request.POST.get("current_password", "")):
                email_errors["current_password"] = ["Enter your current password to change your email address."]
            if not email_errors:
                User = get_user_model()
                if User.objects.filter(email__iexact=normalized).exclude(pk=user.pk).exists():
                    email_errors = {"email": ["Someone on the team already uses this email."]}
                elif normalized == user.email:
                    messages.info(request, "That's already your email address.")
                    return redirect("studio:account")
                else:
                    user.email = normalized
                    user.save(update_fields=["email"])
                    audit("account.email_changed", user=user, obj=user)
                    messages.success(request, "Your email address was updated.")
                    return redirect("studio:account")
            status = 400
        elif action == "password":
            password_form = PasswordChangeForm(user, request.POST)
            if password_form.is_valid():
                password_form.save()
                update_session_auth_hash(request, password_form.user)
                audit("account.password_changed", user=user, obj=user)
                messages.success(request, "Your password was changed. You're still signed in.")
                return redirect("studio:account")
            status = 400
        else:
            messages.error(request, "Something went wrong with that form. Please try again.")
            return redirect("studio:account")
    password_errors = {k: [str(m) for m in v] for k, v in password_form.errors.items()} if password_form.is_bound else {}
    summary = _summary(email_errors, {"email": "Email address", "current_password": "Current password"}) + _summary(
        password_errors,
        {"old_password": "Current password", "new_password1": "New password", "new_password2": "Confirm new password"},
    )
    return render(
        request,
        T + "account.html",
        {
            "email": email,
            "email_errors": email_errors,
            "password_errors": password_errors,
            "summary": summary,
            "role": invitations.role_of(user) or "No role",
        },
        status=status,
    )
