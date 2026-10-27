from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webinars", "0008_tickets_capacity_hosting"),
    ]

    operations = [
        migrations.AddField(
            model_name="browsercapability",
            name="mailbox_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
