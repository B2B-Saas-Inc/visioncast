"""Shared fixtures and helpers for the webinar platform test-suite.

Tests are written against docs/architecture.md (module interfaces + HTTP contract).
Helpers are plain functions so test modules can ``from tests.conftest import ...``.
"""
import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from django.utils import timezone

# base64.urlsafe_b64encode(b"0" * 32)
MASTER_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
ORIGIN = "https://www.example.com"
COOKIE = "wbr_cap"
FORM_VERSION = "f1"
WEBHOOK_SECRET = "whsec_test_secret_value"
JOIN_URL = "https://join.example.org/s/secret-join-token-123"
WAITING_URL = "https://join.example.org/w/secret-waiting-token-456"
DEFAULT_EMAIL = "Ada.Lovelace+webinar@Example.COM"


@pytest.fixture(autouse=True)
def app_settings(settings):
    settings.INTEGRATION_MASTER_KEY = MASTER_KEY
    settings.INTEGRATION_MASTER_KEY_PREVIOUS = []
    settings.WEBINAR_ALLOWED_ORIGINS = [ORIGIN]
    settings.THANK_YOU_REDIRECT_HOSTS = ["www.example.com"]
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    settings.WEBHOOK_ALLOWED_PORTS = [443]
    settings.CAPABILITY_COOKIE_NAME = COOKIE
    settings.CAPABILITY_COOKIE_SECURE = True
    settings.CAPABILITY_TTL_DAYS = 180
    settings.PUBLIC_BASE_URL = "https://webinars.example.com"
    settings.PUBLIC_EVENT_URL_TEMPLATE = ""
    settings.WEBINAR_DIRECT_ICS_REWRITE = True
    settings.SECURE_SSL_REDIRECT = False
    settings.REMINDER_OFFSETS_MINUTES = [1440, 60]
    settings.REMINDER_LATE_GRACE_MINUTES = 15
    settings.OUTBOX_LEASE_SECONDS = 120
    settings.OUTBOX_BATCH_SIZE = 20
    settings.ALLOWED_HOSTS = ["testserver", "localhost"]
    # Rate-limit counters live in the per-process cache; start every test clean.
    from django.core.cache import cache

    cache.clear()
    return settings


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def make_form(version=FORM_VERSION, **kw):
    from webinars.models import FormVersion

    defaults = dict(
        version=version,
        required_fields=["email", "firstName"],
        optional_fields=["lastName", "company", "phone"],
        field_mapping={},
        custom_fields=["role"],
        default_phone_region="GB",
        sms_disclosure_version="sms-v1",
        sms_disclosure_text="We may text you reminders. Reply STOP to opt out.",
    )
    defaults.update(kw)
    return FormVersion.objects.create(**defaults)


def make_event(form, slug=None, visibility="public", **kw):
    from webinars.models import Event

    slug = slug or f"event-{uuid.uuid4().hex[:8]}"
    defaults = dict(
        internal_name=f"Internal {slug}",
        slug=slug,
        public_title=f"Webinar {slug}",
        summary="A public summary.",
        visibility=visibility,
        form_version=form,
        thank_you_mode="message",
        thank_you_message="Thanks for registering!",
    )
    defaults.update(kw)
    return Event.objects.create(**defaults)


def make_occurrence(event, starts_at=None, days=10, tz="Europe/London", duration=60, **kw):
    """Create an occurrence. ``starts_at`` (aware UTC) defaults to noon local, ``days`` ahead."""
    from zoneinfo import ZoneInfo

    from webinars.models import Occurrence
    from webinars.timeutils import local_to_utc

    if starts_at is None:
        local_day = (timezone.now() + timedelta(days=days)).astimezone(ZoneInfo(tz)).date()
        local = datetime(local_day.year, local_day.month, local_day.day, 12, 0)
        starts_at = local_to_utc(local, tz, None)
    else:
        local = starts_at.astimezone(ZoneInfo(tz)).replace(tzinfo=None, fold=0)
    defaults = dict(
        event=event,
        timezone=tz,
        # Stored UTC-tagged wall clock, same convention as scheduling.reschedule_occurrence.
        local_start=local.replace(tzinfo=dt_timezone.utc),
        duration_minutes=duration,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=duration),
        join_url=JOIN_URL,
        waiting_room_url=WAITING_URL,
        ics_uid=f"{uuid.uuid4()}@webinars.test",
    )
    defaults.update(kw)
    return Occurrence.objects.create(**defaults)


def make_api_key(scopes=("proxy",), name="proxy"):
    from webinars.api.auth import generate_api_key

    _key, raw = generate_api_key(name, list(scopes))
    return raw


def make_destination(event_types=("registration.created",), **kw):
    from webinars.crypto import encrypt_secret
    from webinars.models import Destination

    defaults = dict(
        name="Test hook",
        url="https://hooks.example.net/webhook",
        enabled=True,
        event_types=list(event_types),
        allowed_groups=list(Destination.GROUPS),
        required_consents=[],
        include_private_links=False,
        signing_secret_encrypted=encrypt_secret(WEBHOOK_SECRET),
        max_attempts=8,
    )
    defaults.update(kw)
    return Destination.objects.create(**defaults)


def make_person(email="grace@example.com", **kw):
    from webinars.models import Person
    from webinars.normalization import email_key

    return Person.objects.create(email=email, email_key=email_key(email) if email else None, **kw)


def make_registration(occurrence, person=None, **kw):
    from webinars.models import Registration

    person = person or make_person(email=f"p{uuid.uuid4().hex[:8]}@example.com", first_name="Pat")
    return Registration.objects.create(
        occurrence=occurrence, person=person, form_version=occurrence.event.form_version.version, identity_verified=True, **kw
    )


@pytest.fixture
def form(db):
    return make_form()


@pytest.fixture
def event(form):
    return make_event(form, slug="public-webinar")


@pytest.fixture
def occurrence(event):
    return make_occurrence(event)


@pytest.fixture
def proxy_key(db):
    return make_api_key(("proxy",))


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _deep_merge(base, overrides):
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def payload(occurrence=None, **overrides):
    """Build an exact SPEC registration body. Dict overrides are deep-merged;
    pass ``profile=...`` etc. via ``replace={...}`` to replace wholesale."""
    replace = overrides.pop("replace", {})
    body = {
        "schemaVersion": "1",
        "occurrenceId": str(occurrence.id) if occurrence is not None else str(uuid.uuid4()),
        "formVersion": FORM_VERSION,
        "idempotencyKey": f"idem-{uuid.uuid4().hex}",
        "profile": {"email": DEFAULT_EMAIL, "firstName": "Ada"},
        "organization": {"externalIds": []},
        "consents": {
            "sms": {"granted": False, "disclosureVersion": "sms-v1"},
            "analytics": {"granted": False},
            "advertising": {"granted": False},
        },
        "attribution": {"parameters": {}, "identifiers": {}, "pageUrl": "", "referrer": ""},
        "customFields": {},
    }
    _deep_merge(body, copy.deepcopy(overrides))
    body.update(copy.deepcopy(replace))
    return body


def post_registration(client, body, key, cookie=None, path="/api/v1/registrations", raw=None, **headers):
    """POST a registration as an explicit browser identity.

    ``cookie=None`` means an anonymous browser: any capability cookie the test client
    picked up from earlier responses is removed first.
    """
    client.cookies.pop(COOKIE, None)
    if cookie is not None:
        client.cookies[COOKIE] = cookie
    if key:
        headers.setdefault("Authorization", f"Bearer {key}")
    data = raw if raw is not None else json.dumps(body)
    return client.post(path, data=data, content_type=headers.pop("content_type", "application/json"), headers=headers)


def get_profile(client, occurrence, key, cookie=None, form_version=FORM_VERSION):
    client.cookies.pop(COOKIE, None)
    if cookie is not None:
        client.cookies[COOKIE] = cookie
    return client.get(
        "/api/v1/profile",
        {"occurrenceId": str(occurrence.id), "formVersion": form_version},
        headers={"Authorization": f"Bearer {key}"},
    )


def cookie_value(response):
    morsel = response.cookies.get(COOKIE)
    return morsel.value if morsel is not None and morsel.value else None


def sha256(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def register_with_cookie(client, occurrence, key, **overrides):
    """Register anonymously as a new person and return (response, raw_token)."""
    resp = post_registration(client, payload(occurrence, **overrides), key)
    assert resp.status_code == 201, resp.content
    token = cookie_value(resp)
    assert token, "new person registration must issue a capability cookie"
    return resp, token
