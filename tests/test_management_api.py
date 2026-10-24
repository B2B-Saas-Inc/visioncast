"""Management (write) API: docs/management-api.md."""
import json
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.conftest import (
    JOIN_URL,
    WAITING_URL,
    make_api_key,
    make_destination,
    make_event,
    make_occurrence,
    make_registration,
)

pytestmark = pytest.mark.django_db

BASE = "/api/v1/manage"


@pytest.fixture
def content_key(db):
    return make_api_key(("content_write",), name="cms")


@pytest.fixture
def attendance_key(db):
    return make_api_key(("attendance_write",), name="attendance")


@pytest.fixture
def publish_key(db):
    # Publication (visibility changes, recordings) needs the publication scope + confirmation.
    return make_api_key(("content_write", "publication"), name="publisher")


def call(client, method, path, key, body=None, raw=None, confirm=None):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if confirm:
        headers["Webinar-Confirm-Token"] = confirm
    data = raw if raw is not None else (json.dumps(body) if body is not None else "")
    fn = getattr(client, method.lower())
    if method in ("GET", "DELETE"):
        return fn(path, headers=headers)
    return fn(path, data=data, content_type="application/json", headers=headers)


def event_body(form, **kw):
    body = {
        "internalName": "Q4 pipeline webinar",
        "slug": "q4-pipeline",
        "publicTitle": "Building pipeline in Q4",
        "formVersion": form.version,
        "learningOutcomes": ["One", "Two"],
    }
    body.update(kw)
    return body


# ---------------------------------------------------------------------------
# Authentication / authorization
# ---------------------------------------------------------------------------


def test_requires_key(client, form):
    from webinars.models import Event

    resp = call(client, "POST", f"{BASE}/events", None, event_body(form))
    assert resp.status_code == 401
    assert not Event.objects.exists()


@pytest.mark.parametrize("scopes", [("proxy",), ("trusted",), ("public_read",), ("reporting",), ("attendance_write",)])
def test_other_scopes_cannot_write_content(client, form, scopes):
    from webinars.models import Event

    resp = call(client, "POST", f"{BASE}/events", make_api_key(scopes), event_body(form))
    assert resp.status_code == 403
    assert not Event.objects.exists()


def test_content_key_cannot_record_attendance(client, occurrence, content_key):
    reg = make_registration(occurrence)
    resp = call(client, "POST", f"{BASE}/registrations/{reg.pk}/attendance", content_key, {"kind": "attended"})
    assert resp.status_code == 403


def test_revoked_key_denied(client, form, content_key):
    from webinars.models import ApiKey

    ApiKey.objects.update(revoked_at=timezone.now())
    assert call(client, "POST", f"{BASE}/events", content_key, event_body(form)).status_code == 401


def test_write_scopes_are_known():
    from webinars.api.auth import generate_api_key

    key, _raw = generate_api_key("x", ["content_write", "attendance_write"])
    assert key.scopes == ["content_write", "attendance_write"]


def test_method_not_allowed(client, content_key):
    resp = call(client, "PUT", f"{BASE}/events", content_key, {})
    assert resp.status_code == 405


def test_requires_json(client, form, content_key):
    resp = client.post(f"{BASE}/events", data="x=1", content_type="application/x-www-form-urlencoded",
                       headers={"Authorization": f"Bearer {content_key}"})
    assert resp.status_code == 415


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_create_event_is_draft_with_revision_and_audit(client, form, content_key):
    from webinars.models import AuditLog, Event, EventRevision

    resp = call(client, "POST", f"{BASE}/events", content_key, event_body(form))
    assert resp.status_code == 201, resp.content
    data = resp.json()
    assert data["visibility"] == "draft"
    assert data["revision"] == 1
    event = Event.objects.get(pk=data["id"])
    assert EventRevision.objects.filter(event=event, revision=1).exists()
    log = AuditLog.objects.get(action="event.created")
    assert log.actor_api_key is not None and log.object_id == str(event.pk)
    # Draft is not public.
    assert client.get("/api/v1/events/q4-pipeline").status_code == 404


def test_create_rejects_non_draft_visibility(client, form, content_key):
    resp = call(client, "POST", f"{BASE}/events", content_key, event_body(form, visibility="public"))
    assert resp.status_code == 400
    assert "visibility" in resp.json()["error"]["fields"]


def test_unknown_fields_rejected_no_setattr(client, form, content_key):
    from webinars.models import Event

    resp = call(client, "POST", f"{BASE}/events", content_key, event_body(form, revision=99, id="x", is_admin=True))
    assert resp.status_code == 400
    fields = resp.json()["error"]["fields"]
    assert {"revision", "id", "is_admin"} <= set(fields)
    assert not Event.objects.exists()


def test_type_errors_and_missing_required(client, form, content_key):
    resp = call(client, "POST", f"{BASE}/events", content_key, {"slug": 5, "learningOutcomes": "nope"})
    assert resp.status_code == 400
    fields = resp.json()["error"]["fields"]
    assert {"slug", "learningOutcomes", "internalName", "publicTitle", "formVersion"} <= set(fields)


def test_body_must_be_object(client, content_key):
    assert call(client, "POST", f"{BASE}/events", content_key, raw="[1]").status_code == 400
    assert call(client, "POST", f"{BASE}/events", content_key, raw="{bad").status_code == 400


def test_validation_reused(client, form, content_key):
    resp = call(
        client, "POST", f"{BASE}/events", content_key,
        event_body(form, heroImageUrl="http://insecure.example.com/x.png", thankYouMode="redirect",
                   thankYouUrl="https://evil.example.net/"),
    )
    assert resp.status_code == 400
    fields = resp.json()["error"]["fields"]
    assert "heroImageUrl" in fields
    resp = call(client, "POST", f"{BASE}/events", content_key,
                event_body(form, thankYouMode="redirect", thankYouUrl="https://evil.example.net/"))
    assert resp.status_code == 400
    resp = call(client, "POST", f"{BASE}/events", content_key, event_body(form, formVersion="missing"))
    assert resp.json()["error"]["fields"] == {"formVersion": "Unknown form version."}


def test_duplicate_slug_rejected(client, form, content_key):
    make_event(form, slug="q4-pipeline")
    resp = call(client, "POST", f"{BASE}/events", content_key, event_body(form))
    assert resp.status_code == 400
    assert "slug" in resp.json()["error"]["fields"]


def confirmed(client, method, path, key, body):
    """Publication two-step: 428 preview with confirmToken, then the identical request with it."""
    first = call(client, method, path, key, body)
    assert first.status_code == 428, first.content
    assert first.json()["error"]["code"] == "confirmation_required"
    return call(client, method, path, key, body, confirm=first.json()["confirmToken"])


def test_update_bumps_revision_and_publishes(client, form, content_key, publish_key):
    from webinars.models import Event, EventRevision, Host, Topic

    Host.objects.create(name="Ada", slug="ada")
    Topic.objects.create(name="Growth", slug="growth")
    event_id = call(client, "POST", f"{BASE}/events", content_key, event_body(form, action="Register now", attention="Hook")).json()["id"]
    make_occurrence(Event.objects.get(pk=event_id))  # readiness: an upcoming session
    body = {"visibility": "public", "hosts": ["ada"], "topics": ["growth"]}
    # content_write alone can no longer publish (publication scope + confirmation required).
    assert call(client, "PATCH", f"{BASE}/events/{event_id}", content_key, body).status_code == 403
    resp = confirmed(client, "PATCH", f"{BASE}/events/{event_id}", publish_key, body)
    assert resp.status_code == 200, resp.content
    data = resp.json()
    assert data["revision"] == 2 and data["hosts"] == ["ada"] and data["topics"] == ["growth"]
    assert EventRevision.objects.get(event_id=event_id, revision=2).snapshot["visibility"] == "public"
    # No-op PATCH (same visibility) needs no confirmation and does not create a revision.
    assert call(client, "PATCH", f"{BASE}/events/{event_id}", content_key, {"visibility": "public"}).json()["revision"] == 2
    # Unknown host slug rejected, nothing changes.
    resp = call(client, "PATCH", f"{BASE}/events/{event_id}", content_key, {"hosts": ["nobody"]})
    assert resp.status_code == 400
    assert client.get("/api/v1/events/q4-pipeline").status_code == 200


def test_unpublish_via_patch(client, form, content_key, publish_key):
    event = make_event(form, slug="live")
    assert call(client, "PATCH", f"{BASE}/events/{event.pk}", content_key, {"visibility": "draft"}).status_code == 403
    resp = confirmed(client, "PATCH", f"{BASE}/events/{event.pk}", publish_key, {"visibility": "draft"})
    assert resp.status_code == 200
    assert client.get("/api/v1/events/live").status_code == 404


def test_publication_preview_has_no_side_effects_and_token_is_bound(client, form, publish_key):
    from webinars.models import AuditLog, Event, EventRevision

    event = ready_draft(form, "gate")
    url = f"{BASE}/events/{event.pk}"
    audits, revisions = AuditLog.objects.count(), EventRevision.objects.count()
    first = call(client, "PATCH", url, publish_key, {"visibility": "public"})
    assert first.status_code == 428 and first.json()["preview"]["to"] == "public"
    assert (AuditLog.objects.count(), EventRevision.objects.count()) == (audits, revisions)
    assert Event.objects.get(pk=event.pk).visibility == "draft"
    token = first.json()["confirmToken"]
    # Different body, different key, garbage: rejected and nothing changes.
    assert call(client, "PATCH", url, publish_key, {"visibility": "unlisted"}, confirm=token).status_code == 400
    other = make_api_key(("content_write", "publication"), name="other")
    assert call(client, "PATCH", url, other, {"visibility": "public"}, confirm=token).status_code == 400
    assert call(client, "PATCH", url, publish_key, {"visibility": "public"}, confirm="x" * 40).status_code == 400
    assert Event.objects.get(pk=event.pk).visibility == "draft"
    assert call(client, "PATCH", url, publish_key, {"visibility": "public"}, confirm=token).status_code == 200
    assert Event.objects.get(pk=event.pk).visibility == "public"


def ready_draft(form, slug):
    event = make_event(form, slug=slug, visibility="draft", action="Register now")
    make_occurrence(event)
    return event


def test_publish_refuses_not_ready_candidate(client, form, publish_key):
    """Readiness is checked on the patched candidate, before any preview or token."""
    from webinars.models import Event

    event = ready_draft(form, "not-ready")
    url = f"{BASE}/events/{event.pk}"
    resp = call(client, "PATCH", url, publish_key, {"visibility": "public", "action": "", "attention": "", "summary": ""})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "not_ready"
    assert "confirmToken" not in resp.json()
    no_session = make_event(form, slug="no-session", visibility="draft", action="Go")
    resp = call(client, "PATCH", f"{BASE}/events/{no_session.pk}", publish_key, {"visibility": "unlisted"})
    assert resp.status_code == 409
    assert Event.objects.get(pk=event.pk).visibility == "draft"
    assert Event.objects.get(pk=no_session.pk).visibility == "draft"
    # Unpublishing needs no readiness.
    live = make_event(form, slug="live-empty", visibility="public")
    assert confirmed(client, "PATCH", f"{BASE}/events/{live.pk}", publish_key, {"visibility": "draft"}).status_code == 200


def test_publication_token_bound_to_state(client, form, publish_key, content_key):
    event = ready_draft(form, "gate-state")
    url = f"{BASE}/events/{event.pk}"
    token = call(client, "PATCH", url, publish_key, {"visibility": "public"}).json()["confirmToken"]
    # A content edit after the preview bumps the revision: the old preview no longer applies.
    assert call(client, "PATCH", url, content_key, {"summary": "Changed after preview"}).status_code == 200
    resp = call(client, "PATCH", url, publish_key, {"visibility": "public"}, confirm=token)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "confirmation_mismatch"


def test_delete_rules(client, form, content_key):
    from webinars.models import Event

    public = make_event(form, slug="pub")
    assert call(client, "DELETE", f"{BASE}/events/{public.pk}", content_key).status_code == 409
    draft_with_regs = make_event(form, slug="drafted", visibility="draft")
    make_registration(make_occurrence(draft_with_regs))
    assert call(client, "DELETE", f"{BASE}/events/{draft_with_regs.pk}", content_key).status_code == 409
    draft = make_event(form, slug="empty", visibility="draft")
    make_occurrence(draft)
    assert call(client, "DELETE", f"{BASE}/events/{draft.pk}", content_key).status_code == 204
    assert not Event.objects.filter(pk=draft.pk).exists()
    assert Event.objects.filter(pk__in=[public.pk, draft_with_regs.pk]).count() == 2


def test_missing_event_404(client, content_key):
    import uuid

    assert call(client, "GET", f"{BASE}/events/{uuid.uuid4()}", content_key).status_code == 404


# ---------------------------------------------------------------------------
# Hosts / topics / form versions
# ---------------------------------------------------------------------------


def test_host_topic_crud(client, content_key):
    resp = call(client, "POST", f"{BASE}/hosts", content_key, {"name": "Ada", "slug": "ada", "imageUrl": "http://x.example.com/a.png"})
    assert resp.status_code == 400 and "imageUrl" in resp.json()["error"]["fields"]
    assert call(client, "POST", f"{BASE}/hosts", content_key, {"name": "Ada", "slug": "ada"}).status_code == 201
    resp = call(client, "PATCH", f"{BASE}/hosts/ada", content_key, {"title": "CTO"})
    assert resp.json()["title"] == "CTO"
    assert call(client, "POST", f"{BASE}/hosts", content_key, {"name": "Dup", "slug": "ada"}).status_code == 400
    assert call(client, "POST", f"{BASE}/topics", content_key, {"name": "Growth", "slug": "growth"}).status_code == 201
    assert call(client, "PATCH", f"{BASE}/topics/growth", content_key, {"bogus": 1}).status_code == 400
    assert call(client, "PATCH", f"{BASE}/topics/missing", content_key, {"name": "x"}).status_code == 404


def test_form_versions_create_and_immutable(client, content_key):
    body = {"version": "f2", "requiredFields": ["email"], "optionalFields": ["company"], "fieldMapping": {"work_email": "email"}}
    assert call(client, "POST", f"{BASE}/forms", content_key, body).status_code == 201
    assert call(client, "POST", f"{BASE}/forms", content_key, body).status_code == 409
    bad = {"version": "f3", "requiredFields": ["emailAddress"]}
    assert call(client, "POST", f"{BASE}/forms", content_key, bad).status_code == 400
    resp = call(client, "PATCH", f"{BASE}/forms/f2", content_key, {"requiredFields": ["email", "phone"]})
    assert resp.status_code == 400
    resp = call(client, "PATCH", f"{BASE}/forms/f2", content_key, {"isActive": False})
    assert resp.status_code == 200 and resp.json()["isActive"] is False


# ---------------------------------------------------------------------------
# Occurrences
# ---------------------------------------------------------------------------


def _future_local(days=20):
    return (timezone.now() + timedelta(days=days)).strftime("%Y-%m-%dT12:00")


def test_create_occurrence_no_private_links_in_responses(client, event, content_key):
    body = {"timezone": "Europe/London", "localStart": _future_local(), "joinUrl": JOIN_URL, "waitingRoomUrl": WAITING_URL}
    resp = call(client, "POST", f"{BASE}/events/{event.pk}/occurrences", content_key, body)
    assert resp.status_code == 201, resp.content
    data = resp.json()
    assert data["hasJoinUrl"] is True and data["icsSequence"] == 0
    for r in (resp, call(client, "GET", f"{BASE}/events/{event.pk}", content_key),
              client.get(f"/api/v1/events/{event.slug}")):
        assert JOIN_URL not in r.content.decode() and WAITING_URL not in r.content.decode()


def test_occurrence_dst_validation(client, event, content_key):
    url = f"{BASE}/events/{event.pk}/occurrences"
    gap = {"timezone": "Europe/London", "localStart": "2030-03-31T01:30"}
    assert call(client, "POST", url, content_key, gap).status_code == 400
    fold = {"timezone": "Europe/London", "localStart": "2030-10-27T01:30"}
    resp = call(client, "POST", url, content_key, fold)
    assert resp.status_code == 400 and "localStart" in resp.json()["error"]["fields"]
    resp = call(client, "POST", url, content_key, {**fold, "dstFold": 1})
    assert resp.status_code == 201
    assert resp.json()["startsAt"] == "2030-10-27T01:30:00Z"
    for bad in ({"timezone": "Mars/Base", "localStart": _future_local()},
                {"timezone": "UTC", "localStart": "2030-01-01T10:00+02:00"},
                {"timezone": "UTC", "localStart": _future_local(), "joinUrl": "http://x.example.com"},
                {"timezone": "UTC", "localStart": _future_local(), "dstFold": 2},
                {"timezone": "UTC", "localStart": _future_local(), "recordingUrl": "https://x.example.com"}):
        assert call(client, "POST", url, content_key, bad).status_code == 400, bad


def test_reschedule_supersedes_reminders_and_notifies(client, occurrence, content_key):
    from webinars.models import OutboxMessage, ReminderSchedule
    from webinars.scheduling import create_reminders

    make_destination(event_types=("occurrence.rescheduled",))
    reg = make_registration(occurrence)
    create_reminders(reg)
    assert ReminderSchedule.objects.filter(registration=reg, status="pending").count() == 2
    resp = call(client, "PATCH", f"{BASE}/occurrences/{occurrence.pk}", content_key,
                {"localStart": _future_local(30), "durationMinutes": 90})
    assert resp.status_code == 200, resp.content
    data = resp.json()
    assert data["icsSequence"] == 1 and data["scheduleGeneration"] == 2 and data["durationMinutes"] == 90
    assert ReminderSchedule.objects.filter(registration=reg, generation=1, status="superseded").count() == 2
    assert ReminderSchedule.objects.filter(registration=reg, generation=2, status="pending").count() == 2
    assert OutboxMessage.objects.filter(event_type="occurrence.rescheduled").count() == 1


def test_non_time_update_does_not_reschedule(client, occurrence, content_key):
    resp = call(client, "PATCH", f"{BASE}/occurrences/{occurrence.pk}", content_key, {"registrationOpen": False})
    assert resp.status_code == 200
    assert resp.json()["registrationOpen"] is False and resp.json()["icsSequence"] == 0


def test_cancel_suppresses_and_blocks_changes(client, occurrence, content_key, publish_key):
    from webinars.models import OutboxMessage, ReminderSchedule
    from webinars.scheduling import create_reminders

    make_destination(event_types=("occurrence.cancelled",))
    reg = make_registration(occurrence)
    create_reminders(reg)
    resp = call(client, "POST", f"{BASE}/occurrences/{occurrence.pk}/cancel", content_key, {})
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert ReminderSchedule.objects.filter(registration=reg, status="suppressed").count() == 2
    # Idempotent: no second notification or sequence bump.
    again = call(client, "POST", f"{BASE}/occurrences/{occurrence.pk}/cancel", content_key, {})
    assert again.json()["icsSequence"] == resp.json()["icsSequence"]
    assert OutboxMessage.objects.filter(event_type="occurrence.cancelled").count() == 1
    resp = call(client, "PATCH", f"{BASE}/occurrences/{occurrence.pk}", content_key, {"localStart": _future_local(40)})
    assert resp.status_code == 409
    resp = call(client, "POST", f"{BASE}/occurrences/{occurrence.pk}/recording", publish_key,
                {"recordingUrl": "https://media.example.com/r"})
    assert resp.status_code == 409


def test_publish_recording(client, occurrence, content_key, publish_key):
    from webinars.models import OutboxMessage

    make_destination(event_types=("recording.published",))
    make_registration(occurrence)
    url = f"{BASE}/occurrences/{occurrence.pk}/recording"
    # Recordings are a publication path: content_write alone is refused.
    assert call(client, "POST", url, content_key, {"recordingUrl": "https://media.example.com/r"}).status_code == 403
    assert call(client, "POST", url, publish_key, {"recordingUrl": "http://media.example.com/r"}).status_code == 400
    assert call(client, "POST", url, publish_key, {"recordingUrl": "https://media.example.com/r", "x": 1}).status_code == 400
    resp = confirmed(client, "POST", url, publish_key, {"recordingUrl": "https://media.example.com/r"})
    assert resp.status_code == 200
    assert resp.json()["recordingUrl"] == "https://media.example.com/r"
    assert OutboxMessage.objects.filter(event_type="recording.published").count() == 1


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


def test_attendance_requires_started_occurrence(client, occurrence, attendance_key):
    reg = make_registration(occurrence)
    resp = call(client, "POST", f"{BASE}/registrations/{reg.pk}/attendance", attendance_key, {"kind": "attended"})
    assert resp.status_code == 409


def test_attendance_recorded_without_pii(client, event, attendance_key):
    from webinars.models import AttendanceEvent, AuditLog

    past = make_occurrence(event, starts_at=timezone.now() - timedelta(hours=2))
    reg = make_registration(past)
    url = f"{BASE}/registrations/{reg.pk}/attendance"
    assert call(client, "POST", url, attendance_key, {"kind": "slept"}).status_code == 400
    assert call(client, "POST", url, attendance_key, {"kind": "attended", "personId": "x"}).status_code == 400
    resp = call(client, "POST", url, attendance_key, {"kind": "no_show"})
    assert resp.status_code == 200
    assert resp.json() == {"registrationId": str(reg.pk), "occurrenceId": str(past.pk), "attendance": "no_show"}
    assert reg.person.email not in resp.content.decode()
    assert AttendanceEvent.objects.filter(registration=reg, kind="no_show", source="api").count() == 1
    log = AuditLog.objects.get(action="attendance.api_write")
    assert log.actor_api_key is not None
    assert reg.person.email not in json.dumps(log.metadata)


def test_attendance_key_cannot_write_content(client, event, attendance_key):
    resp = call(client, "PATCH", f"{BASE}/events/{event.pk}", attendance_key, {"visibility": "draft"})
    assert resp.status_code == 403
