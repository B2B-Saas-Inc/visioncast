"""Seed preview sample data and the temporary ``preview`` superuser.

Runs once inside the managed preview container (``python -m preview.seed``).
The password is read from the credential file the host copied in, then that
copy is deleted. It is never printed or logged.
"""
import os
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "preview.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from webinars.models import Event, FormVersion, Host, Occurrence, Topic  # noqa: E402
from webinars.timeutils import local_to_utc  # noqa: E402

CREDENTIAL = "/tmp/preview-credential"

SAMPLES = [
    {
        "slug": "preview-pipeline-analytics",
        "title": "Pipeline Analytics in Practice",
        "host": ("Avery Chen", "avery-chen", "Head of Revenue Operations"),
        "topic": ("Revenue Operations", "revenue-operations"),
        "timezone": "America/New_York",
        "days": 3,
        "hour": 13,
    },
    {
        "slug": "preview-lifecycle-email",
        "title": "Lifecycle Email Without the Guesswork",
        "host": ("Jordan Patel", "jordan-patel", "Director of Lifecycle Marketing"),
        "topic": ("Lifecycle Marketing", "lifecycle-marketing"),
        "timezone": "Europe/London",
        "days": 7,
        "hour": 15,
    },
    {
        "slug": "preview-attribution-fundamentals",
        "title": "Webinar Attribution Fundamentals",
        "host": ("Sam Okafor", "sam-okafor", "Marketing Analytics Lead"),
        "topic": ("Attribution", "attribution"),
        "timezone": "Australia/Sydney",
        "days": 14,
        "hour": 10,
    },
]


def _password():
    try:
        with open(CREDENTIAL, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("password: "):
                    value = line[len("password: "):].strip()
                    if len(value) >= 24:
                        return value
    finally:
        try:
            os.unlink(CREDENTIAL)
        except FileNotFoundError:
            pass
    raise SystemExit("Preview credential missing or invalid.")  # value never echoed


def _occurrence(event, sample):
    tz = sample["timezone"]
    day = (timezone.now().astimezone(ZoneInfo(tz)) + timedelta(days=sample["days"])).date()
    local_naive = datetime(day.year, day.month, day.day, sample["hour"], 0)
    starts_at = local_to_utc(local_naive, tz, None)
    return Occurrence.objects.create(
        event=event,
        timezone=tz,
        # Wall-clock value stored UTC-tagged, matching admin.persist_occurrence.
        local_start=local_naive.replace(tzinfo=dt_timezone.utc),
        duration_minutes=60,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=60),
        ics_uid=f"{uuid.uuid4()}@localhost",
    )


def _starters(form):
    """Realistic AIDA events from webinars.samples; the first gets a waiting room opening soon."""
    from webinars import waiting_room
    from webinars.samples import STARTER_ORDER, STARTERS

    for index, key in enumerate(STARTER_ORDER):
        s = STARTERS[key]
        host, _ = Host.objects.get_or_create(slug=s["host"][1], defaults={"name": s["host"][0], "title": s["host"][2],
                                                                           "bio": "Fictional preview host."})
        topic, _ = Topic.objects.get_or_create(slug=s["topic"][1], defaults={"name": s["topic"][0]})
        event = Event.objects.create(
            internal_name=f"[Preview] {s['internal_name']}", slug=s["slug"], public_title=s["public_title"],
            summary=s["summary"], attention=s["attention"], interest=s["interest"], decision=s["decision"],
            action=s["action"], learning_outcomes=s["learning_outcomes"], visibility=Event.Visibility.PUBLIC,
            form_version=form, thank_you_message=s["thank_you_message"],
        )
        event.hosts.set([host])
        event.topics.set([topic])
        if index == 0:
            starts = (timezone.now() + timedelta(minutes=20)).replace(second=0, microsecond=0)
            tz = s["timezone"]
            local = starts.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
            occ = Occurrence.objects.create(
                event=event, timezone=tz, local_start=local.replace(tzinfo=dt_timezone.utc),
                duration_minutes=s["duration_minutes"], starts_at=starts,
                ends_at=starts + timedelta(minutes=s["duration_minutes"]),
                join_url="https://meet.example.com/fieldnote-live-dashboards", ics_uid=f"{uuid.uuid4()}@localhost",
            )
            waiting_room.configure(occ, enabled=True, lead_minutes=10)
        else:
            _occurrence(event, {"timezone": s["timezone"], "days": s["days"], "hour": s["hour"]})


def main():
    password = _password()
    from webinars.forms_schema import build_definition
    from webinars.samples import DEFAULT_FORM_DESCRIPTION, DEFAULT_FORM_ROWS, DEFAULT_FORM_VERSION

    with transaction.atomic():
        definition = build_definition(DEFAULT_FORM_ROWS)
        standard = FormVersion.objects.create(
            version=DEFAULT_FORM_VERSION, description=DEFAULT_FORM_DESCRIPTION, **definition
        )
        _starters(standard)
        form = FormVersion.objects.create(
            version="preview-v1",
            description="Preview sample form",
            required_fields=["email", "firstName", "lastName"],
            optional_fields=["company", "jobTitle"],
        )
        for sample in SAMPLES:
            name, slug, title = sample["host"]
            host, _ = Host.objects.get_or_create(slug=slug, defaults={"name": name, "title": title, "bio": "Fictional preview host."})
            topic, _ = Topic.objects.get_or_create(slug=sample["topic"][1], defaults={"name": sample["topic"][0]})
            event = Event.objects.create(
                internal_name=f"[Preview] {sample['title']}",
                slug=sample["slug"],
                public_title=sample["title"],
                summary="Preview sample data. No emails or webhooks are sent.",
                learning_outcomes=["Sample outcome one", "Sample outcome two"],
                visibility=Event.Visibility.PUBLIC,
                form_version=form,
                thank_you_message="Thanks. This is a preview: nothing is sent.",
            )
            event.hosts.set([host])
            event.topics.set([topic])
            _occurrence(event, sample)
        get_user_model().objects.create_superuser(username="preview", email="", password=password)
    print(f"Preview sample data seeded: {len(SAMPLES) + 3} events.")


if __name__ == "__main__":
    main()
