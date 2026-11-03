from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webinars", "0014_studio_brand_font"),
    ]

    operations = [
        migrations.AddField(
            model_name="hostingaccount",
            name="auto_provision",
            field=models.BooleanField(default=False),
        ),
    ]
