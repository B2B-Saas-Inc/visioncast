"""Create a scoped API key. The raw key is printed exactly once; only its SHA-256 is stored.

    python manage.py create_api_key --name "CMS proxy" --scope proxy
"""
from django.core.management.base import BaseCommand, CommandError

from webinars.api.auth import generate_api_key
from webinars.audit import audit
from webinars.models import ApiKey

VALID_SCOPES = [choice for choice, _ in ApiKey.Scope.choices]


class Command(BaseCommand):
    help = "Create a hashed, scoped API key and print it once."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--scope", action="append", required=True, choices=VALID_SCOPES, dest="scopes")

    def handle(self, *args, **opts):
        name = opts["name"].strip()
        if not name:
            raise CommandError("--name must not be blank")
        try:
            key, raw = generate_api_key(name, opts["scopes"])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        audit("api_key.created", obj=key, scopes=list(key.scopes))
        self.stdout.write(f"API key id: {key.id}  prefix: {key.prefix}  scopes: {','.join(key.scopes)}")
        self.stdout.write("Raw key (shown once; store it in your server-side secret store):")
        self.stdout.write(raw)
