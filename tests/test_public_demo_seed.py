"""Invariants of ``manage.py seed_public_demo`` (webinars/management/commands/seed_public_demo.py)."""
import io
from datetime import timedelta
from unittest import mock

import pytest
from django.core import mail
from django.core.management import CommandError, call_command
from django.utils import timezone

from tests.conftest import make_destination, make_event, make_form, make_occurrence
from webinars import services
from webinars.api import queries
from webinars.api.views import serialize_event
from webinars.forms_schema import public_schema, validate_layout
from webinars.management.commands import seed_public_demo as seed
from webinars.models import (
    DEFAULT_CONFIRM_LABEL,
    DEFAULT_REGISTER_LABEL,
    AttributionTouch,
    AuditLog,
    ConsentRecord,
    Destination,
    Event,
    EventRevision,
    FormVersion,
    Host,
    Occurrence,
    OutboxMessage,
    Person,
    PreviewMessage,
    Registration,
    ReminderSchedule,
    Topic,
)

pytestmark = pytest.mark.django_db

EVENT_SLUGS = [e["slug"] for e in seed.EVENTS]
HOST_SLUGS = [h["slug"] for h in seed.HOSTS]
TOPIC_SLUGS = [slug for slug, _ in seed.TOPICS]
FORM_VERSIONS = [f["version"] for f in seed.FORMS]


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    call_command("seed_public_demo", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def demo_events():
    return queries.event_queryset().filter(slug__in=EVENT_SLUGS)


def demo_sessions():
    return Occurrence.objects.filter(event__slug__in=EVENT_SLUGS)


def counts():
    return {
        model.__name__: model.objects.count()
        for model in (Event, Host, Topic, FormVersion, Occurrence, EventRevision, AuditLog)
    }


# ---------------------------------------------------------------------------
# Catalogue shape
# ---------------------------------------------------------------------------


def test_seeds_four_upcoming_and_two_past_public_events():
    run()
    assert demo_events().count() == 6
    assert {e.visibility for e in demo_events()} == {Event.Visibility.PUBLIC}
    upcoming = {e.slug for e in queries.public_events(when="upcoming") if e.slug in EVENT_SLUGS}
    recorded = {e.slug for e in queries.public_events(when="recorded") if e.slug in EVENT_SLUGS}
    assert len(upcoming) == 4
    assert len(recorded) == 2
    assert upcoming.isdisjoint(recorded)
    assert upcoming | recorded == set(EVENT_SLUGS)


def test_every_event_is_fully_enriched_in_the_public_projection():
    run()
    for event in demo_events():
        body = serialize_event(event)
        assert event.internal_name.startswith("[Demo] ")
        assert body["title"].startswith("Demo: ")
        assert body["summary"].startswith(seed.FICTIONAL_NOTE)
        assert body["heroImageUrl"] == f"https://visioncast-ten.vercel.app/demo-art/{event.slug}.svg"
        for part in ("attention", "interest", "decision", "action"):
            assert len(body["aida"][part]) > 40, (event.slug, part)
        assert len(body["learningOutcomes"]) >= 3
        assert body["hosts"] and body["topics"]
        for host in body["hosts"]:
            assert host["title"] and host["imageUrl"] == f"https://visioncast-ten.vercel.app/demo-art/{host['slug']}.svg"
        assert body["cta"]["register"] != DEFAULT_REGISTER_LABEL
        assert body["cta"]["confirm"] != DEFAULT_CONFIRM_LABEL
        assert body["formVersion"] in FORM_VERSIONS
        assert event.thank_you_mode == Event.ThankYouMode.MESSAGE
        assert "fictional" in event.thank_you_message.lower()
        assert len(body["occurrences"]) == 1


def test_upcoming_events_pass_the_publish_readiness_check():
    run()
    upcoming = [e for e in demo_events() if e.occurrences.filter(starts_at__gt=timezone.now()).exists()]
    assert len(upcoming) == 4
    for event in upcoming:
        assert services.publish_problems(event) == []


def test_hosts_and_topics_are_complete_and_labelled_fictional():
    run()
    hosts = Host.objects.filter(slug__in=HOST_SLUGS)
    assert hosts.count() == len(HOST_SLUGS) == 5
    for host in hosts:
        assert host.bio.startswith("Fictional demo host.") and len(host.bio) > 120
        assert "(fictional)" in host.title
        assert host.image_url == seed.art_url(host.slug)
        assert host.events.filter(slug__in=EVENT_SLUGS).exists()
    topics = Topic.objects.filter(slug__in=TOPIC_SLUGS)
    assert topics.count() == len(TOPIC_SLUGS)
    for topic in topics:
        assert topic.name and topic.events.filter(slug__in=EVENT_SLUGS).exists()


def test_form_versions_are_valid_and_publicly_described():
    run()
    for version in FORM_VERSIONS:
        fv = FormVersion.objects.get(version=version)
        assert fv.is_active and "Fictional demo form" in fv.description
        assert validate_layout(fv.field_layout, fv.required_fields, fv.optional_fields, fv.custom_fields)
        assert "email" in fv.required_fields
        assert fv.custom_fields
        assert not fv.sms_disclosure_version  # no SMS consent capture on the demo
        schema = public_schema(fv)
        assert [f["key"] for f in schema["fields"]][0] == "email"
        assert any(f["options"] for f in schema["fields"])
        assert queries.public_form_version(version) is not None


def test_sessions_have_no_private_links_and_past_recordings_are_demo_pages():
    run()
    now = timezone.now()
    for occ in demo_sessions():
        assert occ.join_url == "" and occ.waiting_room_url == ""
        assert not occ.waiting_room_enabled and occ.waiting_room_code is None
        assert occ.ics_uid.endswith("@demo-seed.invalid")
        assert not occ.registration_open
        if occ.ends_at <= now:
            assert occ.status == Occurrence.Status.COMPLETED
            assert occ.recording_published
            assert occ.recording_url == f"https://visioncast-ten.vercel.app/demo-recording/{occ.event.slug}"
            assert occ.recording_published_at <= now
        else:
            assert occ.status == Occurrence.Status.SCHEDULED
            assert occ.recording_url == "" and occ.recording_published_at is None


def test_allow_registration_opens_only_new_upcoming_sessions():
    run("--allow-registration")
    now = timezone.now()
    for occ in demo_sessions():
        assert occ.registration_open is (occ.starts_at > now)


# ---------------------------------------------------------------------------
# No attendee data, integrations or sends
# ---------------------------------------------------------------------------


def test_creates_no_people_integrations_or_messages():
    run()
    for model in (Person, Registration, ConsentRecord, AttributionTouch, ReminderSchedule, Destination,
                  OutboxMessage, PreviewMessage):
        assert model.objects.count() == 0, model.__name__
    assert mail.outbox == []


def test_existing_enabled_destination_gets_nothing_and_is_warned_about():
    destination = make_destination(event_types=["recording.published", "registration.created"])
    _, err = run()
    assert "1 enabled destination" in err
    assert OutboxMessage.objects.count() == 0
    destination.refresh_from_db()
    assert destination.enabled  # never touched


def test_audit_metadata_carries_no_pii():
    run()
    for entry in AuditLog.objects.all():
        assert not ({"email", "phone", "name"} & set(entry.metadata))


# ---------------------------------------------------------------------------
# Idempotency and stable dates
# ---------------------------------------------------------------------------


def test_rerun_later_changes_nothing_and_keeps_dates():
    run()
    before = counts()
    sessions = {o.ics_uid: (o.starts_at, o.ends_at, o.local_start, o.ics_sequence, o.schedule_generation)
                for o in demo_sessions()}
    revisions = {e.slug: e.revision for e in demo_events()}
    later = timezone.now() + timedelta(days=5)
    with mock.patch.object(seed.timezone, "now", return_value=later):
        out, _ = run("--allow-registration")
    assert "0 created, 0 updated" in out
    assert counts() == before
    assert {o.ics_uid: (o.starts_at, o.ends_at, o.local_start, o.ics_sequence, o.schedule_generation)
            for o in demo_sessions()} == sessions
    assert {e.slug: e.revision for e in demo_events()} == revisions
    assert not demo_sessions().filter(registration_open=True).exists()  # existing sessions untouched


def test_rerun_restores_edited_demo_content_with_a_new_revision():
    run()
    event = Event.objects.get(slug=EVENT_SLUGS[0])
    Event.objects.filter(pk=event.pk).update(public_title="Edited", visibility=Event.Visibility.DRAFT)
    Host.objects.filter(slug=HOST_SLUGS[0]).update(bio="Edited")
    out, _ = run()
    event.refresh_from_db()
    assert event.public_title.startswith("Demo: ")
    assert event.visibility == Event.Visibility.PUBLIC
    assert event.revision == 2
    assert EventRevision.objects.filter(event=event, revision=2).exists()
    assert Host.objects.get(slug=HOST_SLUGS[0]).bio.startswith("Fictional demo host.")
    assert "0 created, 2 updated" in out


def test_deleted_demo_session_is_recreated_without_moving_others():
    run()
    first = demo_sessions().get(ics_uid=seed.session_uid(EVENT_SLUGS[0], 1))
    others = {o.ics_uid: o.starts_at for o in demo_sessions().exclude(pk=first.pk)}
    first.delete()
    run()
    assert demo_sessions().count() == 6
    assert {o.ics_uid: o.starts_at for o in demo_sessions().exclude(ics_uid=first.ics_uid)} == others


def test_dry_run_writes_nothing():
    before = counts()
    out, _ = run("--dry-run")
    assert "[dry run] would have created event" in out
    assert counts() == before


# ---------------------------------------------------------------------------
# Owner data is never modified
# ---------------------------------------------------------------------------


def test_unrelated_owner_data_is_untouched():
    form = make_form("owner-form")
    owner = make_event(form, slug="owner-webinar", public_title="Owner webinar")
    occ = make_occurrence(owner)
    host = Host.objects.create(slug="owner-host", name="Owner Host", bio="Real person.")
    owner.hosts.set([host])
    snapshot = (
        Event.objects.filter(pk=owner.pk).values().get(),
        Occurrence.objects.filter(pk=occ.pk).values().get(),
        Host.objects.filter(pk=host.pk).values().get(),
        FormVersion.objects.filter(pk=form.pk).values().get(),
    )
    run()
    run()
    assert snapshot == (
        Event.objects.filter(pk=owner.pk).values().get(),
        Occurrence.objects.filter(pk=occ.pk).values().get(),
        Host.objects.filter(pk=host.pk).values().get(),
        FormVersion.objects.filter(pk=form.pk).values().get(),
    )


def _owner_host():
    return Host.objects.create(slug=HOST_SLUGS[-1], name="Owner's own host", bio="Not a demo.")


def _owner_topic():
    return Topic.objects.create(slug=TOPIC_SLUGS[-1], name="Owner topic")


def _owner_form():
    return make_form(FORM_VERSIONS[0])


def _owner_event():
    return make_event(make_form("owner-form"), slug=EVENT_SLUGS[-1], public_title="Owner event")


def _owner_session():
    event = make_event(make_form("owner-form"), slug="owner-webinar")
    return make_occurrence(event, ics_uid=seed.session_uid(EVENT_SLUGS[0], 1))


@pytest.mark.parametrize("make_owner_row", [_owner_host, _owner_topic, _owner_form, _owner_event, _owner_session])
def test_namespace_collision_with_owner_data_aborts_without_changes(make_owner_row):
    row = make_owner_row()
    stored = type(row).objects.filter(pk=row.pk).values().get()
    before = counts()
    with pytest.raises(CommandError, match="Refusing to seed"):
        run()
    assert counts() == before
    assert type(row).objects.filter(pk=row.pk).values().get() == stored
    assert not AuditLog.objects.filter(action=seed.OWNED).exists()


def test_collision_is_detected_on_rerun_too():
    run()
    Topic.objects.filter(slug=TOPIC_SLUGS[0]).delete()
    Topic.objects.create(slug=TOPIC_SLUGS[0], name="Owner reused the slug")
    before = counts()
    with pytest.raises(CommandError, match="Topic"):
        run()
    assert counts() == before


def test_every_seeded_row_is_recorded_as_seed_owned():
    run()
    for model, field, values in (
        (Topic, "slug", TOPIC_SLUGS),
        (Host, "slug", HOST_SLUGS),
        (FormVersion, "version", FORM_VERSIONS),
        (Event, "slug", EVENT_SLUGS),
    ):
        rows = model.objects.filter(**{f"{field}__in": values})
        assert rows.count() == len(values)
        assert all(seed.is_owned(row) for row in rows)
    assert all(seed.is_owned(occ) for occ in demo_sessions())
    assert seed.find_conflicts() == []


# ---------------------------------------------------------------------------
# Art contract with the demo frontend
# ---------------------------------------------------------------------------


def test_art_slug_list_covers_every_event_and_host_image():
    out, _ = run()
    slugs = seed.art_slugs()
    assert len(slugs) == len(set(slugs)) == 11
    used = {e.hero_image_url for e in Event.objects.filter(slug__in=EVENT_SLUGS)}
    used |= {h.image_url for h in Host.objects.filter(slug__in=HOST_SLUGS)}
    assert used == {seed.art_url(s) for s in slugs}
    for slug in slugs:
        assert seed.art_url(slug) in out
