import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """Capacity, ticket prices, Stripe orders and hosting-provider links.

    Purely additive: every new column is nullable or has a default that keeps the
    existing free, unlimited behaviour.
    """

    dependencies = [
        ("webinars", "0007_confirmation_use"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField("event", "ticket_price_minor", models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField("event", "ticket_currency", models.CharField(blank=True, max_length=3)),
        migrations.AddConstraint(
            "event",
            models.CheckConstraint(
                condition=Q(ticket_price_minor__isnull=True) | ~Q(ticket_currency=""),
                name="event_price_requires_currency",
            ),
        ),
        migrations.AddField("occurrence", "capacity", models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField("occurrence", "show_remaining", models.BooleanField(default=False)),
        migrations.AddField("occurrence", "ticket_price_minor", models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField("occurrence", "ticket_currency", models.CharField(blank=True, max_length=3)),
        migrations.AddConstraint(
            "occurrence",
            models.CheckConstraint(
                condition=Q(ticket_price_minor__isnull=True) | ~Q(ticket_currency=""),
                name="occurrence_price_requires_currency",
            ),
        ),
        migrations.CreateModel(
            name="PaymentAccount",
            fields=[
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("provider", models.CharField(default="stripe", max_length=16)),
                ("name", models.CharField(default="Stripe", max_length=200)),
                ("mode", models.CharField(choices=[("test", "Test"), ("live", "Live")], default="test", max_length=8)),
                ("secret_key_encrypted", models.TextField(blank=True)),
                ("webhook_secret_encrypted", models.TextField(blank=True)),
                ("expected_account_id", models.CharField(blank=True, max_length=64)),
                ("enabled", models.BooleanField(default=False)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        condition=Q(enabled=True), fields=("enabled",), name="uniq_enabled_payment_account"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="TicketOrder",
            fields=[
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("hold", "Seat held (creating checkout)"),
                            ("checkout_open", "Checkout open"),
                            ("fulfilled", "Paid and registered"),
                            ("expired", "Expired (seat released)"),
                            ("cancelled", "Cancelled before payment"),
                            ("needs_reconciliation", "Needs reconciliation"),
                            ("refund_required", "Paid but not admitted: refund required"),
                            ("refunded", "Refunded (recorded by staff)"),
                        ],
                        db_index=True,
                        default="hold",
                        max_length=24,
                    ),
                ),
                ("mode", models.CharField(choices=[("test", "Test"), ("live", "Live")], max_length=8)),
                ("amount_minor", models.PositiveIntegerField()),
                ("currency", models.CharField(max_length=3)),
                ("price_snapshot", models.JSONField(default=dict)),
                ("idempotency_key", models.CharField(max_length=128, unique=True)),
                ("request_hash", models.CharField(max_length=64)),
                ("capability_hash", models.CharField(max_length=64, unique=True)),
                ("capability_expires_at", models.DateTimeField()),
                ("hold_expires_at", models.DateTimeField(db_index=True)),
                ("checkout_session_id", models.CharField(blank=True, max_length=255, null=True, unique=True)),
                ("checkout_url", models.URLField(blank=True, max_length=2000)),
                ("checkout_expires_at", models.DateTimeField(blank=True, null=True)),
                ("payment_intent_id", models.CharField(blank=True, max_length=255)),
                ("pending_payload", models.JSONField(blank=True, default=dict)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, max_length=300)),
                (
                    "account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="orders",
                        to="webinars.paymentaccount",
                    ),
                ),
                (
                    "occurrence",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="ticket_orders", to="webinars.occurrence"
                    ),
                ),
                (
                    "person",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ticket_orders",
                        to="webinars.person",
                    ),
                ),
                ("email_key", models.CharField(blank=True, db_index=True, max_length=254)),
                ("caller_capability_id", models.UUIDField(blank=True, null=True)),
                (
                    "registration",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ticket_orders",
                        to="webinars.registration",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["occurrence", "status"], name="webinars_ti_occurre_3f1c2a_idx")],
            },
        ),
        migrations.CreateModel(
            name="StripeEventRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.CharField(max_length=255, unique=True)),
                ("event_type", models.CharField(max_length=100)),
                ("livemode", models.BooleanField(default=False)),
                ("outcome", models.CharField(blank=True, max_length=64)),
                ("received_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                (
                    "order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="webinars.ticketorder",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="HostingAccount",
            fields=[
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "provider",
                    models.CharField(
                        choices=[
                            ("zoom", "Zoom Webinars"),
                            ("wistia", "Wistia Webinars"),
                            ("sequel", "Sequel (Introvoke)"),
                            ("riverside", "Riverside (attach existing event)"),
                            ("streamyard", "StreamYard (manual link)"),
                        ],
                        max_length=16,
                    ),
                ),
                ("name", models.CharField(max_length=200)),
                ("config", models.JSONField(blank=True, default=dict)),
                ("credentials_encrypted", models.TextField(blank=True)),
                ("enabled", models.BooleanField(default=False)),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="HostedSession",
            fields=[
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "provider",
                    models.CharField(
                        choices=[
                            ("zoom", "Zoom Webinars"),
                            ("wistia", "Wistia Webinars"),
                            ("sequel", "Sequel (Introvoke)"),
                            ("riverside", "Riverside (attach existing event)"),
                            ("streamyard", "StreamYard (manual link)"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "mode",
                    models.CharField(
                        choices=[
                            ("provisioned", "Created by this platform"),
                            ("attached", "Attached to an existing provider event"),
                            ("manual", "Manual link / embed"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("draft", "Not yet created"),
                            ("in_flight", "Request in flight"),
                            ("linked", "Linked"),
                            ("out_of_date", "Schedule changed; update pending"),
                            ("needs_reconciliation", "Outcome unknown: reconcile before retrying"),
                            ("failed", "Failed (safe to retry)"),
                            ("detached", "Detached (external session left untouched)"),
                        ],
                        default="draft",
                        max_length=24,
                    ),
                ),
                ("external_id", models.CharField(blank=True, max_length=255)),
                ("host_url_encrypted", models.TextField(blank=True)),
                ("embed_url", models.URLField(blank=True, max_length=1000)),
                ("synced_schedule_generation", models.PositiveIntegerField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, max_length=300)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                (
                    "account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="sessions",
                        to="webinars.hostingaccount",
                    ),
                ),
                (
                    "occurrence",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, related_name="hosted_session", to="webinars.occurrence"
                    ),
                ),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="ProvisioningOperation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("create", "Create"),
                            ("update", "Update"),
                            ("attach", "Attach existing"),
                            ("register", "Register attendee"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("in_flight", "In flight"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed (definitive)"),
                            ("unknown", "Unknown outcome"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("idempotency_key", models.CharField(max_length=128, unique=True)),
                ("schedule_generation", models.PositiveIntegerField(blank=True, null=True)),
                ("status_code", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("error", models.CharField(blank=True, max_length=300)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "actor_api_key",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="webinars.apikey",
                    ),
                ),
                (
                    "actor_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="operations", to="webinars.hostedsession"
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="ProviderRegistrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("linked", "Linked"),
                            ("needs_reconciliation", "Needs reconciliation"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=24,
                    ),
                ),
                ("external_id", models.CharField(blank=True, max_length=255)),
                ("join_url_encrypted", models.TextField(blank=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("last_error", models.CharField(blank=True, max_length=300)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "registration",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="provider_registrant",
                        to="webinars.registration",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="registrants", to="webinars.hostedsession"
                    ),
                ),
            ],
        ),
    ]
