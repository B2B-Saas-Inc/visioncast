"""Management API scopes ``content_write`` and ``attendance_write``.

``ApiKey.scopes`` is a plain JSON list validated in ``webinars.api.auth`` against
``ApiKey.Scope``, so adding the enum members changes no schema. This migration
records the change in the migration history; existing keys are not granted the
new scopes (they must be issued explicitly with ``create_api_key``).
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("webinars", "0002_roles"),
    ]

    operations = [
        migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop),
    ]
