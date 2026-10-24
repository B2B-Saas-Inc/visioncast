import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("webinars", "0005_studio_waiting_room_invitations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CoverImage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("content_type", models.CharField(max_length=32)),
                ("data", models.BinaryField()),
                ("width", models.PositiveIntegerField()),
                ("height", models.PositiveIntegerField()),
                ("byte_size", models.PositiveIntegerField()),
                ("sha256", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="event",
            name="cover_image",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="events",
                to="webinars.coverimage",
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="register_label",
            field=models.CharField(blank=True, help_text="Blank uses “Register”.", max_length=60),
        ),
        migrations.AddField(
            model_name="event",
            name="confirm_label",
            field=models.CharField(
                blank=True,
                help_text="Shown when a recognised visitor has nothing left to fill in. Blank uses “Confirm my RSVP”.",
                max_length=60,
            ),
        ),
    ]
