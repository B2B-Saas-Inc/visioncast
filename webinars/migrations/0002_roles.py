"""Create the Administrator / Editor / Analyst groups (see webinars/roles.py)."""
from django.db import migrations


def forwards(apps, schema_editor):
    from django.contrib.auth.management import create_permissions

    # Default model permissions are normally created in post_migrate, i.e. after
    # all migrations; create them now so the groups can reference them.
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, verbosity=0, apps=apps)
        app_config.models_module = None

    from webinars.roles import apply_roles

    apply_roles(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("webinars", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
