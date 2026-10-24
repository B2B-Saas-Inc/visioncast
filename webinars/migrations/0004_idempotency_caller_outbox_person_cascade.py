import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("webinars", "0003_management_api_scopes"),
    ]

    operations = [
        migrations.AddField(
            model_name="idempotencyrecord",
            name="caller_capability_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="outboxmessage",
            name="person",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="webinars.person",
            ),
        ),
    ]
