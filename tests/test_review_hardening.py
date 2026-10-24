"""Regression tests for continuation review findings F1 and F3
(.runs/continuation/review-result.md).

F1: Django admin publication readiness must hold for the sessions as submitted in the same
    change form (OccurrenceInline), not only the sessions stored before the POST.
F3: the proxy-target ``/api/v1/my-registration`` shares the direct route's per-client limit
    when the proxy's X-Forwarded-For address is trusted.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from tests.conftest import make_api_key, make_event, make_occurrence
from webinars.models import Event, Occurrence

pytestmark = pytest.mark.django_db

NOT_READY = "The event is not ready to publish."
NO_UPCOMING = "Schedule at least one upcoming session. These session changes leave none in the future."


# ---------------------------------------------------------------------------
# F1: admin readiness with simultaneous inline session changes
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client(client, django_user_model):
    user = django_user_model.objects.create_superuser("hardening", "hardening@example.com", "x-Long-pass-Harbor-9")
    client.force_login(user)
    return client


def _change_url(event):
    return f"/admin/webinars/event/{event.pk}/change/"


def _local_parts(occ, *, days_from_now=None):
    """(date, time) admin split-datetime values: the stored wall clock, or noon local ``days_from_now``."""
    if days_from_now is None:
        local = occ.local_start.replace(tzinfo=None)
    else:
        day = (timezone.now() + timedelta(days=days_from_now)).astimezone(ZoneInfo(occ.timezone)).date()
        local = datetime(day.year, day.month, day.day, 12, 0)
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")


def _post_change(client, event, occ, *, session_days=None, others=(), **changes):
    """POST the admin change form for ``event`` with session ``occ`` (and ``others``) inline.

    ``session_days`` reschedules ``occ`` to noon local that many days from now (negative is
    in the past); ``others`` are posted unchanged. Other keyword arguments override event
    fields.
    """
    from webinars.services import event_form_data

    data = {k: v for k, v in event_form_data(event).items() if v != []}
    data.update(changes)
    sessions = [(occ, session_days)] + [(o, None) for o in others]
    for i, (session, days) in enumerate(sessions):
        date_part, time_part = _local_parts(session, days_from_now=days)
        prefix = f"occurrences-{i}-"
        data.update(
            {
                prefix + "id": str(session.pk),
                prefix + "event": str(event.pk),
                prefix + "timezone": session.timezone,
                prefix + "local_start_0": date_part,
                prefix + "local_start_1": time_part,
                prefix + "dst_fold": "",
                prefix + "duration_minutes": str(session.duration_minutes),
                prefix + "series": "",
                prefix + "registration_open": "on",
            }
        )
    data.update(
        {
            "occurrences-TOTAL_FORMS": str(len(sessions)),
            "occurrences-INITIAL_FORMS": str(len(sessions)),
            "occurrences-MIN_NUM_FORMS": "0",
            "occurrences-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }
    )
    return client.post(_change_url(event), data)


def _inline_errors(response):
    formset = response.context["inline_admin_formsets"][0].formset
    return list(formset.non_form_errors())


def test_event_inline_uses_readiness_formset():
    from django.contrib import admin

    from webinars.admin import OccurrenceInline, OccurrenceInlineFormSet

    assert OccurrenceInline.formset is OccurrenceInlineFormSet
    assert OccurrenceInline in admin.site._registry[Event].inlines


def test_publish_and_move_only_session_into_past_is_rejected_and_rolled_back(admin_client, form):
    draft = make_event(form, slug="f1-past", visibility="draft", action="Register now")
    occ = make_occurrence(draft)
    before = Occurrence.objects.get(pk=occ.pk)

    resp = _post_change(admin_client, draft, occ, visibility="public", session_days=-2)

    assert resp.status_code == 200  # change form re-rendered, not redirected
    errors = _inline_errors(resp)
    assert NOT_READY in errors and NO_UPCOMING in errors
    draft.refresh_from_db()
    assert draft.visibility == "draft"
    after = Occurrence.objects.get(pk=occ.pk)
    assert (after.starts_at, after.ics_sequence, after.schedule_generation) == (
        before.starts_at,
        before.ics_sequence,
        before.schedule_generation,
    )


def test_rejected_publish_does_not_bump_revision(admin_client, form):
    draft = make_event(form, slug="f1-revision", visibility="draft", action="Register now")
    occ = make_occurrence(draft)
    revision = Event.objects.get(pk=draft.pk).revision

    resp = _post_change(admin_client, draft, occ, visibility="unlisted", session_days=-1)

    assert resp.status_code == 200
    assert Event.objects.get(pk=draft.pk).revision == revision


def test_publish_with_future_reschedule_is_allowed(admin_client, form):
    draft = make_event(form, slug="f1-future", visibility="draft", action="Register now")
    occ = make_occurrence(draft, days=10)

    resp = _post_change(admin_client, draft, occ, visibility="public", session_days=20)

    assert resp.status_code == 302
    draft.refresh_from_db()
    assert draft.visibility == "public"
    assert Occurrence.objects.get(pk=occ.pk).starts_at > timezone.now() + timedelta(days=15)


def test_publish_with_unchanged_upcoming_session_is_allowed(admin_client, form):
    draft = make_event(form, slug="f1-unchanged", visibility="draft", action="Register now")
    occ = make_occurrence(draft)

    resp = _post_change(admin_client, draft, occ, visibility="public")

    assert resp.status_code == 302
    assert Event.objects.get(pk=draft.pk).visibility == "public"


def test_publish_keeps_other_upcoming_session(admin_client, form):
    # Moving one session into the past is fine while another stays upcoming.
    draft = make_event(form, slug="f1-other", visibility="draft", action="Register now")
    occ = make_occurrence(draft, days=5)
    other = make_occurrence(draft, days=30)

    resp = _post_change(admin_client, draft, occ, visibility="public", session_days=-2, others=[other])

    assert resp.status_code == 302
    assert Event.objects.get(pk=draft.pk).visibility == "public"


def test_publish_with_only_cancelled_future_session_is_rejected(admin_client, form):
    # A cancelled session in the future does not count (same rule as publish_problems).
    draft = make_event(form, slug="f1-cancelled", visibility="draft", action="Register now")
    occ = make_occurrence(draft, days=5)
    cancelled = make_occurrence(draft, days=30, status=Occurrence.Status.CANCELLED)

    resp = _post_change(admin_client, draft, occ, visibility="public", session_days=-2, others=[cancelled])

    assert resp.status_code == 200
    assert NO_UPCOMING in _inline_errors(resp)
    assert Event.objects.get(pk=draft.pk).visibility == "draft"


def test_already_public_event_may_still_reschedule_into_past(admin_client, form):
    # No visibility transition: same rule as REST/MCP, where cancelling or moving the
    # last session after publishing is allowed.
    live = make_event(form, slug="f1-live", visibility="public", action="Register now")
    occ = make_occurrence(live)

    resp = _post_change(admin_client, live, occ, session_days=-2)

    assert resp.status_code == 302
    assert Occurrence.objects.get(pk=occ.pk).starts_at < timezone.now()


def test_unpublish_with_past_reschedule_is_allowed(admin_client, form):
    live = make_event(form, slug="f1-unpublish", visibility="public", action="Register now")
    occ = make_occurrence(live)

    resp = _post_change(admin_client, live, occ, visibility="draft", session_days=-2)

    assert resp.status_code == 302
    assert Event.objects.get(pk=live.pk).visibility == "draft"


def test_content_problems_still_reported_on_visibility(admin_client, form):
    # The parent form's publish_problems check is unchanged and still shown on the field.
    draft = make_event(form, slug="f1-content", visibility="draft", action="")
    occ = make_occurrence(draft)

    resp = _post_change(admin_client, draft, occ, visibility="public")

    assert resp.status_code == 200
    visibility_errors = resp.context["adminform"].form.errors["visibility"]
    assert "Write the Action copy: what should visitors do?" in visibility_errors
    assert Event.objects.get(pk=draft.pk).visibility == "draft"


# ---------------------------------------------------------------------------
# F3: proxy-target my-registration rate limiting
# ---------------------------------------------------------------------------

API_MY = "/api/v1/my-registration"


def _api_my(client, occurrence, key, xff="203.0.113.5"):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if xff:
        headers["X-Forwarded-For"] = xff
    return client.get(API_MY, {"occurrenceId": str(occurrence.pk)}, headers=headers)


@pytest.fixture
def limited(settings, monkeypatch):
    from webinars.api import views

    settings.RATE_LIMIT_ENABLED = True
    settings.RATE_LIMIT_TRUST_X_FORWARDED_FOR = True
    settings.RATE_LIMIT_TRUSTED_PROXY_COUNT = 1
    monkeypatch.setattr(views, "MY_REGISTRATION_LIMIT", (2, 60))
    return settings


def test_api_my_registration_rate_limited_per_forwarded_client(client, limited, occurrence):
    key = make_api_key(("proxy",))
    assert _api_my(client, occurrence, key).status_code == 200
    assert _api_my(client, occurrence, key).status_code == 200
    resp = _api_my(client, occurrence, key)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    assert resp["Cache-Control"] == "no-store"
    # A different browser behind the same proxy has its own bucket.
    assert _api_my(client, occurrence, key, xff="198.51.100.7").status_code == 200


def test_api_my_registration_not_limited_without_trusted_forwarded_for(client, limited, occurrence):
    # Without XFF trust every proxied request has the proxy's address: one shared budget
    # would throttle all browsers, so the backend leaves this route to edge limits.
    limited.RATE_LIMIT_TRUST_X_FORWARDED_FOR = False
    key = make_api_key(("proxy",))
    for _ in range(5):
        assert _api_my(client, occurrence, key).status_code == 200


def test_api_my_registration_auth_failures_not_counted(client, limited, occurrence):
    key = make_api_key(("proxy",))
    wrong_scope = make_api_key(("register",), name="register")
    for _ in range(3):
        assert _api_my(client, occurrence, None).status_code == 401
        assert _api_my(client, occurrence, wrong_scope).status_code == 403
    assert _api_my(client, occurrence, key).status_code == 200


def test_api_my_registration_respects_rate_limit_disabled(client, limited, occurrence):
    limited.RATE_LIMIT_ENABLED = False
    key = make_api_key(("proxy",))
    for _ in range(5):
        assert _api_my(client, occurrence, key).status_code == 200


def test_direct_my_registration_limit_unchanged(client, limited, occurrence):
    from tests.conftest import ORIGIN

    limited.RATE_LIMIT_TRUST_X_FORWARDED_FOR = False
    headers = {"Origin": ORIGIN, "X-Webinar-Request": "1", "Sec-Fetch-Site": "same-origin"}
    url = "/webinar-api/my-registration"
    params = {"occurrenceId": str(occurrence.pk)}
    assert client.get(url, params, headers=headers).status_code == 200
    assert client.get(url, params, headers=headers).status_code == 200
    assert client.get(url, params, headers=headers).status_code == 429
