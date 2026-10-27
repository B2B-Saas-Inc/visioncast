"""Seed synthetic Riverside fixtures for tests/browser-riverside.cjs.

Runs ONLY inside the disposable managed preview container, after the preview server
is ready, against the same SQLite database (``preview.settings``). Nothing contacts
Riverside: the hosting account has no credentials, the session is attached directly in
the database and the registrant is left stalled as if Riverside never answered.

Writes one JSON file (mode 0600, under /tmp only) holding the attendee capabilities, a
synthetic personal link and a random Editor password the browser test needs. Nothing
secret is printed.

Usage (inside the container): PYTHONPATH=/app python riverside_browser_seed.py /tmp/rv/fixture.json
"""
import json
import os
import secrets
import sys
import uuid
from datetime import timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

if not os.environ.get("WEBINAR_PREVIEW_RUN", "").startswith("pi-webinar-"):
    raise SystemExit("Not a managed webinar preview container")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "preview.settings")
if os.environ["DJANGO_SETTINGS_MODULE"] != "preview.settings":
    raise SystemExit("Riverside browser fixtures are preview-only")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.auth.models import Group  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from webinars import identity, waiting_room  # noqa: E402
from webinars.hosting import base  # noqa: E402
from webinars.models import (  # noqa: E402
    Event,
    FormVersion,
    HostedSession,
    HostingAccount,
    Occurrence,
    Person,
    ProviderRegistrant,
    Registration,
)
from webinars.normalization import email_key  # noqa: E402
from webinars.roles import EDITOR  # noqa: E402

TZ = "Europe/London"
STALLED_EMAIL = "riverside-linked@example.invalid"
MISSING_EMAIL = "riverside-missing@example.invalid"


def _no_network(*a, **kw):
    raise AssertionError("the Riverside browser seed must never call a provider")


def _person(email, first):
    return Person.objects.create(email=email, email_key=email_key(email), first_name=first, last_name="Example")


def _registration(occ, person):
    return Registration.objects.create(occurrence=occ, person=person, form_version=occ.event.form_version.version,
                                       identity_verified=True)


def main(path):
    base.default_transport = _no_network
    suffix = uuid.uuid4().hex[:8]
    with transaction.atomic():
        form = FormVersion.objects.get(version="preview-v1")
        event = Event.objects.create(
            internal_name="[Preview] Riverside browser fixture", slug=f"riverside-browser-{suffix}",
            public_title="Riverside browser fixture", summary="Synthetic fixture. Nothing is sent.",
            learning_outcomes=["Synthetic"], visibility=Event.Visibility.PUBLIC, form_version=form,
            thank_you_message="Synthetic fixture.",
        )
        starts = (timezone.now() + timedelta(minutes=30)).replace(second=0, microsecond=0)
        local = starts.astimezone(ZoneInfo(TZ)).replace(tzinfo=None)
        occ = Occurrence.objects.create(
            event=event, timezone=TZ, local_start=local.replace(tzinfo=dt_timezone.utc), duration_minutes=60,
            starts_at=starts, ends_at=starts + timedelta(minutes=60), join_url="",  # no shared link
            ics_uid=f"{uuid.uuid4()}@localhost",
        )
        account = HostingAccount.objects.create(provider="riverside", name=f"Riverside fixture {suffix}", enabled=True)
        session = HostedSession.objects.create(occurrence=occ, account=account, provider="riverside",
                                               mode=HostedSession.Mode.ATTACHED, state=HostedSession.State.LINKED,
                                               external_id=f"rv-event-{suffix}")
        stalled = _registration(occ, _person(STALLED_EMAIL, "Riley"))
        ProviderRegistrant.objects.create(
            registration=stalled, session=session, state=ProviderRegistrant.State.NEEDS_RECONCILIATION, attempts=1,
            event_account_id=account.pk, event_external_id=session.external_id,
        )
        missing = _registration(occ, _person(MISSING_EMAIL, "Morgan"))  # registered, no personal link
        editor_password = secrets.token_urlsafe(24)
        editor = get_user_model().objects.create_user(f"rv-editor-{suffix}", password=editor_password)
        editor.groups.add(Group.objects.get_or_create(name=EDITOR)[0])
    waiting_room.configure(occ, enabled=True, lead_minutes=15)
    waiting_room.set_admission(occ, "open")
    occ.refresh_from_db()
    fixture = {
        "occurrenceId": str(occ.pk),
        "roomCode": occ.waiting_room_code,
        "stalledRegistrationId": str(stalled.pk),
        "missingRegistrationId": str(missing.pk),
        "maskedEmail": "r***@example.invalid",
        "stalledEmail": STALLED_EMAIL,
        "missingEmail": MISSING_EMAIL,
        "stalledCapability": identity.issue_capability(stalled.person),
        "missingCapability": identity.issue_capability(missing.person),
        "cookieName": identity._cookie_name(),
        "personalLink": f"https://riverside.fm/studio/browser-e2e?t=SYNTH-{secrets.token_hex(12)}",
        "editorUsername": editor.username,
        "editorPassword": editor_password,
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(fixture, handle)
    print("Riverside browser fixture seeded.")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("/tmp/"):
        raise SystemExit("usage: riverside_browser_seed.py /tmp/<dir>/fixture.json")
    main(sys.argv[1])
