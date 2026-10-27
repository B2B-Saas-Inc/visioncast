"""Write services shared by the studio UI and the MCP server.

These reuse the exact validation and lifecycle code already used by Django admin
and the management API: ``admin.EventForm`` / ``admin.OccurrenceForm`` /
``admin.FormVersionForm`` / ``admin.DestinationForm``, ``admin.persist_occurrence``,
``admin.event_snapshot`` + ``EventRevision``, ``scheduling.*`` and
``api.auth.generate_api_key``. Callers are responsible for authorization (studio:
Django permissions; MCP: API key scopes). Every write is audited without PII or
secrets.
"""
import json
import re
from datetime import timezone as dt_timezone
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from webinars.admin import DestinationForm, EventForm, FormVersionForm, OccurrenceForm, event_snapshot, persist_occurrence
from webinars.audit import audit
from webinars.forms_schema import build_definition
from webinars.models import ApiKey, Destination, Event, EventRevision, FormVersion, Occurrence, Registration

VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Scopes that act like an owner (publish, deliver data, mint keys, manage the team, or
# ``trusted``: assert verified organization IDs that native integrations rely on).
# Granted only by superusers (studio) or the owner shell command, never by Administrators
# or other keys (see webinars.mcp_config.DELEGABLE_SCOPES).
PRIVILEGED_SCOPES = (
    ApiKey.Scope.TRUSTED,
    ApiKey.Scope.PUBLICATION, ApiKey.Scope.DELIVERY_ADMIN, ApiKey.Scope.KEYS_ADMIN, ApiKey.Scope.TEAM_ADMIN,
    # Charge configuration and provider credentials are owner-controlled; provisioning
    # creates real external sessions.
    ApiKey.Scope.PAYMENTS_ADMIN, ApiKey.Scope.HOSTING_ADMIN, ApiKey.Scope.HOSTING_PROVISION,
)


class FormErrors(ValidationError):
    """A ValidationError carrying a bound Django form's ``errors`` for re-rendering."""

    def __init__(self, form):
        self.form = form
        super().__init__({k: [str(m) for m in v] for k, v in form.errors.items()})


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def event_form_data(event):
    """EventForm ``data`` for an existing event (same as management._event_form_data)."""
    return {
        "internal_name": event.internal_name,
        "slug": event.slug,
        "public_title": event.public_title,
        "summary": event.summary,
        "hero_image_url": event.hero_image_url,
        "attention": event.attention,
        "interest": event.interest,
        "decision": event.decision,
        "action": event.action,
        "learning_outcomes": json.dumps(list(event.learning_outcomes or [])),
        "hosts": list(event.hosts.values_list("pk", flat=True)),
        "topics": list(event.topics.values_list("pk", flat=True)),
        "visibility": event.visibility,
        "form_version": event.form_version_id,
        "thank_you_mode": event.thank_you_mode,
        "thank_you_url": event.thank_you_url,
        "thank_you_message": event.thank_you_message,
        "register_label": event.register_label,
        "confirm_label": event.confirm_label,
    }


UNCHANGED = object()


def _prepare(data):
    data = dict(data)
    outcomes = data.get("learning_outcomes")
    if isinstance(outcomes, (list, tuple)):
        # EventForm.learning_outcomes is a JSONField form field: it parses JSON text.
        data["learning_outcomes"] = json.dumps([o.strip() for o in outcomes if isinstance(o, str) and o.strip()])
    return data


def save_event(data, *, user=None, api_key=None, instance=None, cover=UNCHANGED):
    """Create (``instance=None``) or update an event. Returns ``(event, changed_fields)``.

    ``data`` uses model field names; ``learning_outcomes`` may be a list of strings and
    ``hosts``/``topics`` lists of primary keys. ``cover`` is a saved ``CoverImage`` to
    attach, ``None`` to remove the uploaded cover, or ``UNCHANGED``. Raises ``FormErrors``.
    """
    data = _prepare(data)
    changed_cover = [] if cover is UNCHANGED else ["cover_image"]
    with transaction.atomic():
        if instance is None:
            form = EventForm(data=data, instance=Event(cover_image=None if cover is UNCHANGED else cover))
            if not form.is_valid():
                raise FormErrors(form)
            event = form.save(commit=False)
            event.revision = 1
            event.save()
            form.save_m2m()
            _revision(event, user, api_key, True, form.changed_data + changed_cover)
            return event, sorted(form.changed_data + changed_cover)
        event = Event.objects.select_for_update().get(pk=instance.pk)
        before = event_snapshot(event)
        if cover is not UNCHANGED:
            event.cover_image = cover
        form = EventForm(data=data, instance=event)
        if not form.is_valid():
            raise FormErrors(form)
        after = event_snapshot(form.instance)
        after["hosts"] = sorted(h.slug for h in form.cleaned_data["hosts"])
        after["topics"] = sorted(t.slug for t in form.cleaned_data["topics"])
        if after == before:
            return event, []
        changed = form.changed_data + (changed_cover if after["coverImageId"] != before["coverImageId"] else [])
        event = form.save(commit=False)
        event.revision += 1
        event.save()
        form.save_m2m()
        _revision(event, user, api_key, False, changed)
        return event, sorted(changed)


def _revision(event, user, api_key, created, changed):
    EventRevision.objects.get_or_create(
        event=event,
        revision=event.revision,
        defaults={"snapshot": event_snapshot(event), "created_by": user if getattr(user, "is_authenticated", False) else None},
    )
    audit(
        "event.created" if created else "event.revised",
        user=user,
        api_key=api_key,
        obj=event,
        revision=event.revision,
        fields=sorted(changed),
    )


def set_visibility(event, visibility, *, user=None, api_key=None):
    if visibility not in Event.Visibility.values:
        raise ValidationError({"visibility": ["Unknown visibility."]})
    data = event_form_data(event)
    data["visibility"] = visibility
    return save_event(data, user=user, api_key=api_key, instance=event)


def publish_problems(event):
    """Human-readable reasons an event is not ready to publish (empty when ready)."""
    problems = []
    if not event.public_title.strip():
        problems.append("Add a public title.")
    if not (event.attention or event.summary).strip():
        problems.append("Write the Attention hook or a summary so the page has an opening.")
    if not event.action.strip():
        problems.append("Write the Action copy: what should visitors do?")
    upcoming = event.occurrences.filter(status=Occurrence.Status.SCHEDULED, starts_at__gt=timezone.now())
    if not upcoming.exists():
        problems.append("Schedule at least one upcoming session.")
    return problems


def delete_draft_event(event, *, user=None):
    with transaction.atomic():
        event = Event.objects.select_for_update().get(pk=event.pk)
        if event.visibility != Event.Visibility.DRAFT:
            raise ValidationError("Only draft events can be deleted. Unpublish it first.")
        if Registration.objects.filter(occurrence__event=event).exists():
            raise ValidationError("Events with registrations cannot be deleted.")
        from webinars.models import TicketOrder

        if TicketOrder.objects.filter(occurrence__event=event).exists():
            # Orders are the financial record (PROTECT): never cascade them away.
            raise ValidationError("Events with ticket orders cannot be deleted.")
        audit("event.deleted", user=user, obj=event, revision=event.revision)
        event.delete()


# ---------------------------------------------------------------------------
# Occurrences (sessions)
# ---------------------------------------------------------------------------


def occurrence_form_data(occ):
    return {
        "event": occ.event_id,
        "series": occ.series_id or "",
        "timezone": occ.timezone,
        "local_start": (
            timezone.make_naive(occ.local_start, dt_timezone.utc) if timezone.is_aware(occ.local_start) else occ.local_start
        ).strftime("%Y-%m-%dT%H:%M"),
        "dst_fold": "" if occ.dst_fold is None else occ.dst_fold,
        "duration_minutes": occ.duration_minutes,
        "registration_open": occ.registration_open,
        "join_url": occ.join_url,
        "waiting_room_url": occ.waiting_room_url,
        "recording_url": occ.recording_url,
    }


def save_occurrence(data, *, event=None, instance=None, user=None, api_key=None):
    """Create or update a session through OccurrenceForm + persist_occurrence (reschedules too)."""
    data = dict(data)
    if instance is not None:
        base = occurrence_form_data(instance)
        base.update(data)
        data = base
        if instance.status == Occurrence.Status.CANCELLED:
            raise ValidationError("Cancelled sessions cannot be changed.")
    else:
        data.setdefault("event", event.pk)
        for key, default in (("series", ""), ("dst_fold", ""), ("duration_minutes", 60), ("registration_open", True),
                             ("join_url", ""), ("waiting_room_url", ""), ("recording_url", "")):
            data.setdefault(key, default)
    form = OccurrenceForm(data=data, instance=instance)
    if not form.is_valid():
        raise FormErrors(form)
    actor = SimpleNamespace(user=user)
    with transaction.atomic():
        occ = form.instance
        if instance is None:
            persist_occurrence(actor, form, occ, change=False)
        elif form.has_changed():
            persist_occurrence(actor, form, occ, change=True)
        if api_key is not None:
            audit("occurrence.api_write", api_key=api_key, obj=occ, created=instance is None)
    return Occurrence.objects.get(pk=occ.pk)


# ---------------------------------------------------------------------------
# Form versions
# ---------------------------------------------------------------------------


def form_is_locked(fv):
    """A version is a published contract once registrations used it or a live event embeds it."""
    if Registration.objects.filter(form_version=fv.version).exists():
        return True
    return fv.events.exclude(visibility=Event.Visibility.DRAFT).exists()


def suggest_version(base):
    stem = re.sub(r"-v\d+$", "", base or "form")[:56] or "form"
    n = 2
    while FormVersion.objects.filter(version=f"{stem}-v{n}").exists():
        n += 1
    return f"{stem}-v{n}"


def save_form_version(rows, meta, *, user=None, instance=None, new_version=None):
    """Save builder rows. Updates ``instance`` in place unless it is locked, in which
    case (or when ``instance`` is None) a new version named ``new_version`` is created.

    ``meta``: description, field_mapping (dict), default_phone_region,
    sms_disclosure_version, sms_disclosure_text, is_active. Returns ``(fv, created)``.
    Raises ``ValidationError`` (dict: builder row index or field name -> message).
    """
    definition = build_definition(rows)
    data = {
        "description": meta.get("description", ""),
        "required_fields": json.dumps(definition["required_fields"]),
        "optional_fields": json.dumps(definition["optional_fields"]),
        "custom_fields": json.dumps(definition["custom_fields"]),
        "field_mapping": json.dumps(meta.get("field_mapping") or {}),
        "default_phone_region": meta.get("default_phone_region", ""),
        "sms_disclosure_version": meta.get("sms_disclosure_version", ""),
        "sms_disclosure_text": meta.get("sms_disclosure_text", ""),
        "is_active": bool(meta.get("is_active", True)),
        "field_layout": json.dumps(definition["field_layout"]),
    }
    if data["sms_disclosure_text"] and not data["sms_disclosure_version"]:
        raise ValidationError({"sms_disclosure_version": "Give the SMS disclosure a version label."})
    with transaction.atomic():
        create = instance is None or form_is_locked(instance)
        if create:
            version = (new_version or "").strip()
            if not VERSION_RE.match(version):
                raise ValidationError({"version": "Use 1-64 letters, digits, dots, dashes or underscores."})
            if FormVersion.objects.filter(version=version).exists():
                raise ValidationError({"version": "That version name is taken."})
            data["version"] = version
            form = FormVersionForm(data=data)
        else:
            instance = FormVersion.objects.select_for_update().get(pk=instance.pk)
            data["version"] = instance.version
            form = FormVersionForm(data=data, instance=instance)
        if not form.is_valid():
            raise FormErrors(form)
        fv = form.save()
        audit("formversion.saved", user=user, obj=fv, created=create)
    return fv, create


# ---------------------------------------------------------------------------
# Destinations (integrations) and API keys — Administrator only at the caller
# ---------------------------------------------------------------------------


def save_destination(data, *, user, instance=None):
    form = DestinationForm(data=data, instance=instance)
    if not form.is_valid():
        raise FormErrors(form)
    with transaction.atomic():
        destination = form.save()
        audit("destination.saved", user=user, obj=destination, created=instance is None, fields=sorted(form.changed_data))
    return destination


def set_signing_secret(destination, *, user, secret=None):
    """Store a pasted secret, or generate one. Returns the generated secret (show once) or None."""
    from webinars.crypto import encrypt_secret, generate_token
    from webinars.management.commands.set_destination_secret import MIN_SECRET_LENGTH

    generated = None
    if secret is None:
        generated = secret = generate_token(32)
    elif len(secret.strip()) < MIN_SECRET_LENGTH:
        raise ValidationError({"signing_secret": f"Signing secrets must be at least {MIN_SECRET_LENGTH} characters."})
    else:
        secret = secret.strip()
    Destination.objects.filter(pk=destination.pk).update(
        signing_secret_encrypted=encrypt_secret(secret), updated_at=timezone.now()
    )
    audit("destination.signing_secret_set", user=user, obj=destination, generated=generated is not None)
    return generated


def set_credentials(destination, credentials, *, user):
    """Replace the encrypted credentials with ``{name: value}`` (empty dict clears them)."""
    from webinars.crypto import encrypt_secret

    if not isinstance(credentials, dict) or not all(
        isinstance(k, str) and k.strip() and isinstance(v, str) for k, v in credentials.items()
    ):
        raise ValidationError({"credentials": "Each credential needs a name and a value."})
    value = encrypt_secret(json.dumps(credentials, sort_keys=True, separators=(",", ":"))) if credentials else ""
    Destination.objects.filter(pk=destination.pk).update(credentials_encrypted=value, updated_at=timezone.now())
    audit("destination.credentials_set", user=user, obj=destination, fields=len(credentials))


def credential_names(destination):
    """Names (never values) of the stored credentials."""
    from webinars.crypto import DecryptionError, decrypt_secret

    if not destination.credentials_encrypted:
        return []
    try:
        return sorted(json.loads(decrypt_secret(destination.credentials_encrypted)).keys())
    except (DecryptionError, ValueError, AttributeError):
        return []


def create_api_key(name, scopes, *, user):
    """Returns ``(ApiKey, raw_key)``. The raw key must be shown once and never stored or logged."""
    from webinars.api.auth import generate_api_key

    name = (name or "").strip()
    if not name or len(name) > 200:
        raise ValidationError({"name": "Give the key a name (up to 200 characters)."})
    if not scopes:
        raise ValidationError({"scopes": "Choose at least one scope."})
    privileged = sorted(set(scopes) & set(PRIVILEGED_SCOPES))
    if privileged and not (user is not None and user.is_active and user.is_superuser):
        # Owner-only: superusers in the studio or `manage.py create_api_key` from a trusted shell.
        raise ValidationError({"scopes": f"Only a superuser can grant: {', '.join(privileged)}."})
    try:
        key, raw = generate_api_key(name, scopes)
    except ValueError as exc:
        raise ValidationError({"scopes": str(exc)})
    audit("apikey.created", user=user, obj=key, scopes=list(key.scopes))
    return key, raw


def revoke_api_key(key, *, user):
    updated = ApiKey.objects.filter(pk=key.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())
    if updated:
        audit("apikey.revoked", user=user, obj=key, prefix=key.prefix)
    return bool(updated)
