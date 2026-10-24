"""Secure first-run bootstrap of the initial administrator.

* Refuses to run when a superuser or Administrator-group member already exists,
  unless ``--force-additional`` is given.
* No default password: the password is read from the environment variable named by
  ``--password-env`` (default ``BOOTSTRAP_ADMIN_PASSWORD``) or, if unset, generated
  with ``secrets.token_urlsafe(18)`` and printed exactly once.
* Password is checked with ``AUTH_PASSWORD_VALIDATORS``.
* The audit entry contains the user id only, never the password or email.
"""
import os
import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from webinars.audit import audit
from webinars.roles import ADMINISTRATOR, apply_roles


class Command(BaseCommand):
    help = "Create the first administrator account (refuses if one exists)."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", required=True)
        parser.add_argument(
            "--password-env",
            default="BOOTSTRAP_ADMIN_PASSWORD",
            help="Name of the environment variable holding the password. If unset, a password is generated and printed once.",
        )
        parser.add_argument(
            "--force-additional",
            action="store_true",
            help="Allow creating another administrator when one already exists.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = (options["username"] or "").strip()
        email = (options["email"] or "").strip()
        if not username:
            raise CommandError("--username must not be empty.")
        if not email or "@" not in email:
            raise CommandError("--email must be a valid email address.")

        admin_exists = (
            User.objects.filter(is_superuser=True).exists()
            or User.objects.filter(groups__name=ADMINISTRATOR).exists()
        )
        if admin_exists and not options["force_additional"]:
            raise CommandError(
                "An administrator already exists. Refusing to bootstrap; use --force-additional to add another."
            )
        if User.objects.filter(**{User.USERNAME_FIELD: username}).exists():
            raise CommandError("A user with that username already exists.")

        env_name = options["password_env"]
        password = os.environ.get(env_name) if env_name else None
        generated = False
        if not password:
            password = secrets.token_urlsafe(18)
            generated = True

        candidate = User(**{User.USERNAME_FIELD: username}, email=email)
        try:
            validate_password(password, user=candidate)
        except ValidationError as exc:
            raise CommandError("Password rejected: " + " ".join(exc.messages))

        with transaction.atomic():
            group = Group.objects.filter(name=ADMINISTRATOR).first()
            if group is None:
                apply_roles()
                group = Group.objects.get(name=ADMINISTRATOR)
            user = User.objects.create_user(**{User.USERNAME_FIELD: username}, email=email, password=password)
            user.is_staff = True
            user.is_superuser = True
            user.save(update_fields=["is_staff", "is_superuser"])
            user.groups.add(group)
            audit(
                "admin.bootstrapped",
                obj=user,
                generated_password=generated,
                additional=bool(admin_exists),
            )

        self.stdout.write(self.style.SUCCESS(f"Administrator '{username}' created."))
        if generated:
            self.stdout.write(
                "Generated password (shown once, store it in your password manager now):\n" + password
            )
        else:
            self.stdout.write(f"Password taken from ${env_name}; unset that variable now.")
