"""Store an encrypted webhook signing secret and/or credentials for a Destination.

    echo -n "$SECRET" | python manage.py set_destination_secret --destination <uuid>
    python manage.py set_destination_secret --destination <uuid> --generate   # prints new secret once
    python manage.py set_destination_secret --destination <uuid> --credentials-stdin < creds.json

Secrets are read from stdin (never from argv, which leaks via process lists and
shell history) and encrypted with INTEGRATION_MASTER_KEY. Nothing secret is logged
or audited.
"""
import json
import sys
import uuid

from django.core.management.base import BaseCommand, CommandError

from webinars.audit import audit
from webinars.crypto import encrypt_secret, generate_token
from webinars.models import Destination

MIN_SECRET_LENGTH = 24


class Command(BaseCommand):
    help = "Set a destination's webhook signing secret (stdin or --generate) or credentials JSON (--credentials-stdin)."

    def add_arguments(self, parser):
        parser.add_argument("--destination", required=True, help="Destination UUID")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--generate", action="store_true", help="Generate a random signing secret and print it once.")
        group.add_argument(
            "--credentials-stdin", action="store_true", help="Read a JSON object of credentials from stdin."
        )

    def handle(self, *args, **opts):
        try:
            dest_id = uuid.UUID(str(opts["destination"]))
        except ValueError:
            raise CommandError("--destination must be a UUID") from None
        try:
            destination = Destination.objects.get(pk=dest_id)
        except Destination.DoesNotExist:
            raise CommandError("Destination not found") from None

        if opts["credentials_stdin"]:
            raw = sys.stdin.read()
            try:
                creds = json.loads(raw)
            except json.JSONDecodeError:
                raise CommandError("Credentials must be a JSON object") from None
            if not isinstance(creds, dict) or not creds:
                raise CommandError("Credentials must be a non-empty JSON object")
            destination.credentials_encrypted = encrypt_secret(json.dumps(creds, sort_keys=True, separators=(",", ":")))
            destination.save(update_fields=["credentials_encrypted", "updated_at"])
            audit("destination.credentials_set", obj=destination, fields=len(creds))
            self.stdout.write("Credentials stored (encrypted).")
            return

        if opts["generate"]:
            secret = generate_token(32)
        else:
            if sys.stdin.isatty():
                self.stderr.write("Paste the signing secret, then press Ctrl-D:")
            secret = sys.stdin.read().strip()
            if len(secret) < MIN_SECRET_LENGTH:
                raise CommandError(f"Signing secret must be at least {MIN_SECRET_LENGTH} characters")

        destination.signing_secret_encrypted = encrypt_secret(secret)
        destination.save(update_fields=["signing_secret_encrypted", "updated_at"])
        audit("destination.signing_secret_set", obj=destination, generated=bool(opts["generate"]))
        if opts["generate"]:
            self.stdout.write("Signing secret (shown once, store it in the receiver now):")
            self.stdout.write(secret)
        else:
            self.stdout.write("Signing secret stored (encrypted).")
