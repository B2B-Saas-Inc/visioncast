# Hand-written initial migration matching webinars/models.py.
import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.backends.utils import names_digest


def _auto_index_name(table, fields, suffix="idx"):
    """Reproduce ``Index.set_name_with_model`` so the names match models.py exactly.

    Column names equal field names here (no FKs-without-``_id`` in the index list
    except ``person`` which is mapped to ``person_id`` by the caller).
    """
    cols_with_order = list(fields)
    cols = [c.lstrip("-") for c in cols_with_order]
    return "%s_%s_%s" % (
        table[:11],
        cols[0][:7],
        "%s_%s" % (names_digest(table, *cols_with_order, suffix, length=6), suffix),
    )


CONSENT_INDEX_NAME = _auto_index_name("webinars_consentrecord", ["person_id", "purpose", "-recorded_at"])
OUTBOX_INDEX_NAME = _auto_index_name("webinars_outboxmessage", ["status", "next_attempt_at"])


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ------------------------------------------------------------------ catalogue
        migrations.CreateModel(
            name="Host",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200)),
                ("slug", models.SlugField(max_length=120, unique=True)),
                ("title", models.CharField(blank=True, max_length=200)),
                ("bio", models.TextField(blank=True)),
                ("image_url", models.URLField(blank=True, max_length=1000)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="Topic",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200)),
                ("slug", models.SlugField(max_length=120, unique=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="FormVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("version", models.CharField(max_length=64, unique=True)),
                ("description", models.CharField(blank=True, max_length=300)),
                ("required_fields", models.JSONField(blank=True, default=list)),
                ("optional_fields", models.JSONField(blank=True, default=list)),
                ("field_mapping", models.JSONField(blank=True, default=dict)),
                ("custom_fields", models.JSONField(blank=True, default=list)),
                (
                    "default_phone_region",
                    models.CharField(
                        blank=True,
                        help_text="ISO 3166-1 alpha-2 used only when the form supplies country context.",
                        max_length=2,
                    ),
                ),
                ("sms_disclosure_version", models.CharField(blank=True, max_length=64)),
                ("sms_disclosure_text", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="Event",
            fields=[
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("internal_name", models.CharField(max_length=200)),
                ("slug", models.SlugField(max_length=160, unique=True)),
                ("public_title", models.CharField(max_length=300)),
                ("summary", models.TextField(blank=True)),
                ("hero_image_url", models.URLField(blank=True, max_length=1000)),
                ("attention", models.TextField(blank=True)),
                ("interest", models.TextField(blank=True)),
                ("decision", models.TextField(blank=True)),
                ("action", models.TextField(blank=True)),
                ("learning_outcomes", models.JSONField(blank=True, default=list)),
                (
                    "visibility",
                    models.CharField(
                        choices=[("draft", "Draft"), ("public", "Public"), ("unlisted", "Unlisted")],
                        default="draft",
                        max_length=16,
                    ),
                ),
                (
                    "thank_you_mode",
                    models.CharField(
                        choices=[("message", "Message"), ("redirect", "Redirect")],
                        default="message",
                        max_length=16,
                    ),
                ),
                ("thank_you_url", models.URLField(blank=True, max_length=1000)),
                ("thank_you_message", models.TextField(blank=True)),
                ("revision", models.PositiveIntegerField(default=1)),
                (
                    "form_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="webinars.formversion",
                    ),
                ),
                ("hosts", models.ManyToManyField(blank=True, related_name="events", to="webinars.host")),
                ("topics", models.ManyToManyField(blank=True, related_name="events", to="webinars.topic")),
            ],
            options={
                "ordering": ["-created_at"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(thank_you_mode="message") | ~models.Q(thank_you_url=""),
                        name="event_redirect_requires_url",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="EventRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("revision", models.PositiveIntegerField()),
                ("snapshot", models.JSONField()),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="revisions",
                        to="webinars.event",
                    ),
                ),
            ],
            options={
                "ordering": ["-revision"],
                "constraints": [models.UniqueConstraint(fields=("event", "revision"), name="uniq_event_revision")],
            },
        ),
        migrations.CreateModel(
            name="Series",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200)),
                ("timezone", models.CharField(max_length=64)),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="series",
                        to="webinars.event",
                    ),
                ),
            ],
            options={"verbose_name_plural": "series"},
        ),
        migrations.CreateModel(
            name="Occurrence",
            fields=[
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("timezone", models.CharField(help_text="IANA timezone, e.g. America/New_York", max_length=64)),
                (
                    "local_start",
                    models.DateTimeField(help_text="Wall-clock start in the occurrence timezone (naive)."),
                ),
                ("duration_minutes", models.PositiveIntegerField(default=60)),
                (
                    "dst_fold",
                    models.PositiveSmallIntegerField(
                        blank=True,
                        choices=[(0, "First (DST)"), (1, "Second (standard)")],
                        null=True,
                    ),
                ),
                ("starts_at", models.DateTimeField(db_index=True, help_text="Computed UTC instant.")),
                ("ends_at", models.DateTimeField()),
                (
                    "status",
                    models.CharField(
                        choices=[("scheduled", "Scheduled"), ("cancelled", "Cancelled"), ("completed", "Completed")],
                        default="scheduled",
                        max_length=16,
                    ),
                ),
                ("join_url", models.URLField(blank=True, max_length=1000)),
                ("waiting_room_url", models.URLField(blank=True, max_length=1000)),
                ("recording_url", models.URLField(blank=True, max_length=1000)),
                ("recording_published_at", models.DateTimeField(blank=True, null=True)),
                ("registration_open", models.BooleanField(default=True)),
                ("ics_uid", models.CharField(max_length=255, unique=True)),
                ("ics_sequence", models.PositiveIntegerField(default=0)),
                ("schedule_generation", models.PositiveIntegerField(default=1)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="occurrences",
                        to="webinars.event",
                    ),
                ),
                (
                    "series",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="occurrences",
                        to="webinars.series",
                    ),
                ),
            ],
            options={
                "ordering": ["starts_at"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(ends_at__gt=models.F("starts_at")),
                        name="occurrence_end_after_start",
                    ),
                ],
            },
        ),
        # ------------------------------------------------------------------ identity
        migrations.CreateModel(
            name="Person",
            fields=[
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("email_key", models.CharField(blank=True, max_length=254, null=True, unique=True)),
                ("phone", models.CharField(blank=True, help_text="E.164", max_length=20)),
                ("first_name", models.CharField(blank=True, max_length=150)),
                ("last_name", models.CharField(blank=True, max_length=150)),
                ("name", models.CharField(blank=True, max_length=300)),
                ("company", models.CharField(blank=True, max_length=300)),
                ("job_title", models.CharField(blank=True, max_length=300)),
                ("company_size", models.CharField(blank=True, max_length=64)),
                ("custom_fields", models.JSONField(blank=True, default=dict)),
                ("erased_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"verbose_name_plural": "people"},
        ),
        migrations.CreateModel(
            name="BrowserCapability",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_reason", models.CharField(blank=True, max_length=64)),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="capabilities",
                        to="webinars.person",
                    ),
                ),
            ],
            options={"verbose_name_plural": "browser capabilities"},
        ),
        migrations.CreateModel(
            name="OrganizationExternalId",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("namespace", models.CharField(max_length=64)),
                ("value", models.CharField(max_length=256)),
                (
                    "source",
                    models.CharField(
                        choices=[("browser", "Browser (unverified)"), ("trusted_api", "Trusted API (verified)")],
                        max_length=16,
                    ),
                ),
                ("verified", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="organization_ids",
                        to="webinars.person",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("person", "namespace", "value", "source"), name="uniq_person_org_id"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(verified=False) | models.Q(source="trusted_api"),
                        name="org_id_verified_only_trusted",
                    ),
                ],
            },
        ),
        # ------------------------------------------------------------------ registrations
        migrations.CreateModel(
            name="Registration",
            fields=[
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "status",
                    models.CharField(
                        choices=[("registered", "Registered"), ("cancelled", "Cancelled")],
                        default="registered",
                        max_length=16,
                    ),
                ),
                (
                    "attendance",
                    models.CharField(
                        choices=[("unknown", "Unknown"), ("attended", "Attended"), ("no_show", "No-show")],
                        default="unknown",
                        max_length=16,
                    ),
                ),
                ("form_version", models.CharField(max_length=64)),
                ("identity_verified", models.BooleanField(default=False)),
                ("submitted_profile", models.JSONField(blank=True, default=dict)),
                ("custom_fields", models.JSONField(blank=True, default=dict)),
                (
                    "occurrence",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="registrations",
                        to="webinars.occurrence",
                    ),
                ),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="registrations",
                        to="webinars.person",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "constraints": [
                    models.UniqueConstraint(fields=("occurrence", "person"), name="uniq_registration_per_occurrence")
                ],
            },
        ),
        migrations.CreateModel(
            name="IdempotencyRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=128, unique=True)),
                ("request_hash", models.CharField(max_length=64)),
                ("status_code", models.PositiveSmallIntegerField()),
                ("response_body", models.JSONField()),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                (
                    "registration",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="webinars.registration",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ConsentRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "purpose",
                    models.CharField(
                        choices=[("sms", "SMS"), ("analytics", "Analytics"), ("advertising", "Advertising")],
                        max_length=16,
                    ),
                ),
                ("granted", models.BooleanField()),
                ("disclosure_version", models.CharField(blank=True, max_length=64)),
                ("disclosure_text_snapshot", models.TextField(blank=True)),
                ("phone_snapshot", models.CharField(blank=True, max_length=20)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("client_claim", "Client claim"),
                            ("admin", "Administrator"),
                            ("trusted_api", "Trusted API"),
                        ],
                        default="client_claim",
                        max_length=16,
                    ),
                ),
                ("server_verified", models.BooleanField(default=False)),
                ("recorded_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("withdrawn_at", models.DateTimeField(blank=True, null=True)),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="consents",
                        to="webinars.person",
                    ),
                ),
                (
                    "registration",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="consents",
                        to="webinars.registration",
                    ),
                ),
            ],
            options={
                "ordering": ["-recorded_at"],
                "indexes": [
                    models.Index(fields=["person", "purpose", "-recorded_at"], name=CONSENT_INDEX_NAME),
                ],
            },
        ),
        migrations.CreateModel(
            name="AttributionTouch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "touch_type",
                    models.CharField(
                        choices=[("first_touch", "First touch"), ("registration_touch", "Registration touch")],
                        max_length=24,
                    ),
                ),
                ("parameters", models.JSONField(blank=True, default=dict)),
                ("identifiers", models.JSONField(blank=True, default=dict)),
                ("page_url", models.CharField(blank=True, max_length=2000)),
                ("referrer", models.CharField(blank=True, max_length=2000)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="touches",
                        to="webinars.person",
                    ),
                ),
                (
                    "registration",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="touches",
                        to="webinars.registration",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(touch_type="first_touch"),
                        fields=("person",),
                        name="uniq_first_touch_per_person",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="AttendanceEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "kind",
                    models.CharField(
                        choices=[("attended", "Attended"), ("no_show", "No-show"), ("follow_up", "Follow-up sent")],
                        max_length=16,
                    ),
                ),
                ("occurred_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("source", models.CharField(blank=True, max_length=64)),
                (
                    "recorded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "registration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attendance_events",
                        to="webinars.registration",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ReminderSchedule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("offset_minutes", models.PositiveIntegerField()),
                ("send_at", models.DateTimeField(db_index=True)),
                ("generation", models.PositiveIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("enqueued", "Enqueued"),
                            ("skipped_late", "Skipped (late)"),
                            ("suppressed", "Suppressed (cancelled)"),
                            ("superseded", "Superseded (rescheduled)"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "registration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reminders",
                        to="webinars.registration",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("registration", "offset_minutes", "generation"), name="uniq_reminder_slot"
                    )
                ],
            },
        ),
        # ------------------------------------------------------------------ integrations
        migrations.CreateModel(
            name="Destination",
            fields=[
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("email", "Email platform (generic webhook)"),
                            ("crm", "CRM (generic webhook)"),
                            ("generic", "Generic webhook"),
                        ],
                        default="generic",
                        max_length=16,
                    ),
                ),
                (
                    "url",
                    models.URLField(
                        help_text="HTTPS only; host must be on WEBHOOK_HOST_ALLOWLIST.", max_length=1000
                    ),
                ),
                ("enabled", models.BooleanField(default=False)),
                ("event_types", models.JSONField(blank=True, default=list)),
                ("allowed_groups", models.JSONField(blank=True, default=list)),
                ("required_consents", models.JSONField(blank=True, default=list)),
                ("include_private_links", models.BooleanField(default=False)),
                ("field_mapping", models.JSONField(blank=True, default=dict)),
                ("signing_secret_encrypted", models.TextField(blank=True)),
                ("credentials_encrypted", models.TextField(blank=True)),
                ("max_attempts", models.PositiveSmallIntegerField(default=8)),
            ],
        ),
        migrations.CreateModel(
            name="OutboxMessage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("event_type", models.CharField(max_length=64)),
                ("payload", models.JSONField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("in_flight", "In flight"),
                            ("delivered", "Delivered"),
                            ("dead", "Dead-lettered"),
                            ("cancelled", "Cancelled"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("lease_owner", models.CharField(blank=True, max_length=128)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                ("last_status_code", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                (
                    "destination",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="webinars.destination",
                    ),
                ),
                (
                    "person",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="webinars.person",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["status", "next_attempt_at"], name=OUTBOX_INDEX_NAME)],
            },
        ),
        migrations.CreateModel(
            name="ApiKey",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                ("prefix", models.CharField(max_length=16, unique=True)),
                ("key_hash", models.CharField(max_length=64)),
                ("scopes", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"verbose_name": "API key"},
        ),
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=64)),
                ("object_type", models.CharField(blank=True, max_length=64)),
                ("object_id", models.CharField(blank=True, max_length=64)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                (
                    "actor_api_key",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="webinars.apikey",
                    ),
                ),
                (
                    "actor_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
