"""Runtime smoke probe for the built production image (run by the managed
`webinar_image_smoke` lifecycle only; never against production).

Runs inside the built image as UID 10001 on an internal-only network, with this
file as the only bind mount (read-only). Talks to the `web` container over HTTP
and to the disposable PostgreSQL database through the packaged Django settings.
Seeds minimal synthetic data (example.invalid). Prints exactly one
``IMAGE_SMOKE_RESULT <json>`` line and never prints secrets or API keys.
Standard library plus the packaged application only.
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

BASE = "http://web:8000"
checks = {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(method, path, body=None, headers=None, https=True):
    headers = dict(headers or {})
    if https:
        # Documented reverse-proxy mode (DJANGO_TRUST_X_FORWARDED_PROTO=1); see docker/web_healthcheck.py.
        headers["X-Forwarded-Proto"] = "https"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with _opener.open(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def check(name, fn):
    try:
        checks[name] = bool(fn())
    except Exception as exc:  # report the category only, never request bodies
        checks[name] = False
        checks[f"{name}_error"] = type(exc).__name__


def egress_blocked():
    try:
        socket.getaddrinfo("example.com", 443)
        return False
    except OSError:
        pass
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        return False
    except OSError:
        return True


def wait_healthy():
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            status, _, body = http("GET", "/healthz")
            if status == 200 and json.loads(body).get("status") == "ok":
                return True
        except OSError:
            pass
        time.sleep(2)
    return False


def main():
    sys.path.insert(0, "/app")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    check("uid_10001", lambda: os.getuid() == 10001 and os.getgid() == 10001)
    check("production_settings", lambda: (
        settings.DEBUG is False and settings.TEST_SQLITE is False
        and settings.SECURE_SSL_REDIRECT is True and settings.SESSION_COOKIE_SECURE is True
        and settings.CSRF_COOKIE_SECURE is True and settings.SECURE_HSTS_SECONDS > 0
        and settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"
    ))
    check("no_outgoing_email_or_webhooks", lambda: (
        settings.EMAIL_BACKEND == "webinars.mail.DisabledBackend" and settings.WEBHOOK_HOST_ALLOWLIST == []
    ))
    check("egress_blocked", egress_blocked)

    def postgres16():
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version_num")
            return 160000 <= int(cursor.fetchone()[0]) < 170000

    check("postgres_16", postgres16)

    def migrated():
        executor = MigrationExecutor(connection)
        return executor.migration_plan(executor.loader.graph.leaf_nodes()) == []

    check("migrations_applied", migrated)
    check("web_healthz", wait_healthy)

    def tls_redirect():
        status, headers, _ = http("GET", "/api/v1/events", https=False)
        location = headers.get("Location") or headers.get("location") or ""
        return status in (301, 308) and location.startswith("https://")

    check("tls_redirect_enforced", tls_redirect)

    from webinars.api.auth import generate_api_key
    from webinars.models import Destination, Event, FormVersion, Occurrence, Registration

    marker = uuid.uuid4().hex[:10]
    slug = f"image-smoke-{marker}"
    seeded = {}

    def seed():
        get_user_model().objects.create_user(username=f"smoke-{marker}", email=f"staff-{marker}@example.invalid", password=None)
        form = FormVersion.objects.create(
            version=f"s{marker}", required_fields=["email", "firstName"], optional_fields=[], field_mapping={},
            custom_fields=[], default_phone_region="GB", sms_disclosure_version="sms-v1",
            sms_disclosure_text="Synthetic smoke disclosure.",
        )
        event = Event.objects.create(
            internal_name=f"Smoke {slug}", slug=slug, public_title=f"Smoke {slug}", summary="Synthetic smoke event.",
            visibility="public", form_version=form, thank_you_mode="message", thank_you_message="Thanks.",
        )
        day = (datetime.now(dt_timezone.utc) + timedelta(days=10)).date()
        starts = datetime(day.year, day.month, day.day, 12, 0, tzinfo=dt_timezone.utc)
        occurrence = Occurrence.objects.create(
            event=event, timezone="UTC", local_start=starts, duration_minutes=30, starts_at=starts,
            ends_at=starts + timedelta(minutes=30), join_url="https://join.example.invalid/s/smoke",
            waiting_room_url="https://join.example.invalid/w/smoke", ics_uid=f"{uuid.uuid4()}@smoke.invalid",
        )
        _key, raw = generate_api_key(f"image-smoke-{marker}", ["proxy"])
        seeded.update(form=form.version, occurrence=occurrence, key=raw)
        return get_user_model().objects.count() >= 1

    check("synthetic_seed", seed)

    def public_list():
        status, _, body = http("GET", "/api/v1/events")
        return status == 200 and slug in body.decode()

    def openapi():
        status, _, body = http("GET", "/api/v1/openapi.json")
        document = json.loads(body)
        return status == 200 and "openapi" in document and "/api/v1/registrations" in document.get("paths", {})

    def mcp_docs():
        status, _, _ = http("GET", "/developers/mcp/")
        denied, _, _ = http("GET", "/mcp")
        return status == 200 and 400 <= denied < 500

    check("public_event_list", public_list)
    check("packaged_openapi", openapi)
    check("packaged_mcp_docs", mcp_docs)

    def register():
        occurrence = seeded["occurrence"]
        body = {
            "schemaVersion": "1", "occurrenceId": str(occurrence.id), "formVersion": seeded["form"],
            "idempotencyKey": f"smoke-{marker}", "profile": {"email": f"smoke+{marker}@example.invalid", "firstName": "Smoke"},
            "organization": {"externalIds": []},
            "consents": {"sms": {"granted": False, "disclosureVersion": "sms-v1"}, "analytics": {"granted": False}, "advertising": {"granted": False}},
            "attribution": {"parameters": {}, "identifiers": {}, "pageUrl": "", "referrer": ""}, "customFields": {},
        }
        status, _, response = http("POST", "/api/v1/registrations", body, {"Authorization": f"Bearer {seeded['key']}"})
        return (status == 201 and json.loads(response).get("status") == "registered"
                and Registration.objects.filter(occurrence=occurrence).count() == 1
                and Destination.objects.count() == 0)

    check("registration_api", register)
    ok = all(value for key, value in checks.items() if not key.endswith("_error"))
    print("IMAGE_SMOKE_RESULT " + json.dumps({"ok": ok, "checks": checks}, sort_keys=True), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
