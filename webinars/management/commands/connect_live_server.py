"""Connects this studio to a live server, for scripted installs (deploy/quickstart/install.sh).

The service secret is read from the environment variable named by ``--secret-env``, never from the
command line, so it doesn't end up in shell history or process lists. Repeating it updates the account.
"""
import os

from django.core.management.base import BaseCommand, CommandError

from webinars.hosting import service
from webinars.models import HostingAccount


class Command(BaseCommand):
    help = "Create or update the hosting account for a self-hosted live server."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="Live")
        parser.add_argument("--host", required=True, help="The service host, like live.example.com")
        parser.add_argument("--secret-env", default="LIVE_SERVICE_SECRET")

    def handle(self, *args, **options):
        secret = os.environ.get(options["secret_env"], "")
        if not secret:
            raise CommandError(f"Set {options['secret_env']} first.")
        existing = HostingAccount.objects.filter(provider="livekit", config__service_host=options["host"]).first()
        account, _ = service.save_account(
            existing, provider="livekit", name=existing.name if existing else options["name"],
            config={**(existing.config if existing else {}), "service_host": options["host"]},
            posted_secrets={"service_secret": secret}, enabled=existing.enabled if existing else True)
        self.stdout.write(f"Connected {options['host']} as hosting account {account.pk}.")
