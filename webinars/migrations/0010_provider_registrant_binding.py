import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Bind each ProviderRegistrant to the provider account + event it was made for.

    No backfill on purpose: existing rows keep a blank binding, which never matches the
    current session, so their links are not used until a person reconciles them."""

    dependencies = [
        ("webinars", "0009_capability_mailbox_verified"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerregistrant",
            name="event_account",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="+", to="webinars.hostingaccount"),
        ),
        migrations.AddField(
            model_name="providerregistrant",
            name="event_external_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
