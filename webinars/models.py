"""Data model for the webinar platform.

All PII lives on Person, Registration.submitted_profile, ConsentRecord evidence,
AttributionTouch, OrganizationExternalId, OutboxMessage.payload and
AttendanceImportRow (email key, masked email, display name). The retention command
(``manage.py purge_personal_data``) must clear every one of them.
"""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------


class Host(TimestampedModel):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=120, unique=True)
    title = models.CharField(max_length=200, blank=True)
    bio = models.TextField(blank=True)
    image_url = models.URLField(max_length=1000, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Topic(TimestampedModel):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=120, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class FormVersion(TimestampedModel):
    """Versioned form schema. ``version`` is the public ``formVersion`` string."""

    version = models.CharField(max_length=64, unique=True)
    description = models.CharField(max_length=300, blank=True)
    # Canonical camelCase keys, e.g. ["email", "firstName", "lastName", "company"].
    required_fields = models.JSONField(default=list, blank=True)
    optional_fields = models.JSONField(default=list, blank=True)
    # Explicit per-form alias -> canonical key mapping, e.g. {"work_email": "email"}.
    field_mapping = models.JSONField(default=dict, blank=True)
    # Allowed custom field keys (stored namespaced as "custom.<key>").
    custom_fields = models.JSONField(default=list, blank=True)
    default_phone_region = models.CharField(
        max_length=2, blank=True, help_text="ISO 3166-1 alpha-2 used only when the form supplies country context."
    )
    sms_disclosure_version = models.CharField(max_length=64, blank=True)
    sms_disclosure_text = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    # Presentation metadata from the studio form builder, in display order:
    # [{"key": "email" | "custom.<key>", "label": str, "help": str, "options": [str, ...]}].
    # Required/optional membership stays in required_fields/optional_fields/custom_fields;
    # see webinars/forms_schema.py for validation and the public schema projection.
    field_layout = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.version


class CoverImage(models.Model):
    """An uploaded event cover, validated and re-encoded by ``webinars.covers``.

    Immutable: a new upload is a new row, so event revisions and webhook snapshots
    that captured its public URL keep resolving. Stored in the database (small
    marketing images only, see ``covers.MAX_UPLOAD_BYTES``) so no media volume is needed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    content_type = models.CharField(max_length=32)
    data = models.BinaryField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    byte_size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

    @property
    def path(self):
        return f"/media/covers/{self.pk}.{self.EXTENSIONS[self.content_type]}"

    @property
    def public_url(self):
        return (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/") + self.path


DEFAULT_REGISTER_LABEL = "Register"
DEFAULT_CONFIRM_LABEL = "Confirm my RSVP"


class Event(TimestampedModel):
    class Visibility(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLIC = "public", "Public"
        UNLISTED = "unlisted", "Unlisted"

    class ThankYouMode(models.TextChoices):
        MESSAGE = "message", "Message"
        REDIRECT = "redirect", "Redirect"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    internal_name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=160, unique=True)
    public_title = models.CharField(max_length=300)
    summary = models.TextField(blank=True)
    hero_image_url = models.URLField(max_length=1000, blank=True)
    # Uploaded alternative to hero_image_url (never both); see ``hero_url``.
    cover_image = models.ForeignKey(CoverImage, null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    attention = models.TextField(blank=True)
    interest = models.TextField(blank=True)
    decision = models.TextField(blank=True)
    action = models.TextField(blank=True)
    learning_outcomes = models.JSONField(default=list, blank=True)
    hosts = models.ManyToManyField(Host, blank=True, related_name="events")
    topics = models.ManyToManyField(Topic, blank=True, related_name="events")
    visibility = models.CharField(max_length=16, choices=Visibility.choices, default=Visibility.DRAFT)
    form_version = models.ForeignKey(FormVersion, on_delete=models.PROTECT, related_name="events")
    thank_you_mode = models.CharField(max_length=16, choices=ThankYouMode.choices, default=ThankYouMode.MESSAGE)
    thank_you_url = models.URLField(max_length=1000, blank=True)
    thank_you_message = models.TextField(blank=True)
    # Registration button text; blank means the default. Plain text only (validation.validate_cta_label).
    register_label = models.CharField(max_length=60, blank=True, help_text=f"Blank uses “{DEFAULT_REGISTER_LABEL}”.")
    confirm_label = models.CharField(
        max_length=60, blank=True,
        help_text=f"Shown when a recognised visitor has nothing left to fill in. Blank uses “{DEFAULT_CONFIRM_LABEL}”.",
    )
    revision = models.PositiveIntegerField(default=1)
    # Event-level ticket price (webinars/payments.py). Null = free (the default); an
    # occurrence may override. Integer minor units (e.g. cents) + ISO 4217 lower-case.
    ticket_price_minor = models.PositiveIntegerField(null=True, blank=True)
    ticket_currency = models.CharField(max_length=3, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ticket_price_minor__isnull=True) | ~Q(ticket_currency=""),
                name="event_price_requires_currency",
            ),
            models.CheckConstraint(
                condition=Q(thank_you_mode="message") | ~Q(thank_you_url=""),
                name="event_redirect_requires_url",
            ),
        ]

    def __str__(self):
        return self.internal_name

    @property
    def hero_url(self):
        """Public cover URL: the uploaded cover when present, otherwise ``hero_image_url``."""
        if self.cover_image_id:
            return self.cover_image.public_url
        return self.hero_image_url

    @property
    def cta(self):
        return {
            "register": self.register_label or DEFAULT_REGISTER_LABEL,
            "confirm": self.confirm_label or DEFAULT_CONFIRM_LABEL,
        }

    def clean(self):
        from webinars.validation import validate_thank_you_url

        if self.thank_you_mode == self.ThankYouMode.REDIRECT:
            if not self.thank_you_url:
                raise ValidationError({"thank_you_url": "Redirect mode requires a URL."})
            validate_thank_you_url(self.thank_you_url)


class EventRevision(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    snapshot = models.JSONField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-revision"]
        constraints = [models.UniqueConstraint(fields=["event", "revision"], name="uniq_event_revision")]


class Series(TimestampedModel):
    """An explicit recurring series: occurrences are listed individually, never expanded from RRULEs."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="series")
    name = models.CharField(max_length=200)
    timezone = models.CharField(max_length=64)

    class Meta:
        verbose_name_plural = "series"

    def __str__(self):
        return self.name


class Occurrence(TimestampedModel):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="occurrences")
    series = models.ForeignKey(Series, null=True, blank=True, on_delete=models.SET_NULL, related_name="occurrences")
    timezone = models.CharField(max_length=64, help_text="IANA timezone, e.g. America/New_York")
    local_start = models.DateTimeField(help_text="Wall-clock start in the occurrence timezone (naive).")
    duration_minutes = models.PositiveIntegerField(default=60)
    # If the wall-clock start is ambiguous (DST fall-back) the editor must choose.
    dst_fold = models.PositiveSmallIntegerField(null=True, blank=True, choices=[(0, "First (DST)"), (1, "Second (standard)")])
    starts_at = models.DateTimeField(db_index=True, help_text="Computed UTC instant.")
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SCHEDULED)
    # Private links: never exposed in public APIs, only in trusted webhook deliveries.
    join_url = models.URLField(max_length=1000, blank=True)
    waiting_room_url = models.URLField(max_length=1000, blank=True)
    recording_url = models.URLField(max_length=1000, blank=True)
    recording_published_at = models.DateTimeField(null=True, blank=True)
    registration_open = models.BooleanField(default=True)
    ics_uid = models.CharField(max_length=255, unique=True)
    ics_sequence = models.PositiveIntegerField(default=0)
    schedule_generation = models.PositiveIntegerField(default=1)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    # ---- Optional hosted waiting room (webinars/waiting_room.py, docs/waiting-room.md)
    class Admission(models.TextChoices):
        AUTO = "auto", "Automatic (opens before start)"
        HOLD = "hold", "Hold (doors closed)"
        OPEN = "open", "Open now"
        ENDED = "ended", "Ended"

    waiting_room_enabled = models.BooleanField(default=False)
    # Stable, attendee-facing room code: /w/<code>/. Never the provider join link.
    waiting_room_code = models.CharField(max_length=32, unique=True, null=True, blank=True)
    admission_mode = models.CharField(max_length=8, choices=Admission.choices, default=Admission.AUTO)
    admission_lead_minutes = models.PositiveSmallIntegerField(default=5)
    # Bumped whenever admission must be re-evaluated (reschedule, cancel, mode change).
    admission_generation = models.PositiveIntegerField(default=1)
    attendance_finalized_at = models.DateTimeField(null=True, blank=True)

    # ---- Capacity and tickets (webinars/capacity.py, webinars/payments.py)
    # Null capacity = unlimited (the default). Counted under this row's lock: confirmed
    # registrations + unexpired/unreconciled ticket holds.
    capacity = models.PositiveIntegerField(null=True, blank=True)
    show_remaining = models.BooleanField(default=False)
    # Null = inherit Event.ticket_price_minor (itself null = free). 0 is not a price:
    # use null for free so "free" has one representation.
    ticket_price_minor = models.PositiveIntegerField(null=True, blank=True)
    ticket_currency = models.CharField(max_length=3, blank=True)

    class Meta:
        ordering = ["starts_at"]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="occurrence_end_after_start"),
            models.CheckConstraint(
                condition=Q(ticket_price_minor__isnull=True) | ~Q(ticket_currency=""),
                name="occurrence_price_requires_currency",
            ),
        ]

    def __str__(self):
        return f"{self.event.internal_name} @ {self.starts_at:%Y-%m-%d %H:%M}Z"

    @property
    def recording_published(self):
        return self.recording_published_at is not None and bool(self.recording_url)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


class Person(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Stored as submitted (trimmed, domain lower-cased); ``email_key`` is the
    # case-folded dedup key. No plus/dot stripping.
    email = models.EmailField(max_length=254, blank=True)
    email_key = models.CharField(max_length=254, null=True, blank=True, unique=True)
    phone = models.CharField(max_length=20, blank=True, help_text="E.164")
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    name = models.CharField(max_length=300, blank=True)
    company = models.CharField(max_length=300, blank=True)
    job_title = models.CharField(max_length=300, blank=True)
    company_size = models.CharField(max_length=64, blank=True)
    custom_fields = models.JSONField(default=dict, blank=True)
    erased_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "people"

    def __str__(self):
        return str(self.id)


class BrowserCapability(models.Model):
    """Opaque browser secret. Only the SHA-256 of the token is stored."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="capabilities")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=64, blank=True)
    # Set only when the capability was issued after an emailed code/link proved the
    # mailbox (waiting_room._complete). Paid checkout requires it (docs/tickets.md).
    mailbox_verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "browser capabilities"


class OrganizationExternalId(models.Model):
    class Source(models.TextChoices):
        BROWSER = "browser", "Browser (unverified)"
        TRUSTED_API = "trusted_api", "Trusted API (verified)"

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="organization_ids")
    namespace = models.CharField(max_length=64)
    value = models.CharField(max_length=256)
    source = models.CharField(max_length=16, choices=Source.choices)
    verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["person", "namespace", "value", "source"], name="uniq_person_org_id"),
            models.CheckConstraint(condition=Q(verified=False) | Q(source="trusted_api"), name="org_id_verified_only_trusted"),
        ]


# --------------------------------------------------------------------------
# Registrations
# --------------------------------------------------------------------------


class Registration(TimestampedModel):
    class Status(models.TextChoices):
        REGISTERED = "registered", "Registered"
        CANCELLED = "cancelled", "Cancelled"

    class Attendance(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        ATTENDED = "attended", "Attended"
        NO_SHOW = "no_show", "No-show"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurrence = models.ForeignKey(Occurrence, on_delete=models.PROTECT, related_name="registrations")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="registrations")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.REGISTERED)
    attendance = models.CharField(max_length=16, choices=Attendance.choices, default=Attendance.UNKNOWN)
    form_version = models.CharField(max_length=64)
    # True when the submission was bound to the person via a valid browser capability
    # (or created the person). False when an unknown browser used an existing email:
    # the profile was NOT updated and the submission is kept only here.
    identity_verified = models.BooleanField(default=False)
    submitted_profile = models.JSONField(default=dict, blank=True)
    custom_fields = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["occurrence", "person"], name="uniq_registration_per_occurrence")]


class IdempotencyRecord(models.Model):
    key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    registration = models.ForeignKey(Registration, null=True, blank=True, on_delete=models.SET_NULL)
    # Id (not a token) of the capability the original request carried, so that caller's
    # retry replays even when it registered someone else. Never used to issue cookies.
    caller_capability_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)


class ConsentRecord(models.Model):
    """Append-only consent evidence. ``server_verified`` is never set from client claims."""

    class Purpose(models.TextChoices):
        SMS = "sms", "SMS"
        ANALYTICS = "analytics", "Analytics"
        ADVERTISING = "advertising", "Advertising"

    class Source(models.TextChoices):
        CLIENT_CLAIM = "client_claim", "Client claim"
        ADMIN = "admin", "Administrator"
        TRUSTED_API = "trusted_api", "Trusted API"

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="consents")
    registration = models.ForeignKey(Registration, null=True, blank=True, on_delete=models.SET_NULL, related_name="consents")
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    granted = models.BooleanField()
    disclosure_version = models.CharField(max_length=64, blank=True)
    disclosure_text_snapshot = models.TextField(blank=True)
    phone_snapshot = models.CharField(max_length=20, blank=True)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.CLIENT_CLAIM)
    server_verified = models.BooleanField(default=False)
    recorded_at = models.DateTimeField(default=timezone.now)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [models.Index(fields=["person", "purpose", "-recorded_at"])]


class AttributionTouch(models.Model):
    """Immutable attribution touch. Never used for authentication."""

    class TouchType(models.TextChoices):
        FIRST_TOUCH = "first_touch", "First touch"
        REGISTRATION_TOUCH = "registration_touch", "Registration touch"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="touches")
    registration = models.ForeignKey(Registration, null=True, blank=True, on_delete=models.CASCADE, related_name="touches")
    touch_type = models.CharField(max_length=24, choices=TouchType.choices)
    parameters = models.JSONField(default=dict, blank=True)
    identifiers = models.JSONField(default=dict, blank=True)
    page_url = models.CharField(max_length=2000, blank=True)
    referrer = models.CharField(max_length=2000, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["person"], condition=Q(touch_type="first_touch"), name="uniq_first_touch_per_person"
            )
        ]


class AttendanceEvent(models.Model):
    class Kind(models.TextChoices):
        ATTENDED = "attended", "Attended"
        NO_SHOW = "no_show", "No-show"
        FOLLOW_UP = "follow_up", "Follow-up sent"
        CHECKED_IN = "checked_in", "Checked in (waiting room)"

    registration = models.ForeignKey(Registration, on_delete=models.CASCADE, related_name="attendance_events")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    occurred_at = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=64, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)


class ReminderSchedule(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ENQUEUED = "enqueued", "Enqueued"
        SKIPPED_LATE = "skipped_late", "Skipped (late)"
        SUPPRESSED = "suppressed", "Suppressed (cancelled)"
        SUPERSEDED = "superseded", "Superseded (rescheduled)"

    registration = models.ForeignKey(Registration, on_delete=models.CASCADE, related_name="reminders")
    offset_minutes = models.PositiveIntegerField()
    send_at = models.DateTimeField(db_index=True)
    generation = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["registration", "offset_minutes", "generation"], name="uniq_reminder_slot")
        ]


# --------------------------------------------------------------------------
# Waiting room
# --------------------------------------------------------------------------


class CheckIn(models.Model):
    """One admission through the hosted waiting room. Unique per registration and occurrence."""

    registration = models.ForeignKey(Registration, on_delete=models.CASCADE, related_name="check_ins")
    occurrence = models.ForeignKey(Occurrence, on_delete=models.CASCADE, related_name="check_ins")
    checked_in_at = models.DateTimeField(default=timezone.now)
    admission_generation = models.PositiveIntegerField()
    # True when the visitor arrived after the scheduled start.
    late = models.BooleanField(default=False)

    class Meta:
        ordering = ["-checked_in_at"]
        constraints = [
            models.UniqueConstraint(fields=["occurrence", "registration"], name="uniq_check_in_per_registration")
        ]


class WaitingRoomVerification(models.Model):
    """Emailed one-time code + link proving mailbox ownership for one occurrence.

    Only hashes are stored: ``token_hash`` (SHA-256 of the 32-byte link token),
    ``code_hash`` (HMAC-SHA256 of the 6-digit code keyed with SECRET_KEY and the row id)
    and ``browser_hash`` (SHA-256 of the pending-browser cookie, so a code can only be
    typed into the browser that requested it). ``email`` is kept only until the row
    expires; housekeeping and erasure delete these rows.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurrence = models.ForeignKey(Occurrence, on_delete=models.CASCADE, related_name="verifications")
    email = models.EmailField(max_length=254)
    email_key = models.CharField(max_length=254, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    code_hash = models.CharField(max_length=64)
    browser_hash = models.CharField(max_length=64, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    # Set only when the message was actually handed to the mail backend (not when suppressed).
    sent = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]


# --------------------------------------------------------------------------
# Team invitations and the preview-only mailbox
# --------------------------------------------------------------------------


class Invitation(models.Model):
    class Role(models.TextChoices):
        ADMINISTRATOR = "Administrator", "Administrator"
        EDITOR = "Editor", "Editor"
        ANALYST = "Analyst", "Analyst"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=254)
    role = models.CharField(max_length=16, choices=Role.choices)
    token_hash = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    email_sent = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    @property
    def status(self):
        if self.accepted_at:
            return "accepted"
        if self.revoked_at:
            return "revoked"
        if self.expires_at <= timezone.now():
            return "expired"
        return "pending"


class PreviewMessage(models.Model):
    """Outgoing mail captured by ``webinars.mail.PreviewMailboxBackend`` (preview/dev only).

    The body is Fernet-encrypted at rest and readable only by administrators in the
    studio mailbox, which is disabled unless that backend is configured.
    """

    to = models.CharField(max_length=254)
    subject = models.CharField(max_length=300)
    body_encrypted = models.TextField()
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]


# --------------------------------------------------------------------------
# Integrations
# --------------------------------------------------------------------------


class Destination(TimestampedModel):
    class Kind(models.TextChoices):
        EMAIL = "email", "Email platform (generic webhook)"
        CRM = "crm", "CRM (generic webhook)"
        GENERIC = "generic", "Generic webhook"

    GROUPS = ["identity", "profile", "organization", "consents", "attribution", "registration", "event"]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.GENERIC)
    url = models.URLField(max_length=1000, help_text="HTTPS only; host must be on WEBHOOK_HOST_ALLOWLIST.")
    enabled = models.BooleanField(default=False)
    event_types = models.JSONField(default=list, blank=True)
    allowed_groups = models.JSONField(default=list, blank=True)
    # Purpose restrictions: consent purposes that must be granted for delivery (e.g. ["advertising"]).
    required_consents = models.JSONField(default=list, blank=True)
    include_private_links = models.BooleanField(default=False)
    field_mapping = models.JSONField(default=dict, blank=True)
    # Fernet-encrypted with INTEGRATION_MASTER_KEY.
    signing_secret_encrypted = models.TextField(blank=True)
    credentials_encrypted = models.TextField(blank=True)
    max_attempts = models.PositiveSmallIntegerField(default=8)

    def __str__(self):
        return self.name


class OutboxMessage(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_FLIGHT = "in_flight", "In flight"
        DELIVERED = "delivered", "Delivered"
        DEAD = "dead", "Dead-lettered"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)  # stable delivery id
    destination = models.ForeignKey(Destination, on_delete=models.CASCADE, related_name="messages")
    event_type = models.CharField(max_length=64)
    payload = models.JSONField()
    # CASCADE: deleting a Person must not leave their PII payloads queued for delivery.
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_owner = models.CharField(max_length=128, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]


class ApiKey(models.Model):
    class Scope(models.TextChoices):
        PUBLIC_READ = "public_read", "Public read"
        REGISTER = "register", "Register"
        PROXY = "proxy", "First-party proxy"
        TRUSTED = "trusted", "Trusted server (verified IDs)"
        REPORTING = "reporting", "Reporting"
        # Management API (webinars/api/management.py, docs/management-api.md).
        CONTENT_WRITE = "content_write", "Content write (events, hosts, topics, forms, occurrences)"
        ATTENDANCE_WRITE = "attendance_write", "Attendance write"
        # Agent configuration (webinars/mcp_config.py, docs/mcp.md). ``scopes`` is a
        # JSONField without choices, so adding scopes needs no migration.
        CONFIG_READ = "config_read", "Configuration read (redacted)"
        PUBLICATION = "publication", "Publication (publish events and recordings; superuser-granted)"
        DELIVERY_ADMIN = "delivery_admin", "Delivery admin (destinations, set-only secrets, retries; superuser-granted)"
        KEYS_ADMIN = "keys_admin", "API key admin (bounded delegation; superuser-granted)"
        TEAM_ADMIN = "team_admin", "Team admin (Editor/Analyst invitations and members; superuser-granted)"
        # Tickets and hosting (docs/tickets.md, docs/hosting.md).
        PAYMENTS_ADMIN = "payments_admin", "Payments admin (Stripe set-only secrets, prices, refunds bookkeeping; superuser-granted)"
        HOSTING_ADMIN = "hosting_admin", "Hosting admin (provider set-only credentials; superuser-granted)"
        HOSTING_PROVISION = "hosting_provision", "Hosting provision (preview/confirm create, update, attach sessions)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    prefix = models.CharField(max_length=16, unique=True)
    key_hash = models.CharField(max_length=64)
    scopes = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "API key"

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"


class ConfirmationUse(models.Model):
    """Burned nonce of a used confirmation token (``webinars.confirmation``). No secrets."""

    nonce = models.CharField(max_length=64, unique=True)
    action = models.CharField(max_length=64)
    api_key = models.ForeignKey(ApiKey, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    used_at = models.DateTimeField(default=timezone.now, db_index=True)


# --------------------------------------------------------------------------
# Tickets (Stripe hosted Checkout; webinars/payments.py, docs/tickets.md)
# --------------------------------------------------------------------------


class PaymentAccount(TimestampedModel):
    """Owner-controlled Stripe configuration. At most one row is ``enabled``.

    Secrets are Fernet-encrypted with INTEGRATION_MASTER_KEY, set-only and never
    rendered. ``mode`` must match the secret key prefix (sk_test_/rk_test_ vs live).
    """

    class Mode(models.TextChoices):
        TEST = "test", "Test"
        LIVE = "live", "Live"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=16, default="stripe")
    name = models.CharField(max_length=200, default="Stripe")
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.TEST)
    secret_key_encrypted = models.TextField(blank=True)
    webhook_secret_encrypted = models.TextField(blank=True)
    # Optional expected Stripe account id (acct_...); when set, webhook events from any
    # other account are rejected.
    expected_account_id = models.CharField(max_length=64, blank=True)
    enabled = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["enabled"], condition=Q(enabled=True), name="uniq_enabled_payment_account"),
        ]

    def __str__(self):
        return f"{self.name} ({self.mode})"


class TicketOrder(TimestampedModel):
    """A seat hold + checkout for one person and occurrence. Immutable price snapshot.

    A Registration is created only by verified-webhook fulfillment (``fulfilled``).
    ``pending_payload`` holds the registration envelope until then (PII: cleared on
    fulfillment, expiry and by purge_personal_data).
    """

    class Status(models.TextChoices):
        HOLD = "hold", "Seat held (creating checkout)"
        CHECKOUT_OPEN = "checkout_open", "Checkout open"
        FULFILLED = "fulfilled", "Paid and registered"
        EXPIRED = "expired", "Expired (seat released)"
        CANCELLED = "cancelled", "Cancelled before payment"
        NEEDS_RECONCILIATION = "needs_reconciliation", "Needs reconciliation"
        REFUND_REQUIRED = "refund_required", "Paid but not admitted: refund required"
        REFUNDED = "refunded", "Refunded (recorded by staff)"

    # Statuses that consume a seat.
    HOLDING = ("hold", "checkout_open", "needs_reconciliation")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurrence = models.ForeignKey(Occurrence, on_delete=models.PROTECT, related_name="ticket_orders")
    # SET_NULL: orders are the financial record and outlive a deleted person.
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="ticket_orders")
    # Case-folded email of the submission (PII, cleared with the envelope): per-person
    # hold reuse and erasure of holds whose person did not exist yet.
    email_key = models.CharField(max_length=254, blank=True, db_index=True)
    # Capability id (not a token) of the browser that created the hold; replays of the
    # same idempotency key from any other browser context are refused.
    caller_capability_id = models.UUIDField(null=True, blank=True)
    account = models.ForeignKey(PaymentAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.HOLD, db_index=True)
    mode = models.CharField(max_length=8, choices=PaymentAccount.Mode.choices)
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    price_snapshot = models.JSONField(default=dict)
    # Registration idempotency key of the intent that created the hold.
    idempotency_key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64)
    # Opaque status capability for the browser (only the SHA-256 is stored).
    capability_hash = models.CharField(max_length=64, unique=True)
    capability_expires_at = models.DateTimeField()
    hold_expires_at = models.DateTimeField(db_index=True)
    checkout_session_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    checkout_url = models.URLField(max_length=2000, blank=True)
    checkout_expires_at = models.DateTimeField(null=True, blank=True)
    payment_intent_id = models.CharField(max_length=255, blank=True)
    pending_payload = models.JSONField(default=dict, blank=True)
    registration = models.ForeignKey(
        Registration, null=True, blank=True, on_delete=models.SET_NULL, related_name="ticket_orders"
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["occurrence", "status"], name="webinars_ti_occurre_3f1c2a_idx")]


class StripeEventRecord(models.Model):
    """Durable dedupe of verified Stripe webhook event ids. No payload or PII stored."""

    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100)
    livemode = models.BooleanField(default=False)
    order = models.ForeignKey(TicketOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    outcome = models.CharField(max_length=64, blank=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)


# --------------------------------------------------------------------------
# Hosting providers (webinars/hosting/, docs/hosting.md)
# --------------------------------------------------------------------------


class HostingAccount(TimestampedModel):
    """Owner-controlled credentials for one hosting provider account (encrypted, set-only)."""

    class Provider(models.TextChoices):
        ZOOM = "zoom", "Zoom Webinars"
        WISTIA = "wistia", "Wistia Webinars"
        SEQUEL = "sequel", "Sequel (Introvoke)"
        RIVERSIDE = "riverside", "Riverside (attach existing event)"
        STREAMYARD = "streamyard", "StreamYard (manual link)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=16, choices=Provider.choices)
    name = models.CharField(max_length=200)
    # Non-secret settings (e.g. Zoom host user id, Sequel company id); validated per adapter.
    config = models.JSONField(default=dict, blank=True)
    credentials_encrypted = models.TextField(blank=True)
    enabled = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} ({self.provider})"


class HostedSession(TimestampedModel):
    """Link between an occurrence and an external session. Never auto-deleted remotely."""

    class Mode(models.TextChoices):
        PROVISIONED = "provisioned", "Created by this platform"
        ATTACHED = "attached", "Attached to an existing provider event"
        MANUAL = "manual", "Manual link / embed"

    class State(models.TextChoices):
        DRAFT = "draft", "Not yet created"
        IN_FLIGHT = "in_flight", "Request in flight"
        LINKED = "linked", "Linked"
        OUT_OF_DATE = "out_of_date", "Schedule changed; update pending"
        NEEDS_RECONCILIATION = "needs_reconciliation", "Outcome unknown: reconcile before retrying"
        FAILED = "failed", "Failed (safe to retry)"
        DETACHED = "detached", "Detached (external session left untouched)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurrence = models.OneToOneField(Occurrence, on_delete=models.CASCADE, related_name="hosted_session")
    account = models.ForeignKey(HostingAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="sessions")
    provider = models.CharField(max_length=16, choices=HostingAccount.Provider.choices)
    mode = models.CharField(max_length=16, choices=Mode.choices)
    state = models.CharField(max_length=24, choices=State.choices, default=State.DRAFT)
    external_id = models.CharField(max_length=255, blank=True)
    # Private host/start URL (encrypted; never public, never in webhooks).
    host_url_encrypted = models.TextField(blank=True)
    # Manual embed (StreamYard): https URL of an allowlisted embed host; private until admission.
    embed_url = models.URLField(max_length=1000, blank=True)
    synced_schedule_generation = models.PositiveIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)


class ProvisioningOperation(models.Model):
    """Durable record of one outbound create/update/attach call."""

    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        ATTACH = "attach", "Attach existing"
        REGISTER = "register", "Register attendee"
        ATTENDANCE = "attendance", "Fetch attendance (read-only)"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_FLIGHT = "in_flight", "In flight"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed (definitive)"
        UNKNOWN = "unknown", "Unknown outcome"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(HostedSession, on_delete=models.CASCADE, related_name="operations")
    action = models.CharField(max_length=16, choices=Action.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    idempotency_key = models.CharField(max_length=128, unique=True)
    schedule_generation = models.PositiveIntegerField(null=True, blank=True)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)
    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_api_key = models.ForeignKey("ApiKey", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class ProviderRegistrant(models.Model):
    """Per-registration provider link (e.g. Riverside unique join link). Private."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        LINKED = "linked", "Linked"
        NEEDS_RECONCILIATION = "needs_reconciliation", "Needs reconciliation"
        FAILED = "failed", "Failed"

    registration = models.OneToOneField(Registration, on_delete=models.CASCADE, related_name="provider_registrant")
    session = models.ForeignKey(HostedSession, on_delete=models.CASCADE, related_name="registrants")
    state = models.CharField(max_length=24, choices=State.choices, default=State.PENDING)
    external_id = models.CharField(max_length=255, blank=True)
    join_url_encrypted = models.TextField(blank=True)
    # Binding: the provider event (and account) this record's attempts and link belong to,
    # captured before any provider call. The session row is reused across detach/re-attach,
    # so the link counts only while this matches the session's current account + event ID.
    # Blank (rows from before this field) never matches: fail closed, reconcile by hand.
    event_account = models.ForeignKey(HostingAccount, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="+")
    event_external_id = models.CharField(max_length=255, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)


# --------------------------------------------------------------------------
# Attendance import (webinars/attendance_import.py, docs/hosting.md)
# --------------------------------------------------------------------------


class AttendanceImport(models.Model):
    """One attendance ingestion run for an occurrence (provider pull or operator file).

    Applying is explicit (preview -> confirm); every applied row goes through
    ``scheduling.record_attendance`` so the usual ``attendance.recorded`` delivery fires
    once per registration. Never stores raw provider payloads or join links."""

    class Source(models.TextChoices):
        ZOOM = "zoom", "Zoom (provider pull)"
        RIVERSIDE = "riverside", "Riverside (provider pull)"
        FILE = "file", "File / rows supplied by an operator"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_FLIGHT = "in_flight", "Provider request in flight"
        NEEDS_REVIEW = "needs_review", "Ready: review and apply"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed (safe to retry)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurrence = models.ForeignKey(Occurrence, on_delete=models.CASCADE, related_name="attendance_imports")
    source = models.CharField(max_length=16, choices=Source.choices)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    # False when the provider list was cut short (page cap): rows may be missing.
    complete = models.BooleanField(default=True)
    row_count = models.PositiveIntegerField(default=0)
    matched_count = models.PositiveIntegerField(default=0)
    unmatched_count = models.PositiveIntegerField(default=0)
    ambiguous_count = models.PositiveIntegerField(default=0)
    ignored_count = models.PositiveIntegerField(default=0)
    applied_count = models.PositiveIntegerField(default=0)
    no_show_count = models.PositiveIntegerField(default=0)
    no_show_refused_count = models.PositiveIntegerField(default=0)
    operation = models.ForeignKey(ProvisioningOperation, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Sanitised step/status strings only (never provider text, never PII).
    last_error = models.CharField(max_length=300, blank=True)
    created_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by_api_key = models.ForeignKey("ApiKey", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    no_shows_marked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class AttendanceImportRow(models.Model):
    """One (deduplicated) participant of an import. Minimum PII: the normalised email key
    (matching), a masked email and a display name (review UI). Purged by
    ``purge_personal_data`` and on person erasure."""

    class Match(models.TextChoices):
        MATCHED = "matched", "Matched"
        UNMATCHED = "unmatched", "Unmatched"
        AMBIGUOUS = "ambiguous", "Ambiguous"
        IGNORED = "ignored", "Ignored"
        RESOLVED = "resolved", "Resolved by a person"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attendance_import = models.ForeignKey(AttendanceImport, on_delete=models.CASCADE, related_name="rows")
    external_id = models.CharField(max_length=255, blank=True)
    email_key = models.CharField(max_length=254, blank=True, db_index=True)
    email_masked = models.CharField(max_length=254, blank=True)
    display_name = models.CharField(max_length=300, blank=True)
    joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    match = models.CharField(max_length=16, choices=Match.choices, default=Match.UNMATCHED)
    registration = models.ForeignKey(Registration, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="attendance_import_rows")
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]


class AuditLog(models.Model):
    """No PII or secrets: only ids, action names and non-sensitive metadata."""

    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    actor_api_key = models.ForeignKey(ApiKey, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
