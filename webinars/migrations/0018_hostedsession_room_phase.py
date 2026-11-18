from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("webinars", "0017_studio_preference"),
    ]

    operations = [
        migrations.AddField(
            model_name="hostedsession",
            name="room_phase",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="hostedsession",
            name="room_phase_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="hostedsession",
            name="room_phase_next_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="hostedsession",
            name="room_phase_attempts",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="hostedsession",
            name="room_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
