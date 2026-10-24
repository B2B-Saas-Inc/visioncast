import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("webinars", "0006_event_cta_cover_image"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConfirmationUse",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("nonce", models.CharField(max_length=64, unique=True)),
                ("action", models.CharField(max_length=64)),
                ("used_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                (
                    "api_key",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="webinars.apikey",
                    ),
                ),
            ],
        ),
    ]
