from django.contrib.auth.management import create_permissions
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from webinars.audit import audit
from webinars.roles import apply_roles


class Command(BaseCommand):
    help = "Create or update the Administrator / Editor / Analyst groups and their permissions (idempotent)."

    def handle(self, *args, **options):
        with transaction.atomic():
            for app_config in apps.get_app_configs():
                create_permissions(app_config, verbosity=0)
            result = apply_roles()
            audit("roles.synced", **{f"perms_{k.lower()}": v for k, v in result.items()})
        for group, count in result.items():
            self.stdout.write(f"{group}: {count} permissions")
        self.stdout.write(self.style.SUCCESS("Roles synchronised."))
