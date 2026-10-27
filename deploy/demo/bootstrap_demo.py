"""Demo droplet bootstrap helpers (run through ``visioncast-manage --bootstrap``).

    preflight                               counts only; fails if any destination exists
    create-admin --username U --email E     first Administrator; password -> stdout
    create-proxy-key --name N               API key with the ``proxy`` scope; raw key -> stdout

The two ``create-*`` actions write exactly one secret to stdout and refuse to run unless
stdout is a regular file: deploy.sh redirects stdout into a new root-only file, so the value
is never displayed. Status messages go to stderr and contain no secret. The secret is
written and flushed inside the database transaction, so a failed write rolls back.

Same model calls as ``manage.py bootstrap_admin`` and ``create_api_key`` (which print
their secrets), plus the same audit entries.
"""
import argparse
import os
import secrets
import stat
import sys

APP_DIR = "/srv/visioncast/app"


def _setup():
    sys.path.insert(0, APP_DIR)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def _emit_secret(value):
    sys.stdout.write(value + "\n")
    sys.stdout.flush()
    os.fsync(sys.stdout.fileno())


def preflight(args):
    from django.contrib.auth import get_user_model

    from webinars.models import ApiKey, Destination

    destinations = Destination.objects.count()
    print(f"destinations: {destinations} (enabled: {Destination.objects.filter(enabled=True).count()})", file=sys.stderr)
    print(f"superusers: {get_user_model().objects.filter(is_superuser=True).count()}", file=sys.stderr)
    print(f"active API keys: {ApiKey.objects.filter(revoked_at__isnull=True).count()}", file=sys.stderr)
    if destinations:
        sys.exit("Refusing: the demo must have no destinations (nothing may be delivered off-host).")


def create_admin(args):
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError
    from django.db import transaction

    from webinars.audit import audit
    from webinars.roles import ADMINISTRATOR, apply_roles

    User = get_user_model()
    if User.objects.filter(is_superuser=True).exists() or User.objects.filter(groups__name=ADMINISTRATOR).exists():
        sys.exit("Refusing: an administrator already exists.")
    if User.objects.filter(**{User.USERNAME_FIELD: args.username}).exists():
        sys.exit("Refusing: a user with that username already exists.")
    password = secrets.token_urlsafe(24)
    candidate = User(**{User.USERNAME_FIELD: args.username}, email=args.email)
    try:
        validate_password(password, user=candidate)
    except ValidationError as exc:
        sys.exit("Generated password rejected: " + " ".join(exc.messages))
    with transaction.atomic():
        group = Group.objects.filter(name=ADMINISTRATOR).first()
        if group is None:
            apply_roles()
            group = Group.objects.get(name=ADMINISTRATOR)
        user = User.objects.create_user(**{User.USERNAME_FIELD: args.username}, email=args.email, password=password)
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=["is_staff", "is_superuser"])
        user.groups.add(group)
        audit("admin.bootstrapped", obj=user, generated_password=True, additional=False)
        _emit_secret(password)
    print(f"Administrator {args.username!r} created; password written to stdout (not displayed).", file=sys.stderr)


def create_proxy_key(args):
    from django.db import transaction

    from webinars.api.auth import generate_api_key
    from webinars.audit import audit
    from webinars.models import ApiKey

    if ApiKey.objects.filter(name=args.name, revoked_at__isnull=True).exists():
        sys.exit(f"Refusing: an active API key named {args.name!r} already exists (revoke it in /admin/ first).")
    with transaction.atomic():
        key, raw = generate_api_key(args.name, [ApiKey.Scope.PROXY.value])
        audit("api_key.created", obj=key, scopes=list(key.scopes))
        _emit_secret(raw)
    print(f"API key id {key.id} prefix {key.prefix} scopes proxy; raw key written to stdout (not displayed).", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="VisionCast demo bootstrap")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("preflight").set_defaults(func=preflight)
    p = sub.add_parser("create-admin")
    p.add_argument("--username", required=True)
    p.add_argument("--email", required=True)
    p.set_defaults(func=create_admin, secret=True)
    p = sub.add_parser("create-proxy-key")
    p.add_argument("--name", required=True)
    p.set_defaults(func=create_proxy_key, secret=True)
    args = parser.parse_args()
    if getattr(args, "secret", False) and not stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode):
        sys.exit("Refusing: stdout must be redirected to a root-only file (never a terminal or pipe).")
    _setup()
    args.func(args)


if __name__ == "__main__":
    main()
