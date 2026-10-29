"""Marketing admin.

Role-aware: PII (person email/name/phone, submitted profiles, outbox payloads) is
only rendered for users holding ``webinars.view_person`` (Administrator role);
everyone else sees a mask. Encrypted destination secrets are never rendered.
"""
import re
import threading
import uuid
from datetime import timedelta
from datetime import timezone as dt_timezone
from urllib.parse import urlparse

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone

from webinars.audit import audit
from webinars.models import (
    ApiKey,
    AttendanceEvent,
    AttributionTouch,
    AuditLog,
    BrowserCapability,
    ConsentRecord,
    Destination,
    Event,
    EventRevision,
    FormVersion,
    Host,
    Occurrence,
    OrganizationExternalId,
    OutboxMessage,
    Person,
    Registration,
    ReminderSchedule,
    Series,
    Topic,
)

MASK = "•••"

_local = threading.local()

EVENT_TYPES = [
    "registration.created",
    "registration.updated",
    "occurrence.cancelled",
    "occurrence.rescheduled",
    "reminder.due",
    "recording.published",
    "attendance.recorded",
    "attendance.checked_in",
    "follow_up.requested",
]

_CUSTOM_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")


def can_view_pii(request):
    return bool(request.user and request.user.has_perm("webinars.view_person"))


class ReadOnlyAdminMixin:
    """No add/change/delete through the admin; view permission still applies."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def _validate_https(value, field):
    """Use from ``clean_<field>``: raises a plain (non-dict) ValidationError."""
    if not value:
        return
    from webinars.validation import validate_public_https_url

    try:
        validate_public_https_url(value)
    except ValidationError as exc:
        raise ValidationError(exc.messages)


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------


class HostForm(forms.ModelForm):
    class Meta:
        model = Host
        fields = "__all__"

    def clean_image_url(self):
        value = self.cleaned_data.get("image_url", "")
        _validate_https(value, "image_url")
        return value


@admin.register(Host)
class HostAdmin(admin.ModelAdmin):
    form = HostForm
    list_display = ("name", "slug", "title")
    search_fields = ("name", "slug", "title")
    prepopulated_fields = {"slug": ("name",)}

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        audit("host.saved", user=request.user, obj=obj, created=not change)


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        audit("topic.saved", user=request.user, obj=obj, created=not change)


class FormVersionForm(forms.ModelForm):
    class Meta:
        model = FormVersion
        fields = "__all__"

    @staticmethod
    def _canonical():
        from webinars.normalization import CANONICAL_FIELDS

        return list(CANONICAL_FIELDS)

    def _canonical_list(self, name):
        value = self.cleaned_data.get(name)
        if value in (None, ""):
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValidationError("Must be a JSON list of canonical field keys.")
        canonical = self._canonical()
        bad = [v for v in value if v not in canonical]
        if bad:
            raise ValidationError(
                "Unknown field key(s): %(bad)s. Allowed: %(allowed)s.",
                params={"bad": ", ".join(bad), "allowed": ", ".join(canonical)},
            )
        if len(set(value)) != len(value):
            raise ValidationError("Duplicate keys.")
        return value

    def clean_required_fields(self):
        return self._canonical_list("required_fields")

    def clean_optional_fields(self):
        return self._canonical_list("optional_fields")

    def clean_field_mapping(self):
        value = self.cleaned_data.get("field_mapping")
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise ValidationError("Must be a JSON object mapping form keys to canonical keys.")
        canonical = self._canonical()
        for alias, target in value.items():
            if not isinstance(alias, str) or not alias.strip():
                raise ValidationError("Mapping keys must be non-empty strings.")
            if target not in canonical:
                raise ValidationError(
                    "Mapping target %(t)r is not a canonical key. Allowed: %(allowed)s.",
                    params={"t": target, "allowed": ", ".join(canonical)},
                )
        return value

    def clean_custom_fields(self):
        value = self.cleaned_data.get("custom_fields")
        if value in (None, ""):
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValidationError("Must be a JSON list of custom field keys.")
        canonical = self._canonical()
        for key in value:
            if not _CUSTOM_KEY_RE.match(key):
                raise ValidationError("Custom keys must match [a-zA-Z][a-zA-Z0-9_]{0,63}.")
            if key in canonical:
                raise ValidationError("Custom key %(k)r collides with a canonical key.", params={"k": key})
        return value

    def clean_default_phone_region(self):
        value = (self.cleaned_data.get("default_phone_region") or "").strip().upper()
        if value and not re.fullmatch(r"[A-Z]{2}", value):
            raise ValidationError("Use an ISO 3166-1 alpha-2 code, e.g. US.")
        return value

    def clean(self):
        cleaned = super().clean()
        overlap = set(cleaned.get("required_fields") or []) & set(cleaned.get("optional_fields") or [])
        if overlap:
            self.add_error("optional_fields", "Fields cannot be both required and optional: " + ", ".join(sorted(overlap)))
        if "field_layout" in self.fields and cleaned.get("field_layout") and not self.errors:
            from webinars.forms_schema import validate_layout

            try:
                cleaned["field_layout"] = validate_layout(
                    cleaned.get("field_layout"),
                    cleaned.get("required_fields"),
                    cleaned.get("optional_fields"),
                    cleaned.get("custom_fields"),
                )
            except ValidationError as exc:
                self.add_error("field_layout", " ".join(str(m) for m in exc.messages))
        elif "field_layout" in self.fields and not cleaned.get("field_layout"):
            cleaned["field_layout"] = []
        return cleaned


@admin.register(FormVersion)
class FormVersionAdmin(admin.ModelAdmin):
    form = FormVersionForm
    list_display = ("version", "description", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("version", "description")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        audit("formversion.saved", user=request.user, obj=obj, created=not change)


# --------------------------------------------------------------------------
# Occurrences
# --------------------------------------------------------------------------

_TIME_FIELDS = {"local_start", "timezone", "dst_fold", "duration_minutes"}


def _new_ics_uid():
    host = urlparse(getattr(settings, "PUBLIC_BASE_URL", "") or "").hostname or "webinars.invalid"
    return f"{uuid.uuid4()}@{host}"


class OccurrenceForm(forms.ModelForm):
    class Meta:
        model = Occurrence
        fields = (
            "event",
            "series",
            "timezone",
            "local_start",
            "dst_fold",
            "duration_minutes",
            "registration_open",
            "join_url",
            "waiting_room_url",
            "recording_url",
        )

    def clean_join_url(self):
        value = self.cleaned_data.get("join_url", "")
        _validate_https(value, "join_url")
        return value

    def clean_waiting_room_url(self):
        value = self.cleaned_data.get("waiting_room_url", "")
        _validate_https(value, "waiting_room_url")
        return value

    def clean_recording_url(self):
        value = self.cleaned_data.get("recording_url", "")
        _validate_https(value, "recording_url")
        return value

    def clean_timezone(self):
        from webinars.timeutils import validate_timezone

        value = (self.cleaned_data.get("timezone") or "").strip()
        validate_timezone(value)
        return value

    def clean(self):
        from webinars.timeutils import local_to_utc

        cleaned = super().clean()
        self.computed = None
        tz_name = cleaned.get("timezone")
        local_start = cleaned.get("local_start")
        fold = cleaned.get("dst_fold")
        duration = cleaned.get("duration_minutes")
        if fold in ("", None):
            fold = None
        else:
            fold = int(fold)
        if duration is not None and duration < 1:
            self.add_error("duration_minutes", "Duration must be at least one minute.")
            return cleaned
        if not tz_name or local_start is None or duration is None:
            return cleaned
        # With USE_TZ the form returns an aware datetime in the current (UTC) timezone;
        # its wall-clock components are exactly what the editor typed.
        local_naive = timezone.make_naive(local_start, dt_timezone.utc) if timezone.is_aware(local_start) else local_start
        try:
            starts_at = local_to_utc(local_naive, tz_name, fold)
        except ValidationError as exc:
            self.add_error("local_start", exc)
            return cleaned

        instance = self.instance
        is_existing = instance is not None and not instance._state.adding
        if is_existing and _TIME_FIELDS & set(self.changed_data):
            if instance.status == Occurrence.Status.CANCELLED:
                raise ValidationError("Cancelled occurrences cannot be rescheduled.")

        self.computed = {
            "local_naive": local_naive,
            "tz_name": tz_name,
            "fold": fold,
            "duration": duration,
            "starts_at": starts_at,
            "ends_at": starts_at + timedelta(minutes=duration),
        }
        return cleaned


def persist_occurrence(request, form, obj, change):
    """Create or update an occurrence from a validated ``OccurrenceForm``."""
    from webinars.scheduling import reschedule_occurrence

    data = form.computed
    if data is None:  # pragma: no cover - form.is_valid() guarantees this
        raise ValidationError("Occurrence times could not be computed.")
    if not change or obj._state.adding:
        obj.local_start = data["local_naive"].replace(tzinfo=dt_timezone.utc)
        obj.dst_fold = data["fold"]
        obj.starts_at = data["starts_at"]
        obj.ends_at = data["ends_at"]
        if not obj.ics_uid:
            obj.ics_uid = _new_ics_uid()
        obj.save()
        audit("occurrence.created", user=request.user, obj=obj, event_id=str(obj.event_id))
        return

    changed = set(form.changed_data)
    time_changed = changed & _TIME_FIELDS
    others = sorted(changed - _TIME_FIELDS)
    if others:
        obj.save(update_fields=others + ["updated_at"])
    if time_changed:
        reschedule_occurrence(
            obj,
            local_start=data["local_naive"],
            tz_name=data["tz_name"],
            fold=data["fold"],
            duration_minutes=data["duration"],
            user=request.user,
        )
    if changed:
        audit("occurrence.updated", user=request.user, obj=obj, fields=sorted(changed))


def _is_publishing(event):
    """Same transition rule as ``AdminEventForm``: visibility moves to public/unlisted."""
    target = event.visibility
    if not target or target == Event.Visibility.DRAFT:
        return False
    if event._state.adding:
        return True
    return target != Event.objects.filter(pk=event.pk).values_list("visibility", flat=True).first()


class OccurrenceInlineFormSet(forms.BaseInlineFormSet):
    """Re-checks the upcoming-session rule of ``services.publish_problems`` on the sessions
    as submitted, when the parent event is being published.

    ``AdminEventForm`` checks the sessions stored before this submission, so without this
    one POST could publish and move the only upcoming session into the past. The error is
    raised during validation, before anything is saved, so the change form re-renders
    with the submitted values.
    """

    def clean(self):
        super().clean()
        event = self.instance
        if any(self.errors) or not _is_publishing(event):
            return
        now = timezone.now()
        listed = set()
        for form in self.forms:
            occ = form.instance
            if not occ._state.adding:
                listed.add(occ.pk)
            elif not form.has_changed():
                continue  # blank extra row: never saved
            if form in self.deleted_forms or occ.status != Occurrence.Status.SCHEDULED:
                continue
            computed = getattr(form, "computed", None)
            starts_at = computed["starts_at"] if computed else occ.starts_at
            if starts_at is not None and starts_at > now:
                return
        if not event._state.adding and (
            event.occurrences.filter(status=Occurrence.Status.SCHEDULED, starts_at__gt=now).exclude(pk__in=listed).exists()
        ):
            return
        raise ValidationError(
            [
                "The event is not ready to publish.",
                "Schedule at least one upcoming session. These session changes leave none in the future.",
            ]
        )


class OccurrenceInline(admin.TabularInline):
    model = Occurrence
    form = OccurrenceForm
    formset = OccurrenceInlineFormSet
    extra = 0
    show_change_link = True
    fk_name = "event"
    fields = (
        "timezone",
        "local_start",
        "dst_fold",
        "duration_minutes",
        "series",
        "registration_open",
        "starts_at",
        "ends_at",
        "status",
        "ics_uid",
        "ics_sequence",
    )
    readonly_fields = ("starts_at", "ends_at", "status", "ics_uid", "ics_sequence")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Occurrence)
class OccurrenceAdmin(admin.ModelAdmin):
    form = OccurrenceForm
    list_display = ("__str__", "event", "starts_at", "timezone", "status", "registration_open", "recording_is_published")
    list_filter = ("status", "registration_open", "event")
    search_fields = ("event__internal_name", "event__public_title", "ics_uid")
    date_hierarchy = "starts_at"
    list_select_related = ("event",)
    readonly_fields = (
        "starts_at",
        "ends_at",
        "status",
        "ics_uid",
        "ics_sequence",
        "schedule_generation",
        "recording_published_at",
        "cancelled_at",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (None, {"fields": ("event", "series", "status", "registration_open")}),
        (
            "Schedule",
            {
                "fields": ("timezone", "local_start", "dst_fold", "duration_minutes", "starts_at", "ends_at"),
                "description": "Enter the wall-clock start in the occurrence timezone. "
                "For times that occur twice (DST fall-back) choose the fold.",
            },
        ),
        ("Private links (never shown publicly)", {"fields": ("join_url", "waiting_room_url")}),
        ("Recording", {"fields": ("recording_url", "recording_published_at")}),
        (
            "Calendar",
            {"fields": ("ics_uid", "ics_sequence", "schedule_generation", "cancelled_at", "created_at", "updated_at")},
        ),
    )
    actions = ("cancel_selected", "publish_recordings")

    @admin.display(boolean=True, description="Recording published")
    def recording_is_published(self, obj):
        return obj.recording_published

    def save_model(self, request, obj, form, change):
        persist_occurrence(request, form, obj, change)

    @admin.action(description="Cancel selected occurrences (suppresses reminders)", permissions=["change"])
    def cancel_selected(self, request, queryset):
        from webinars.scheduling import cancel_occurrence

        count = 0
        for occ in queryset:
            cancel_occurrence(occ, user=request.user)
            count += 1
        self.message_user(request, f"Cancelled {count} occurrence(s).", messages.SUCCESS)

    @admin.action(description="Publish recording (uses the recording URL)", permissions=["change"])
    def publish_recordings(self, request, queryset):
        from webinars.scheduling import publish_recording

        published = 0
        for occ in queryset:
            if not occ.recording_url:
                self.message_user(request, f"{occ}: set a recording URL first.", messages.WARNING)
                continue
            try:
                publish_recording(occ, occ.recording_url, user=request.user)
                published += 1
            except ValidationError as exc:
                self.message_user(request, f"{occ}: {' '.join(exc.messages)}", messages.ERROR)
        if published:
            self.message_user(request, f"Published {published} recording(s).", messages.SUCCESS)


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        # cover_image is set only by the studio upload (services.save_event(cover=...)).
        # Ticket prices are charge configuration: set only through payments.update_event_price
        # (payments_admin / Administrator), never by an ordinary content edit, which would
        # otherwise clear them (unbound form fields clean to None) and make a paid event free.
        exclude = ("revision", "cover_image", "ticket_price_minor", "ticket_currency")

    def clean_hero_image_url(self):
        value = self.cleaned_data.get("hero_image_url", "")
        _validate_https(value, "hero_image_url")
        if value and self.instance.cover_image_id:
            raise ValidationError("This event has an uploaded cover image. Remove it before using an image URL.")
        return value

    def _clean_label(self, name):
        from webinars.validation import validate_cta_label

        value = (self.cleaned_data.get(name) or "").strip()
        validate_cta_label(value)
        return value

    def clean_register_label(self):
        return self._clean_label("register_label")

    def clean_confirm_label(self):
        return self._clean_label("confirm_label")

    def clean_learning_outcomes(self):
        value = self.cleaned_data.get("learning_outcomes")
        if value in (None, ""):
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValidationError("Must be a JSON list of strings.")
        return value


class AdminEventForm(EventForm):
    """Django admin's event form: publishing runs the same readiness check as the studio,
    REST and MCP (``services.publish_problems``) on the edited candidate.

    Like those paths it applies only when visibility moves to public/unlisted. Sessions
    added in the same submission are not saved yet, so save the event as a draft with a
    session first, then publish. Inline session edits in the publishing submission are
    re-checked by ``OccurrenceInlineFormSet``.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stored_visibility = None if self.instance._state.adding else self.instance.visibility

    def _post_clean(self):
        super()._post_clean()  # self.instance now holds the submitted values
        target = self.cleaned_data.get("visibility")
        if self.errors or not target or target == Event.Visibility.DRAFT or target == self._stored_visibility:
            return
        from webinars.services import publish_problems  # services imports this module

        problems = publish_problems(self.instance)
        if problems:
            self.add_error("visibility", ValidationError(["The event is not ready to publish."] + problems))


def event_snapshot(event):
    return {
        "revision": event.revision,
        "internalName": event.internal_name,
        "slug": event.slug,
        "publicTitle": event.public_title,
        "summary": event.summary,
        "heroImageUrl": event.hero_url,
        "coverImageId": str(event.cover_image_id) if event.cover_image_id else None,
        "attention": event.attention,
        "interest": event.interest,
        "decision": event.decision,
        "action": event.action,
        "learningOutcomes": list(event.learning_outcomes or []),
        "hosts": sorted(event.hosts.values_list("slug", flat=True)),
        "topics": sorted(event.topics.values_list("slug", flat=True)),
        "visibility": event.visibility,
        "formVersion": event.form_version.version if event.form_version_id else None,
        "thankYouMode": event.thank_you_mode,
        "thankYouUrl": event.thank_you_url,
        "thankYouMessage": event.thank_you_message,
        "registerLabel": event.register_label,
        "confirmLabel": event.confirm_label,
    }


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    form = AdminEventForm
    list_display = ("internal_name", "public_title", "slug", "visibility", "revision", "created_at")
    list_filter = ("visibility", "topics", "hosts")
    search_fields = ("internal_name", "public_title", "slug")
    filter_horizontal = ("hosts", "topics")
    prepopulated_fields = {"slug": ("public_title",)}
    readonly_fields = ("revision", "created_at", "updated_at")
    inlines = (OccurrenceInline,)
    fieldsets = (
        (None, {"fields": ("internal_name", "slug", "public_title", "visibility", "form_version", "revision")}),
        ("Content", {"fields": ("summary", "hero_image_url", "attention", "interest", "decision", "action", "learning_outcomes")}),
        ("People and topics", {"fields": ("hosts", "topics")}),
        ("Registration button", {"fields": ("register_label", "confirm_label")}),
        ("Thank-you", {"fields": ("thank_you_mode", "thank_you_message", "thank_you_url")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def save_model(self, request, obj, form, change):
        if change and form.has_changed():
            current = Event.objects.select_for_update().values_list("revision", flat=True).get(pk=obj.pk)
            obj.revision = current + 1
        elif not change:
            obj.revision = 1
        obj.save()

    def save_formset(self, request, form, formset, change):
        if formset.model is not Occurrence:
            return super().save_formset(request, form, formset, change)
        formset.save(commit=False)  # sets the event FK on new instances
        from webinars.models import TicketOrder

        for obj in formset.deleted_objects:
            if TicketOrder.objects.filter(occurrence=obj).exists():
                # Ticket orders are the financial record (PROTECT); keep the session.
                messages.error(request, "A session with ticket orders cannot be deleted; cancel it instead.")
                continue
            obj.delete()
        for inline_form in formset.forms:
            if inline_form in formset.deleted_forms or not inline_form.has_changed():
                continue
            instance = inline_form.instance
            persist_occurrence(request, inline_form, instance, change=not instance._state.adding)
        formset.save_m2m()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if not change or form.has_changed():
            event = form.instance
            EventRevision.objects.get_or_create(
                event=event,
                revision=event.revision,
                defaults={"snapshot": event_snapshot(event), "created_by": request.user},
            )
            audit(
                "event.revised" if change else "event.created",
                user=request.user,
                obj=event,
                revision=event.revision,
                fields=sorted(form.changed_data),
            )


@admin.register(EventRevision)
class EventRevisionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("event", "revision", "created_by", "created_at")
    list_filter = ("event",)
    list_select_related = ("event", "created_by")


@admin.register(Series)
class SeriesAdmin(admin.ModelAdmin):
    list_display = ("name", "event", "timezone")
    search_fields = ("name", "event__internal_name")
    list_select_related = ("event",)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)

        class SeriesForm(form):
            def clean_timezone(self):
                from webinars.timeutils import validate_timezone

                value = (self.cleaned_data.get("timezone") or "").strip()
                validate_timezone(value)
                return value

        return SeriesForm

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        audit("series.saved", user=request.user, obj=obj, created=not change)


# --------------------------------------------------------------------------
# Registrations / people (PII-aware)
# --------------------------------------------------------------------------


@admin.register(Registration)
class RegistrationAdmin(admin.ModelAdmin):
    list_display = ("id", "occurrence", "status", "attendance", "identity_verified", "person_email", "created_at")
    list_filter = ("status", "attendance", "identity_verified", "occurrence__event")
    list_select_related = ("occurrence__event", "person")
    date_hierarchy = "created_at"
    fields = (
        "id",
        "occurrence",
        "status",
        "attendance",
        "form_version",
        "identity_verified",
        "person_ref",
        "person_email",
        "person_name",
        "submitted_profile_display",
        "custom_fields_display",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields
    actions = ("mark_attended", "mark_no_show", "mark_follow_up")

    def get_search_fields(self, request):
        if can_view_pii(request):
            return ("id", "person__email", "person__email_key")
        return ("id",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_record_attendance_permission(self, request):
        return request.user.has_perm("webinars.add_attendanceevent")

    @admin.display(description="Person")
    def person_ref(self, obj):
        return str(obj.person_id)

    @admin.display(description="Email")
    def person_email(self, obj):
        request = getattr(self, "_request", None)
        if request is not None and can_view_pii(request):
            return obj.person.email or "—"
        return MASK

    @admin.display(description="Name")
    def person_name(self, obj):
        request = getattr(self, "_request", None)
        if request is not None and can_view_pii(request):
            p = obj.person
            return " ".join(x for x in (p.first_name, p.last_name) if x) or p.name or "—"
        return MASK

    @admin.display(description="Submitted profile")
    def submitted_profile_display(self, obj):
        request = getattr(self, "_request", None)
        if request is not None and can_view_pii(request):
            return obj.submitted_profile
        return {k: MASK for k in (obj.submitted_profile or {})}

    @admin.display(description="Custom fields")
    def custom_fields_display(self, obj):
        request = getattr(self, "_request", None)
        if request is not None and can_view_pii(request):
            return obj.custom_fields
        return {k: MASK for k in (obj.custom_fields or {})}

    # The display callables need the request to decide on masking. ModelAdmin does not
    # pass it, so keep it in a thread-local for the duration of the view. Template
    # responses are rendered lazily, so render before clearing it.
    @property
    def _request(self):
        return getattr(_local, "request", None)

    def _with_request(self, request, func, *args):
        _local.request = request
        try:
            response = func(request, *args)
            if hasattr(response, "render") and not getattr(response, "is_rendered", True):
                response.render()
            return response
        finally:
            _local.request = None

    def changelist_view(self, request, extra_context=None):
        return self._with_request(request, super().changelist_view, extra_context)

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        return self._with_request(request, super().changeform_view, object_id, form_url, extra_context)

    def _record(self, request, queryset, kind, label):
        from webinars.scheduling import record_attendance

        count = 0
        for registration in queryset.select_related("occurrence"):
            record_attendance(registration, kind, user=request.user, source="admin")
            count += 1
        self.message_user(request, f"Recorded {label} for {count} registration(s).", messages.SUCCESS)

    @admin.action(description="Mark attended", permissions=["record_attendance"])
    def mark_attended(self, request, queryset):
        self._record(request, queryset, AttendanceEvent.Kind.ATTENDED, "attendance")

    @admin.action(description="Mark no-show", permissions=["record_attendance"])
    def mark_no_show(self, request, queryset):
        self._record(request, queryset, AttendanceEvent.Kind.NO_SHOW, "no-show")

    @admin.action(description="Record follow-up sent", permissions=["record_attendance"])
    def mark_follow_up(self, request, queryset):
        self._record(request, queryset, AttendanceEvent.Kind.FOLLOW_UP, "follow-up")


@admin.register(AttendanceEvent)
class AttendanceEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("registration_id", "kind", "occurred_at", "source", "recorded_by")
    list_filter = ("kind", "source")
    fields = ("registration_id", "kind", "occurred_at", "source", "recorded_by")
    readonly_fields = fields


@admin.register(Person)
class PersonAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("id", "email", "first_name", "last_name", "company", "created_at", "erased_at")
    search_fields = ("id", "email", "email_key", "last_name", "company")
    list_filter = ("erased_at",)
    exclude = ("email_key",)
    actions = ("revoke_capabilities",)

    def has_module_permission(self, request):
        return can_view_pii(request)

    def has_view_permission(self, request, obj=None):
        return can_view_pii(request)

    def has_revoke_permission(self, request):
        return request.user.has_perm("webinars.change_person")

    @admin.action(description="Revoke browser capabilities", permissions=["revoke"])
    def revoke_capabilities(self, request, queryset):
        from webinars.identity import revoke_capability

        total = 0
        for person in queryset:
            caps = list(BrowserCapability.objects.filter(person=person, revoked_at__isnull=True))
            for cap in caps:
                revoke_capability(cap, "admin")
            total += len(caps)
            audit("person.capabilities_revoked", user=request.user, obj=person, count=len(caps))
        self.message_user(request, f"Revoked {total} browser capability(ies).", messages.SUCCESS)


@admin.register(BrowserCapability)
class BrowserCapabilityAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("id", "person_id", "created_at", "last_seen_at", "expires_at", "revoked_at", "revoked_reason")
    list_filter = ("revoked_reason",)
    exclude = ("token_hash",)


@admin.register(OrganizationExternalId)
class OrganizationExternalIdAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("person_id", "namespace", "value", "source", "verified", "created_at")
    list_filter = ("source", "verified", "namespace")


@admin.register(ConsentRecord)
class ConsentRecordAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("person_id", "purpose", "granted", "source", "server_verified", "disclosure_version", "recorded_at", "withdrawn_at")
    list_filter = ("purpose", "granted", "source", "server_verified")


@admin.register(AttributionTouch)
class AttributionTouchAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("id", "person_id", "registration_id", "touch_type", "created_at")
    list_filter = ("touch_type",)


@admin.register(ReminderSchedule)
class ReminderScheduleAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("registration_id", "offset_minutes", "send_at", "generation", "status", "processed_at")
    list_filter = ("status", "offset_minutes")


# --------------------------------------------------------------------------
# Integrations
# --------------------------------------------------------------------------


class DestinationForm(forms.ModelForm):
    class Meta:
        model = Destination
        exclude = ("signing_secret_encrypted", "credentials_encrypted")

    def __init__(self, *args, pinned_hosts=(), **kwargs):
        # Native destinations: the URL is derived from the provider and pinned in code, so it
        # passes without an operator allowlist entry (ssrf honours NATIVE_VENDOR_HOSTS_IMPLICIT).
        super().__init__(*args, **kwargs)
        self.pinned_hosts = tuple(pinned_hosts or ())

    def clean_url(self):
        from webinars.ssrf import UnsafeDestination, validate_destination_url

        value = self.cleaned_data.get("url", "")
        try:
            validate_destination_url(value, pinned_hosts=self.pinned_hosts)
        except UnsafeDestination as exc:
            raise ValidationError(str(exc))
        return value

    def _subset(self, name, allowed, label):
        value = self.cleaned_data.get(name)
        if value in (None, ""):
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValidationError(f"Must be a JSON list of {label}.")
        bad = [v for v in value if v not in allowed]
        if bad:
            raise ValidationError(
                "Unknown value(s): %(bad)s. Allowed: %(allowed)s.",
                params={"bad": ", ".join(bad), "allowed": ", ".join(allowed)},
            )
        return list(dict.fromkeys(value))

    def clean_allowed_groups(self):
        return self._subset("allowed_groups", list(Destination.GROUPS), "payload groups")

    def clean_event_types(self):
        return self._subset("event_types", EVENT_TYPES, "event types")

    def clean_required_consents(self):
        return self._subset("required_consents", list(ConsentRecord.Purpose.values), "consent purposes")

    def clean_field_mapping(self):
        value = self.cleaned_data.get("field_mapping")
        if value in (None, ""):
            return {}
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and k and v for k, v in value.items()
        ):
            raise ValidationError("Must be a JSON object of non-empty string -> string.")
        return value


@admin.register(Destination)
class DestinationAdmin(admin.ModelAdmin):
    form = DestinationForm
    list_display = ("name", "kind", "enabled", "has_signing_secret", "max_attempts", "updated_at")
    list_filter = ("kind", "enabled")
    search_fields = ("name",)
    exclude = ("signing_secret_encrypted", "credentials_encrypted")
    readonly_fields = ("has_signing_secret", "has_credentials", "secret_help", "created_at", "updated_at")

    @admin.display(boolean=True, description="Signing secret set")
    def has_signing_secret(self, obj):
        return bool(obj and obj.signing_secret_encrypted)

    @admin.display(boolean=True, description="Credentials set")
    def has_credentials(self, obj):
        return bool(obj and obj.credentials_encrypted)

    @admin.display(description="Secrets")
    def secret_help(self, obj):
        return (
            "Secrets are never shown or entered here. Set them with: "
            "python manage.py set_destination_secret --destination <destination-id> [--generate] "
            "(reads the secret from stdin; --generate prints a new one once)."
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        audit("destination.saved", user=request.user, obj=obj, created=not change, fields=sorted(form.changed_data))

    def delete_model(self, request, obj):
        audit("destination.deleted", user=request.user, obj=obj)
        super().delete_model(request, obj)


@admin.register(OutboxMessage)
class OutboxMessageAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("id", "destination", "event_type", "status", "attempts", "next_attempt_at", "last_status_code", "last_error", "created_at")
    list_filter = ("status", "event_type", "destination")
    list_select_related = ("destination",)
    search_fields = ("id",)
    actions = ("requeue_selected",)
    _base_fields = (
        "id",
        "destination",
        "event_type",
        "status",
        "attempts",
        "next_attempt_at",
        "lease_owner",
        "lease_expires_at",
        "last_status_code",
        "last_error",
        "created_at",
        "delivered_at",
    )

    def get_fields(self, request, obj=None):
        if can_view_pii(request):
            return self._base_fields + ("payload",)
        return self._base_fields

    def get_readonly_fields(self, request, obj=None):
        return self.get_fields(request, obj)

    def has_requeue_permission(self, request):
        return request.user.has_perm("webinars.change_outboxmessage")

    @admin.action(description="Retry selected (requeue)", permissions=["requeue"])
    def requeue_selected(self, request, queryset):
        from webinars.delivery import requeue_message

        requeued = sum(1 for message in queryset if requeue_message(message, user=request.user))
        skipped = queryset.count() - requeued
        self.message_user(request, f"Requeued {requeued} message(s).", messages.SUCCESS)
        if skipped:
            self.message_user(
                request, f"{skipped} message(s) were not dead-lettered or had purged payloads and were skipped.", messages.WARNING
            )


@admin.register(ApiKey)
class ApiKeyAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("name", "prefix", "scopes", "created_at", "last_used_at", "revoked_at")
    list_filter = ("revoked_at",)
    search_fields = ("name", "prefix")
    fields = ("id", "name", "prefix", "scopes", "created_at", "last_used_at", "revoked_at")
    readonly_fields = fields
    actions = ("revoke_keys",)

    def has_revoke_permission(self, request):
        return request.user.has_perm("webinars.change_apikey")

    @admin.action(description="Revoke selected API keys", permissions=["revoke"])
    def revoke_keys(self, request, queryset):
        now = timezone.now()
        count = 0
        with transaction.atomic():
            for key in queryset.filter(revoked_at__isnull=True):
                ApiKey.objects.filter(pk=key.pk, revoked_at__isnull=True).update(revoked_at=now)
                audit("apikey.revoked", user=request.user, obj=key, prefix=key.prefix)
                count += 1
        self.message_user(request, f"Revoked {count} API key(s). Create keys with: python manage.py create_api_key", messages.SUCCESS)


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("created_at", "action", "actor_user", "actor_api_key", "object_type", "object_id")
    list_filter = ("action", "object_type")
    search_fields = ("action", "object_id")
    date_hierarchy = "created_at"
    list_select_related = ("actor_user", "actor_api_key")


# --------------------------------------------------------------------------
# Reports (custom admin URL: /admin/reports/)
# --------------------------------------------------------------------------


def reports_view(request):
    if not request.user.has_perm("webinars.view_reports"):
        raise PermissionDenied
    from webinars.reporting import report_summary

    summary = report_summary()
    context = {
        **admin.site.each_context(request),
        "title": "Reports",
        "summary": summary,
    }
    return TemplateResponse(request, "admin/webinars/reports.html", context)


def _wrap_admin_urls(site):
    original = site.get_urls

    def get_urls():
        return [path("reports/", site.admin_view(reports_view), name="webinars_reports")] + original()

    site.get_urls = get_urls


if not getattr(admin.site, "_webinars_reports_installed", False):
    _wrap_admin_urls(admin.site)
    admin.site._webinars_reports_installed = True
