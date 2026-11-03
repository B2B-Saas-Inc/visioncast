import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webinars", "0015_hosting_auto_provision"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="card_image_url",
            field=models.URLField(blank=True, max_length=1000),
        ),
        migrations.AddField(
            model_name="event",
            name="card_image",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="card_events", to="webinars.coverimage",
            ),
        ),
    ]
