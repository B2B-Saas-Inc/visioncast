"""The VisionCast studio for the local preview, wired to the local visioncast-live service.

Preview only, nothing here ships. Production talks to the service over HTTPS at a public host, so
this swaps the outbound transport and link check for ones that accept the local service. With
``--seed`` it sets up an administrator, a session and its room, and prints what it made.
"""
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HOST = "live.example.com"
SERVICE = os.environ.get("LIVE_STACK_URL", "http://127.0.0.1:18090")
DATA = Path(os.environ["PREVIEW_DIR"])

os.environ.update({
    "DJANGO_SETTINGS_MODULE": "config.settings",
    "DJANGO_TEST_SQLITE": "1",
    "DJANGO_TEST_SQLITE_PATH": str(DATA / "studio.sqlite3"),
    "DJANGO_DEBUG": "1",
    "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost",
    "PUBLIC_BASE_URL": "http://127.0.0.1:18100",
    "DJANGO_SECURE_SSL_REDIRECT": "0",
    "DJANGO_SESSION_COOKIE_SECURE": "0",
    "DJANGO_CSRF_COOKIE_SECURE": "0",
    "DJANGO_HSTS_SECONDS": "0",
    "WEBHOOK_HOST_ALLOWLIST": HOST,
    "DJANGO_STATIC_ROOT": str(DATA / "static"),
})
for name, value in (("DJANGO_SECRET_KEY", "preview-only-" + "x" * 50), ("INTEGRATION_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")):
    os.environ.setdefault(name, value)

import django  # noqa: E402

django.setup()

from webinars.hosting import base, livekit  # noqa: E402

_real_https_link = base.https_link


def local_link(value, hosts):
    return value if isinstance(value, str) and value.startswith(SERVICE + "/") else _real_https_link(value, hosts)


def local_transport(method, url, body, headers, *, allowed_hosts):
    request = urllib.request.Request(url.replace(f"https://{HOST}", SERVICE), data=body or None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), None, False
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), None, False


base.https_link = local_link
livekit.https_link = local_link
base.default_transport = local_transport


def seed():
    from datetime import timedelta

    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.utils import timezone

    from tests.conftest import make_event, make_form, make_occurrence
    from webinars import crypto
    from webinars.hosting import service
    from webinars.models import HostedSession, HostingAccount

    call_command("migrate", verbosity=0)
    call_command("collectstatic", verbosity=0, interactive=False)
    call_command("sync_roles", verbosity=0)
    password = secrets.token_urlsafe(12)
    user, _ = get_user_model().objects.get_or_create(username="admin", defaults={"email": "admin@example.com", "is_staff": True, "is_superuser": True})
    user.set_password(password)
    user.save()
    account, _ = HostingAccount.objects.update_or_create(name="Our LiveKit", provider="livekit", defaults={
        "enabled": True, "config": {"service_host": HOST},
        "credentials_encrypted": crypto.encrypt_secret(json.dumps({"service_secret": os.environ["LIVE_SERVICE_SECRET"]})),
    })
    event = make_event(make_form(version=f"preview-{secrets.token_hex(4)}"), public_title="Launch webinar")
    occ = make_occurrence(event, starts_at=timezone.now() + timedelta(minutes=30), join_url="")
    event.slug = "launch-webinar"
    event.save()
    service.execute(occ, account, "create", user=user)
    session = HostedSession.objects.get(occurrence=occ)
    print(json.dumps({"room": session.external_id, "password": password, "session": str(occ.pk), "account": str(account.pk)}))


def serve():
    from django.core.management import call_command

    call_command("runserver", "127.0.0.1:18100", use_reloader=False, verbosity=0)


if __name__ == "__main__":
    seed() if "--seed" in sys.argv else serve()
