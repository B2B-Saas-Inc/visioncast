"""Studio form layout, hosted waiting room, check-ins, verifications, invitations, preview mailbox.

Also re-applies the role groups so the new models' permissions are granted
(see webinars/roles.py).
"""
import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def apply_roles_forwards(apps, schema_editor):
    from django.contrib.auth.management import create_permissions

    # Same approach as 0002_roles: permissions are normally created post_migrate.
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, verbosity=0, apps=apps)
        app_config.models_module = None

    from webinars.roles import apply_roles

    apply_roles(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("webinars", "0004_idempotency_caller_outbox_person_cascade"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="formversion",
            name="field_layout",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="waiting_room_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="waiting_room_code",
            field=models.CharField(blank=True, max_length=32, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="admission_mode",
            field=models.CharField(
                choices=[
                    ("auto", "Automatic (opens before start)"),
                    ("hold", "Hold (doors closed)"),
                    ("open", "Open now"),
                    ("ended", "Ended"),
                ],
                default="auto",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="admission_lead_minutes",
            field=models.PositiveSmallIntegerField(default=5),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="admission_generation",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="attendance_finalized_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="attendanceevent",
            name="kind",
            field=models.CharField(
                choices=[
                    ("attended", "Attended"),
                    ("no_show", "No-show"),
                    ("follow_up", "Follow-up sent"),
                    ("checked_in", "Checked in (waiting room)"),
                ],
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="CheckIn",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("checked_in_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("admission_generation", models.PositiveIntegerField()),
                ("late", models.BooleanField(default=False)),
                (
                    "occurrence",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="check_ins",
                        to="webinars.occurrence",
                    ),
                ),
                (
                    "registration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="check_ins",
                        to="webinars.registration",
                    ),
                ),
            ],
            options={
                "ordering": ["-checked_in_at"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("occurrence", "registration"), name="uniq_check_in_per_registration"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="WaitingRoomVerification",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("email", models.EmailField(max_length=254)),
                ("email_key", models.CharField(db_index=True, max_length=254)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("code_hash", models.CharField(max_length=64)),
                ("browser_hash", models.CharField(db_index=True, max_length=64)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("sent", models.BooleanField(default=False)),
                (
                    "occurrence",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="verifications",
                        to="webinars.occurrence",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="Invitation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("email", models.EmailField(max_length=254)),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("Administrator", "Administrator"),
                            ("Editor", "Editor"),
                            ("Analyst", "Analyst"),
                        ],
                        max_length=16,
                    ),
                ),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField()),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("email_sent", models.BooleanField(default=False)),
                (
                    "accepted_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "invited_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="PreviewMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("to", models.CharField(max_length=254)),
                ("subject", models.CharField(max_length=300)),
                ("body_encrypted", models.TextField()),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.RunPython(apply_roles_forwards, migrations.RunPython.noop),
    ]
