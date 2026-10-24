"""Studio content area: sign-in, dashboard, events, sessions, forms, hosts, reports.

Every view is wrapped with ``studio_view`` (or is the public login page). All writes
go through ``webinars.services`` / ``webinars.waiting_room`` / ``webinars.scheduling``.
The occurrence ``join_url`` is only ever shown inside the studio session editor
to users with ``webinars.change_occurrence``, and never in JSON or public pages.
"""
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from webinars import covers, ratelimit, samples, services, waiting_room
from webinars.admin import HostForm
from webinars.audit import audit
from webinars.forms_schema import CUSTOM_TYPES, DEFAULT_LABELS, public_schema, rows_for
from webinars.models import (
    DEFAULT_CONFIRM_LABEL,
    DEFAULT_REGISTER_LABEL,
    ApiKey,
    CheckIn,
    Destination,
    Event,
    FormVersion,
    Host,
    Invitation,
    Occurrence,
    OutboxMessage,
    Registration,
    Topic,
)
from webinars.normalization import CANONICAL_FIELDS, CUSTOM_PREFIX
from webinars.reporting import report_summary
from webinars.scheduling import cancel_occurrence, publish_recording, record_attendance
from webinars.services import FormErrors
from webinars.studio.access import can, forbidden, no_store, render, studio_view
from webinars.studio.forms_content import TopicForm

COMMON_TIMEZONES = [
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "America/Toronto",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Amsterdam",
    "Europe/Madrid",
    "Europe/Stockholm",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
    "Pacific/Auckland",
    "UTC",
]

EVENT_LABELS = {
    "public_title": "Public title",
    "slug": "URL slug",
    "internal_name": "Internal name",
    "summary": "Summary",
    "hero_image_url": "Hero image URL",
    "cover_upload": "Upload a cover image",
    "register_label": "Button text",
    "confirm_label": "Button text for returning visitors",
    "attention": "Attention",
    "interest": "Interest",
    "decision": "Decision",
    "action": "Action",
    "learning_outcomes": "Learning outcomes",
    "hosts": "Hosts",
    "topics": "Topics",
    "form_version": "Registration form",
    "thank_you_mode": "After registering",
    "thank_you_message": "Thank-you message",
    "thank_you_url": "Redirect URL",
    "visibility": "Visibility",
}
SESSION_LABELS = {
    "timezone": "Time zone",
    "local_start": "Start (local time)",
    "dst_fold": "Repeated hour",
    "duration_minutes": "Duration",
    "join_url": "Join URL",
    "registration_open": "Registration open",
    "recording_url": "Recording URL",
    "event": "Event",
    "series": "Series",
    "waiting_room_url": "Provider waiting room URL",
}
FORM_LABELS = {
    "version": "Version name",
    "description": "Description",
    "default_phone_region": "Default phone region",
    "sms_disclosure_version": "SMS disclosure version",
    "sms_disclosure_text": "SMS disclosure text",
    "field_mapping": "Field mapping",
}
ADMISSION_CONTROLS = [
    ("auto", "Automatic", "clock", "Doors open on schedule."),
    ("hold", "Hold", "pause", "Keep everyone waiting, even after the start time."),
    ("open", "Open now", "play", "Let registered attendees in immediately."),
    ("ended", "End", "stop", "Close the room and finalize attendance."),
]
ROOM_STATE_LABELS = {
    waiting_room.WAITING: "Waiting",
    waiting_room.OPEN: "Open",
    waiting_room.HOLD: "On hold",
    waiting_room.ENDED: "Ended",
    waiting_room.CANCELLED: "Cancelled",
    waiting_room.PREPARING: "Needs join URL",
}
MASK = "•••"
OUTCOME_ROWS = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_reverse(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def _errors(exc):
    """``{field: [messages]}`` from FormErrors / dict ValidationError / plain ValidationError."""
    if isinstance(exc, FormErrors):
        return {str(k): [str(m) for m in v] for k, v in exc.form.errors.items()}
    if hasattr(exc, "error_dict"):
        return {k if isinstance(k, int) else str(k): list(v) for k, v in exc.message_dict.items()}
    return {"__all__": list(exc.messages)}


def _summary(errors, labels, prefix="id_"):
    """Error summary entries ``[{href, label, message}]``; non-field errors have no href."""
    items = []
    for key, msgs in errors.items():
        for msg in msgs:
            if key in labels:
                items.append({"href": f"#{prefix}{key}", "label": labels[key], "message": msg})
            else:
                items.append({"href": "", "label": "", "message": msg})
    return items


def _flash_validation(request, exc):
    for msgs in _errors(exc).values():
        for msg in msgs:
            messages.error(request, msg)


def _local_naive(occ):
    value = occ.local_start
    return timezone.make_naive(value, dt_timezone.utc) if timezone.is_aware(value) else value


def _decorate_occurrence(occ, now=None):
    """Attach display helpers (local label, room state) to an occurrence."""
    local = _local_naive(occ)
    occ.local_label = f"{local:%a %d %b %Y, %H:%M}"
    if occ.waiting_room_enabled:
        state = waiting_room.room_state(occ, now).state
        occ.room_state = state
        occ.room_state_label = ROOM_STATE_LABELS.get(state, state)
    else:
        occ.room_state = None
        occ.room_state_label = ""
    return occ


def _timezones(current=None):
    zones = list(COMMON_TIMEZONES)
    if current and current not in zones:
        zones.insert(0, current)
    return zones


# ---------------------------------------------------------------------------
# Sign in / sign out
# ---------------------------------------------------------------------------


class StudioLoginView(LoginView):
    template_name = "studio/login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        if not ratelimit.hit(request, "studio_login"):
            # Do not run authenticate(): a rate-limited request never checks a password.
            context = self.get_context_data(form=AuthenticationForm(request), rate_limited=True)
            return self.render_to_response(context, status=429)
        return super().post(request, *args, **kwargs)


class StudioLogoutView(LogoutView):
    http_method_names = ["post", "options"]


login_view = StudioLoginView.as_view()
logout_view = StudioLogoutView.as_view()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@studio_view(methods=("GET",))
def dashboard(request):
    user = request.user
    now = timezone.now()
    User = get_user_model()
    steps = [
        {
            "title": "Set up a registration form",
            "detail": "Choose the fields people fill in when they register.",
            "done": FormVersion.objects.exists(),
            "url": _safe_reverse("studio:forms"),
        },
        {
            "title": "Create your first event",
            "detail": "Start from a template; every word is editable.",
            "done": Event.objects.exists(),
            "url": _safe_reverse("studio:event-new"),
        },
        {
            "title": "Schedule a session",
            "detail": "Add a date, time zone and the join link for your webinar tool.",
            "done": Occurrence.objects.exists(),
            "url": _safe_reverse("studio:events"),
        },
        {
            "title": "Publish an event",
            "detail": "Make the landing page public or unlisted when it is ready.",
            "done": Event.objects.exclude(visibility=Event.Visibility.DRAFT).exists(),
            "url": _safe_reverse("studio:events"),
        },
        {
            "title": "Connect an integration",
            "detail": "Send registrations to your email platform or CRM.",
            "done": Destination.objects.exists(),
            "url": _safe_reverse("studio:integrations"),
        },
        {
            "title": "Create an API key",
            "detail": "For your website or agents that read and write events.",
            "done": ApiKey.objects.filter(revoked_at__isnull=True).exists(),
            "url": _safe_reverse("studio:api-keys"),
        },
        {
            "title": "Invite a teammate",
            "detail": "Give editors and analysts their own sign-in.",
            "done": Invitation.objects.exists() or User.objects.filter(is_active=True).count() > 1,
            "url": _safe_reverse("studio:team"),
        },
    ]
    done = sum(1 for s in steps if s["done"])
    upcoming = list(
        Occurrence.objects.select_related("event")
        .filter(status=Occurrence.Status.SCHEDULED, ends_at__gt=now)
        .annotate(registered=Count("registrations", filter=Q(registrations__status=Registration.Status.REGISTERED)))
        .order_by("starts_at")[:8]
    )
    for occ in upcoming:
        _decorate_occurrence(occ, now)
    stats = {
        "events": Event.objects.count(),
        "published": Event.objects.exclude(visibility=Event.Visibility.DRAFT).count(),
        "upcoming": Occurrence.objects.filter(status=Occurrence.Status.SCHEDULED, starts_at__gt=now).count(),
        "registrations": Registration.objects.filter(status=Registration.Status.REGISTERED).count()
        if user.has_perm("webinars.view_registration")
        else None,
    }
    return render(
        request,
        "studio/content/dashboard.html",
        {
            "steps": steps,
            "steps_done": done,
            "steps_total": len(steps),
            "upcoming": upcoming,
            "stats": stats,
            "can_add_event": can(user, "webinars.add_event"),
        },
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@studio_view(perm="webinars.view_event", methods=("GET",))
def events(request):
    now = timezone.now()
    rows = list(
        Event.objects.annotate(
            session_count=Count("occurrences", distinct=True),
        ).order_by("-created_at")
    )
    next_sessions = {}
    for occ in (
        Occurrence.objects.filter(status=Occurrence.Status.SCHEDULED, starts_at__gt=now, event__in=rows)
        .order_by("starts_at")
    ):
        next_sessions.setdefault(occ.event_id, _decorate_occurrence(occ, now))
    for event in rows:
        event.next_session = next_sessions.get(event.pk)
    return render(
        request,
        "studio/content/events.html",
        {"events": rows, "can_add_event": can(request.user, "webinars.add_event")},
    )


def _unique_slug(base):
    base = (base or "event")[:150]
    slug, n = base, 2
    while Event.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _default_form_version(user):
    fv = FormVersion.objects.filter(is_active=True).order_by("created_at").first()
    if fv is not None:
        return fv
    fv = FormVersion.objects.filter(version=samples.DEFAULT_FORM_VERSION).first()
    if fv is not None:
        return fv
    fv, _ = services.save_form_version(
        samples.DEFAULT_FORM_ROWS,
        {"description": samples.DEFAULT_FORM_DESCRIPTION, "is_active": True},
        user=user,
        new_version=samples.DEFAULT_FORM_VERSION,
    )
    return fv


def _starter_cards():
    cards = []
    for key in samples.STARTER_ORDER:
        starter = samples.STARTERS[key]
        cards.append(
            {
                "key": key,
                "label": starter["label"],
                "description": starter["description"],
                "title": starter["public_title"],
                "duration": starter["duration_minutes"],
            }
        )
    return cards


@studio_view(perm="webinars.add_event", write_perm="webinars.add_event")
def event_new(request):
    key = (request.POST.get("starter") if request.method == "POST" else request.GET.get("starter")) or ""
    starter = samples.STARTERS.get(key)
    if request.method == "GET" and not key:
        return render(request, "studio/content/event_new.html", {"cards": _starter_cards(), "choosing": True})
    if key != "blank" and starter is None:
        if request.method == "GET":
            return redirect("studio:event-new")
        key = "blank"

    if request.method == "GET":
        values = {
            "public_title": starter["public_title"] if starter else "",
            "internal_name": starter["internal_name"] if starter else "",
            "slug": _unique_slug(starter["slug"]) if starter else "",
        }
        return render(
            request,
            "studio/content/event_new.html",
            {"choosing": False, "starter_key": key, "starter": starter, "values": values, "errors": {}},
        )

    values = {
        "public_title": request.POST.get("public_title", "").strip(),
        "internal_name": request.POST.get("internal_name", "").strip(),
        "slug": request.POST.get("slug", "").strip(),
    }
    data = {
        "internal_name": values["internal_name"] or values["public_title"],
        "slug": values["slug"] or slugify(values["public_title"])[:160],
        "public_title": values["public_title"],
        "visibility": Event.Visibility.DRAFT,
        "thank_you_mode": Event.ThankYouMode.MESSAGE,
        "thank_you_url": "",
        "thank_you_message": "You're registered. We'll email your calendar invite shortly.",
        "hosts": [],
        "topics": [],
        "learning_outcomes": [],
    }
    if starter:
        for field in ("summary", "attention", "interest", "decision", "action", "thank_you_message"):
            data[field] = starter[field]
        data["learning_outcomes"] = list(starter["learning_outcomes"])
    try:
        with transaction.atomic():
            fv = _default_form_version(request.user)
            data["form_version"] = fv.pk
            if starter:
                host_name, host_slug, host_title = starter["host"]
                host, host_created = Host.objects.get_or_create(
                    slug=host_slug, defaults={"name": host_name, "title": host_title}
                )
                if host_created:
                    audit("host.saved", user=request.user, obj=host, created=True)
                topic_name, topic_slug = starter["topic"]
                topic, topic_created = Topic.objects.get_or_create(slug=topic_slug, defaults={"name": topic_name})
                if topic_created:
                    audit("topic.saved", user=request.user, obj=topic, created=True)
                data["hosts"] = [host.pk]
                data["topics"] = [topic.pk]
            event, _ = services.save_event(data, user=request.user)
    except ValidationError as exc:
        errors = _errors(exc)
        non_field = errors.pop("__all__", [])
        return render(
            request,
            "studio/content/event_new.html",
            {
                "choosing": False,
                "starter_key": key,
                "starter": starter,
                "values": values,
                "errors": errors,
                "non_field_errors": non_field,
                "error_summary": _summary(errors, EVENT_LABELS) + [{"href": "", "label": "", "message": m} for m in non_field],
            },
            status=400,
        )
    messages.success(request, "Draft created. Review the copy, then schedule a session.")
    url = reverse("studio:event-edit", args=[event.pk])
    if starter:
        url += f"?starter={key}"
    return redirect(url)


def _event_values_from_event(event):
    data = services.event_form_data(event)
    data["learning_outcomes"] = list(event.learning_outcomes or [])
    data["hosts"] = [str(pk) for pk in data["hosts"]]
    data["topics"] = [str(pk) for pk in data["topics"]]
    data["form_version"] = str(data["form_version"] or "")
    return data


def _event_values_from_post(post, event):
    fields = (
        "internal_name", "slug", "public_title", "summary", "hero_image_url", "attention", "interest",
        "decision", "action", "form_version", "thank_you_mode", "thank_you_url", "thank_you_message",
        "register_label", "confirm_label",
    )
    values = {f: post.get(f, "") for f in fields}
    values["learning_outcomes"] = [o for o in post.getlist("learning_outcomes")]
    values["hosts"] = post.getlist("hosts")
    values["topics"] = post.getlist("topics")
    values["visibility"] = event.visibility
    return values


def _session_defaults(event, starter_key=None):
    starter = samples.STARTERS.get(starter_key or "")
    tz = starter["timezone"] if starter else "America/New_York"
    days = starter["days"] if starter else 7
    hour = starter["hour"] if starter else 14
    day = datetime.now(ZoneInfo(tz)).date() + timedelta(days=days)
    return {
        "timezone": tz,
        "local_start": f"{day:%Y-%m-%d}T{hour:02d}:00",
        "duration_minutes": str(starter["duration_minutes"] if starter else 60),
        "join_url": "",
    }


def _render_editor(request, event, *, values=None, errors=None, session_values=None, session_errors=None, status=200):
    user = request.user
    now = timezone.now()
    can_change = can(user, "webinars.change_event")
    values = values or _event_values_from_event(event)
    errors = dict(errors or {})
    non_field = errors.pop("__all__", [])
    outcomes = [o for o in values.get("learning_outcomes", [])]
    outcomes = outcomes + [""] * max(0, OUTCOME_ROWS - len(outcomes))
    selected_hosts = set(values.get("hosts", []))
    selected_topics = set(values.get("topics", []))
    hosts = [{"pk": h.pk, "name": h.name, "title": h.title, "checked": str(h.pk) in selected_hosts} for h in Host.objects.all()]
    topics = [{"pk": t.pk, "name": t.name, "checked": str(t.pk) in selected_topics} for t in Topic.objects.all()]
    form_versions = [
        {"pk": fv.pk, "label": fv.version + (f" — {fv.description}" if fv.description else ""),
         "selected": str(fv.pk) == str(values.get("form_version", "")), "active": fv.is_active}
        for fv in FormVersion.objects.filter(Q(is_active=True) | Q(pk=event.form_version_id)).order_by("-created_at")
    ]
    sessions = [
        _decorate_occurrence(o, now)
        for o in event.occurrences.annotate(
            registered=Count("registrations", filter=Q(registrations__status=Registration.Status.REGISTERED))
        ).order_by("starts_at")
    ]
    session_errors = dict(session_errors or {})
    session_non_field = session_errors.pop("__all__", [])
    if session_values is None:
        session_values = _session_defaults(event, request.GET.get("starter") if not sessions else None)
    error_summary = _summary(errors, EVENT_LABELS) + [{"href": "", "label": "", "message": m} for m in non_field]
    session_summary = _summary(session_errors, SESSION_LABELS, prefix="id_session_") + [
        {"href": "", "label": "", "message": m} for m in session_non_field
    ]
    return render(
        request,
        "studio/content/event_edit.html",
        {
            "event": event,
            "values": values,
            "errors": errors,
            "error_summary": error_summary,
            "outcomes": outcomes,
            "hosts": hosts,
            "topics": topics,
            "form_versions": form_versions,
            "sessions": sessions,
            "problems": services.publish_problems(event),
            "can_change": can_change,
            "readonly": not can_change,
            "can_delete": can(user, "webinars.delete_event") and event.visibility == Event.Visibility.DRAFT,
            "can_add_session": can(user, "webinars.add_occurrence"),
            "session_values": session_values,
            "session_errors": session_errors,
            "session_summary": session_summary,
            "timezones": _timezones(session_values.get("timezone")),
            "api_path": f"/api/v1/events/{event.slug}",
            "cover": event.cover_image,
            "cover_max_mb": covers.MAX_UPLOAD_BYTES // (1024 * 1024),
            "cover_max_side": covers.MAX_DIMENSION,
            "default_register_label": DEFAULT_REGISTER_LABEL,
            "default_confirm_label": DEFAULT_CONFIRM_LABEL,
        },
        status=status,
    )


@studio_view(perm="webinars.view_event", write_perm="webinars.change_event")
def event_edit(request, pk):
    event = get_object_or_404(Event.objects.select_related("form_version", "cover_image"), pk=pk)
    if request.method == "GET":
        return _render_editor(request, event)
    values = _event_values_from_post(request.POST, event)
    upload = request.FILES.get("cover_upload")
    remove_cover = "remove_cover" in request.POST
    cover_error = None
    if getattr(request, "upload_too_large", False):
        cover_error = f"Images must be {covers.MAX_UPLOAD_BYTES // (1024 * 1024)} MB or smaller."
    elif upload and values["hero_image_url"].strip():
        cover_error = "Choose one: upload an image or paste an image URL. Clear the URL to use the upload."
    elif upload and remove_cover:
        cover_error = "Choose one: upload a new image or remove the current one."
    if cover_error:
        return _render_editor(request, event, values=values, errors={"cover_upload": [cover_error]}, status=400)
    try:
        # One transaction: a cover row is only kept if the event save that uses it succeeds.
        with transaction.atomic():
            cover = services.UNCHANGED
            if upload:
                try:
                    cover = covers.create_cover(upload, user=request.user)
                except ValidationError as exc:
                    raise ValidationError({"cover_upload": exc.messages})
            elif remove_cover:
                cover = None
            event, changed = services.save_event(values, user=request.user, instance=event, cover=cover)
    except ValidationError as exc:
        return _render_editor(request, event, values=values, errors=_errors(exc), status=400)
    if changed:
        messages.success(request, f"Saved revision {event.revision}.")
    else:
        messages.info(request, "No changes to save.")
    return redirect("studio:event-edit", pk=event.pk)


@studio_view(perm="webinars.change_event", write_perm="webinars.change_event", methods=("POST",))
def event_publish(request, pk):
    event = get_object_or_404(Event, pk=pk)
    visibility = request.POST.get("visibility", "")
    if visibility not in Event.Visibility.values:
        messages.error(request, "Choose public, unlisted or draft.")
        return redirect("studio:event-edit", pk=event.pk)
    if visibility != Event.Visibility.DRAFT:
        problems = services.publish_problems(event)
        if problems:
            for problem in problems:
                messages.error(request, problem)
            return redirect("studio:event-edit", pk=event.pk)
    try:
        services.set_visibility(event, visibility, user=request.user)
    except ValidationError as exc:
        _flash_validation(request, exc)
        return redirect("studio:event-edit", pk=event.pk)
    labels = {
        "public": "Published. The landing page is live and listed.",
        "unlisted": "Published as unlisted: anyone with the link can register.",
        "draft": "Unpublished. The landing page is no longer available.",
    }
    messages.success(request, labels[visibility])
    return redirect("studio:event-edit", pk=event.pk)


@studio_view(perm="webinars.delete_event", write_perm="webinars.delete_event", methods=("POST",))
def event_delete(request, pk):
    event = get_object_or_404(Event, pk=pk)
    name = event.internal_name
    try:
        services.delete_draft_event(event, user=request.user)
    except ValidationError as exc:
        _flash_validation(request, exc)
        return redirect("studio:event-edit", pk=event.pk)
    messages.success(request, f"Deleted “{name}”.")
    return redirect("studio:events")


@studio_view(perm="webinars.view_event", methods=("GET",))
def event_preview(request, pk):
    event = get_object_or_404(Event.objects.select_related("form_version", "cover_image"), pk=pk)
    now = timezone.now()
    sessions = [
        _decorate_occurrence(o, now)
        for o in event.occurrences.filter(status=Occurrence.Status.SCHEDULED, starts_at__gt=now).order_by("starts_at")
    ]
    response = render(
        request,
        "studio/content/event_preview.html",
        {
            "event": event,
            "schema": public_schema(event.form_version),
            "sessions": sessions,
            "hosts": list(event.hosts.all()),
            "topics": list(event.topics.all()),
        },
    )
    response["X-Robots-Tag"] = "noindex"
    return response


@studio_view(perm=("webinars.view_event", "webinars.add_occurrence"), write_perm="webinars.add_occurrence", methods=("POST",))
def session_new(request, pk):
    event = get_object_or_404(Event.objects.select_related("form_version"), pk=pk)
    session_values = {
        "timezone": request.POST.get("timezone", "").strip(),
        "local_start": request.POST.get("local_start", "").strip(),
        "duration_minutes": request.POST.get("duration_minutes", "").strip(),
        "join_url": request.POST.get("join_url", "").strip(),
    }
    data = dict(session_values, registration_open=True)
    try:
        occ = services.save_occurrence(data, event=event, user=request.user)
    except ValidationError as exc:
        return _render_editor(
            request, event, session_values=session_values, session_errors=_errors(exc), status=400
        )
    messages.success(request, "Session scheduled.")
    return redirect("studio:session", pk=occ.pk)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def _session_counts(occ):
    return {
        "registered": Registration.objects.filter(occurrence=occ, status=Registration.Status.REGISTERED).count(),
        "checked_in": CheckIn.objects.filter(occurrence=occ, admission_generation=occ.admission_generation).count(),
    }


def _render_session(request, occ, *, values=None, errors=None, status=200):
    user = request.user
    now = timezone.now()
    _decorate_occurrence(occ, now)
    rs = waiting_room.room_state(occ, now)
    if values is None:
        data = services.occurrence_form_data(occ)
        values = {
            "timezone": data["timezone"],
            "local_start": data["local_start"],
            "dst_fold": str(data["dst_fold"]),
            "duration_minutes": str(data["duration_minutes"]),
            "join_url": data["join_url"],
            "registration_open": data["registration_open"],
        }
    can_change = can(user, "webinars.change_occurrence")
    if not can_change:
        # The provider join URL bypasses the waiting room and attendance tracking:
        # it never reaches the HTML of roles that can't edit the session.
        values = {**values, "join_url": ""}
    errors = dict(errors or {})
    non_field = errors.pop("__all__", [])
    cancelled = occ.status == Occurrence.Status.CANCELLED
    return render(
        request,
        "studio/content/session.html",
        {
            "occ": occ,
            "event": occ.event,
            "values": values,
            "errors": errors,
            "error_summary": _summary(errors, SESSION_LABELS) + [{"href": "", "label": "", "message": m} for m in non_field],
            "can_change": can_change,
            "readonly": not can_change or cancelled,
            "cancelled": cancelled,
            "room": rs,
            "room_state_label": ROOM_STATE_LABELS.get(rs.state, rs.state),
            "room_url": waiting_room.room_url(occ),
            "admission_controls": ADMISSION_CONTROLS,
            "counts": _session_counts(occ),
            "timezones": _timezones(values.get("timezone")),
            "can_view_registrations": can(user, "webinars.view_registration"),
        },
        status=status,
    )


def _get_occurrence(pk):
    return get_object_or_404(Occurrence.objects.select_related("event"), pk=pk)


@studio_view(perm="webinars.view_occurrence", write_perm="webinars.change_occurrence")
def session(request, pk):
    occ = _get_occurrence(pk)
    if request.method == "GET":
        return _render_session(request, occ)
    values = {
        "timezone": request.POST.get("timezone", "").strip(),
        "local_start": request.POST.get("local_start", "").strip(),
        "dst_fold": request.POST.get("dst_fold", "").strip(),
        "duration_minutes": request.POST.get("duration_minutes", "").strip(),
        "registration_open": "registration_open" in request.POST,
    }
    # A form without the field (e.g. rendered read-only) keeps the stored URL;
    # only an explicitly submitted blank value clears it.
    if "join_url" in request.POST:
        values["join_url"] = request.POST["join_url"].strip()
    try:
        occ = services.save_occurrence(values, instance=occ, user=request.user)
    except ValidationError as exc:
        values.setdefault("join_url", occ.join_url)
        return _render_session(request, occ, values=values, errors=_errors(exc), status=400)
    messages.success(request, "Session saved.")
    return redirect("studio:session", pk=occ.pk)


@studio_view(perm="webinars.view_occurrence", write_perm="webinars.change_occurrence", methods=("POST",))
def session_waiting_room(request, pk):
    occ = _get_occurrence(pk)
    enabled = request.POST.get("enabled") in ("on", "1", "true", "yes")
    lead = request.POST.get("admission_lead_minutes")
    try:
        waiting_room.configure(occ, enabled=enabled, lead_minutes=lead if lead not in (None, "") else None, user=request.user)
    except ValidationError as exc:
        _flash_validation(request, exc)
    else:
        messages.success(request, "Waiting room is on." if enabled else "Waiting room is off.")
    return redirect("studio:session", pk=occ.pk)


@studio_view(perm="webinars.view_occurrence", write_perm="webinars.change_occurrence", methods=("POST",))
def session_admission(request, pk):
    occ = _get_occurrence(pk)
    mode = request.POST.get("mode", "")
    try:
        waiting_room.set_admission(occ, mode, user=request.user)
    except ValidationError as exc:
        _flash_validation(request, exc)
    else:
        labels = {
            "auto": "Doors follow the schedule again.",
            "hold": "Doors are on hold. Attendees keep waiting.",
            "open": "Doors are open. Registered attendees can join now.",
            "ended": "Session ended. Attendance has been finalized.",
        }
        messages.success(request, labels.get(mode, "Updated."))
    return redirect("studio:session", pk=occ.pk)


@studio_view(perm="webinars.view_occurrence", write_perm="webinars.change_occurrence", methods=("POST",))
def session_cancel(request, pk):
    occ = _get_occurrence(pk)
    cancel_occurrence(occ, user=request.user)
    messages.success(request, "Session cancelled. Registrants are notified and reminders stop.")
    return redirect("studio:session", pk=occ.pk)


@studio_view(perm="webinars.view_occurrence", write_perm="webinars.change_occurrence", methods=("POST",))
def session_recording(request, pk):
    occ = _get_occurrence(pk)
    url = request.POST.get("recording_url", "").strip()
    if not url:
        messages.error(request, "Paste the recording link first.")
        return redirect("studio:session", pk=occ.pk)
    try:
        publish_recording(occ, url, user=request.user)
    except ValidationError as exc:
        _flash_validation(request, exc)
    else:
        messages.success(request, "Recording published. Registrants will get the link.")
    return redirect("studio:session", pk=occ.pk)


def _iso(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


@studio_view(perm="webinars.view_occurrence", methods=("GET",))
def session_live(request, pk):
    occ = _get_occurrence(pk)
    rs = waiting_room.room_state(occ)
    counts = _session_counts(occ)
    response = JsonResponse(
        {
            "state": rs.state,
            "stateLabel": ROOM_STATE_LABELS.get(rs.state, rs.state),
            "waitingRoomEnabled": occ.waiting_room_enabled,
            "admissionMode": occ.admission_mode,
            "registered": counts["registered"],
            "checkedIn": counts["checked_in"],
            "opensAt": _iso(rs.opens_at),
            "startsAt": _iso(rs.starts_at),
            "endsAt": _iso(rs.ends_at),
        }
    )
    return no_store(response)


@studio_view(perm=("webinars.view_occurrence", "webinars.view_registration"), methods=("GET",))
def session_registrations(request, pk):
    occ = _get_occurrence(pk)
    _decorate_occurrence(occ)
    show_pii = request.user.has_perm("webinars.view_person")
    qs = (
        Registration.objects.filter(occurrence=occ)
        .select_related("person")
        .prefetch_related("check_ins")
        .order_by("-created_at")
    )
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    rows = []
    for reg in page.object_list:
        person = reg.person
        check_in = next((c for c in reg.check_ins.all() if c.occurrence_id == occ.pk), None)
        if show_pii and person.erased_at is None:
            name = person.name or " ".join(p for p in (person.first_name, person.last_name) if p)
            email, company = person.email, person.company
        else:
            name, email, company = MASK, MASK, MASK
        rows.append(
            {
                "reg": reg,
                "name": name or "—",
                "email": email or "—",
                "company": company or "—",
                "check_in": check_in,
            }
        )
    return render(
        request,
        "studio/content/registrations.html",
        {
            "occ": occ,
            "event": occ.event,
            "rows": rows,
            "page": page,
            "show_pii": show_pii,
            "can_record": can(request.user, "webinars.add_attendanceevent"),
            "counts": _session_counts(occ),
        },
    )


@studio_view(perm="webinars.view_registration", write_perm="webinars.add_attendanceevent", methods=("POST",))
def registration_attendance(request, pk):
    reg = get_object_or_404(Registration.objects.select_related("occurrence"), pk=pk)
    kind = request.POST.get("kind", "")
    try:
        record_attendance(reg, kind, user=request.user, source="studio")
    except ValidationError as exc:
        _flash_validation(request, exc)
    else:
        labels = {"attended": "Marked as attended.", "no_show": "Marked as no-show.", "follow_up": "Follow-up requested."}
        messages.success(request, labels.get(kind, "Recorded."))
    url = reverse("studio:session-registrations", args=[reg.occurrence_id])
    page = request.POST.get("page", "")
    if page.isdigit():
        url += f"?page={page}"
    return redirect(url)


# ---------------------------------------------------------------------------
# Registration forms (builder)
# ---------------------------------------------------------------------------

CUSTOM_CHOICE = "__custom__"


def _key_choices():
    return [(k, DEFAULT_LABELS.get(k, k)) for k in CANONICAL_FIELDS]


def _blank_row():
    return {"choice": "", "custom_key": "", "label": "", "help": "", "required": False, "type": "text",
            "options_text": "", "error": ""}


def _display_row(row, error=""):
    key = row.get("key") or ""
    custom = key.startswith(CUSTOM_PREFIX)
    return {
        "choice": CUSTOM_CHOICE if custom else key,
        "custom_key": key[len(CUSTOM_PREFIX):] if custom else "",
        "label": row.get("label") or "",
        "help": row.get("help") or "",
        "required": bool(row.get("required")),
        "type": row.get("type") or "text",
        "options_text": "\n".join(row.get("options") or []),
        "error": error,
    }


def _parse_rows(post):
    try:
        count = max(0, min(int(post.get("rows-count", "0")), 80))
    except ValueError:
        count = 0
    rows = []
    for i in range(count):
        p = f"rows-{i}-"
        choice = post.get(p + "key", "").strip()
        if not choice:
            continue  # blank rows are ignored
        key = CUSTOM_PREFIX + post.get(p + "custom_key", "").strip() if choice == CUSTOM_CHOICE else choice
        rows.append(
            {
                "key": key,
                "label": post.get(p + "label", "").strip(),
                "help": post.get(p + "help", "").strip(),
                "required": (p + "required") in post,
                "type": post.get(p + "type", "") or "text",
                "options": [o.strip() for o in post.get(p + "options", "").splitlines() if o.strip()],
            }
        )
    return rows


def _parse_aliases(post):
    try:
        count = max(0, min(int(post.get("alias-count", "0")), 60))
    except ValueError:
        count = 0
    aliases = []
    for i in range(count):
        name = post.get(f"alias-{i}-name", "").strip()
        target = post.get(f"alias-{i}-target", "").strip()
        if name or target:
            aliases.append({"name": name, "target": target})
    return aliases


def _meta_from_post(post, aliases):
    return {
        "description": post.get("description", "").strip(),
        "field_mapping": {a["name"]: a["target"] for a in aliases},
        "default_phone_region": post.get("default_phone_region", "").strip().upper(),
        "sms_disclosure_version": post.get("sms_disclosure_version", "").strip(),
        "sms_disclosure_text": post.get("sms_disclosure_text", "").strip(),
        "is_active": "is_active" in post,
    }


def _render_builder(request, *, fv=None, rows, meta, aliases, version_value="", errors=None, can_save=True, status=200):
    errors = dict(errors or {})
    row_errors = {k: v for k, v in errors.items() if isinstance(k, int)}
    field_errors = {k: v for k, v in errors.items() if isinstance(k, str) and k in FORM_LABELS}
    general = [m for k, v in errors.items() if isinstance(k, str) and k not in FORM_LABELS for m in v]
    display = [_display_row(r, " ".join(row_errors.get(i, []))) for i, r in enumerate(rows)]
    display += [_blank_row(), _blank_row()]
    alias_rows = list(aliases) + [{"name": "", "target": ""}, {"name": "", "target": ""}]
    summary = []
    for i, msgs in row_errors.items():
        for m in msgs:
            summary.append({"href": f"#row-{i}", "label": f"Field {i + 1}", "message": m})
    summary += _summary(field_errors, FORM_LABELS)
    summary += [{"href": "", "label": "", "message": m} for m in general]
    locked = fv is not None and services.form_is_locked(fv)
    in_use = fv.events.select_related(None).order_by("internal_name") if fv is not None else []
    return render(
        request,
        "studio/content/form_edit.html",
        {
            "fv": fv,
            "locked": locked,
            "rows": display,
            "row_template": _blank_row(),
            "key_choices": _key_choices(),
            "custom_choice": CUSTOM_CHOICE,
            "custom_types": CUSTOM_TYPES,
            "aliases": alias_rows,
            "meta": meta,
            "version_value": version_value,
            "errors": field_errors,
            "error_summary": summary,
            "readonly": not can_save,
            "in_use": in_use,
        },
        status=status,
    )


def _save_builder(request, *, fv=None):
    rows = _parse_rows(request.POST)
    aliases = _parse_aliases(request.POST)
    meta = _meta_from_post(request.POST, aliases)
    version_value = request.POST.get("version", "").strip()
    try:
        saved, created = services.save_form_version(
            rows, meta, user=request.user, instance=fv, new_version=version_value
        )
    except ValidationError as exc:
        return _render_builder(
            request, fv=fv, rows=rows, meta=meta, aliases=aliases, version_value=version_value,
            errors=_errors(exc), status=400,
        )
    if created and fv is not None:
        messages.success(
            request,
            f"Saved as new version “{saved.version}”. Events keep using “{fv.version}” until you switch them.",
        )
    elif created:
        messages.success(request, f"Form “{saved.version}” created.")
    else:
        messages.success(request, "Form saved.")
    return redirect("studio:form-edit", version=saved.version)


@studio_view(perm="webinars.view_formversion", methods=("GET",))
def forms(request):
    versions = list(FormVersion.objects.annotate(event_count=Count("events")).order_by("-created_at"))
    for fv in versions:
        fv.locked = services.form_is_locked(fv)
        fv.field_count = len(fv.required_fields or []) + len(fv.optional_fields or []) + len(fv.custom_fields or [])
    return render(
        request,
        "studio/content/forms.html",
        {"versions": versions, "can_add": can(request.user, "webinars.add_formversion")},
    )


@studio_view(perm="webinars.add_formversion", write_perm="webinars.add_formversion")
def form_new(request):
    if request.method == "POST":
        return _save_builder(request)
    meta = {"description": "", "default_phone_region": "", "sms_disclosure_version": "", "sms_disclosure_text": "",
            "is_active": True}
    suggested = (
        samples.DEFAULT_FORM_VERSION
        if not FormVersion.objects.filter(version=samples.DEFAULT_FORM_VERSION).exists()
        else services.suggest_version(samples.DEFAULT_FORM_VERSION)
    )
    return _render_builder(
        request, rows=[dict(r) for r in samples.DEFAULT_FORM_ROWS], meta=meta, aliases=[], version_value=suggested
    )


@studio_view(perm="webinars.view_formversion")
def form_edit(request, version):
    fv = FormVersion.objects.filter(version=version).first()
    if fv is None:
        raise Http404("No such form version")
    locked = services.form_is_locked(fv)
    needed = "webinars.add_formversion" if locked else "webinars.change_formversion"
    can_save = can(request.user, needed)
    if request.method == "POST":
        if not can_save:
            return forbidden(request, "Your role can view this form but not change it.")
        return _save_builder(request, fv=fv)
    meta = {
        "description": fv.description,
        "default_phone_region": fv.default_phone_region,
        "sms_disclosure_version": fv.sms_disclosure_version,
        "sms_disclosure_text": fv.sms_disclosure_text,
        "is_active": fv.is_active,
    }
    aliases = [{"name": k, "target": v} for k, v in (fv.field_mapping or {}).items()]
    return _render_builder(
        request,
        fv=fv,
        rows=rows_for(fv),
        meta=meta,
        aliases=aliases,
        version_value=services.suggest_version(fv.version) if locked else "",
        can_save=can_save,
    )


# ---------------------------------------------------------------------------
# Hosts and topics
# ---------------------------------------------------------------------------


@studio_view(perm="webinars.view_host")
def hosts(request):
    user = request.user
    host_form = HostForm(prefix="host")
    topic_form = TopicForm(prefix="topic")
    status = 200
    if request.method == "POST":
        kind = request.POST.get("kind")
        if kind == "topic":
            if not can(user, "webinars.add_topic"):
                return forbidden(request, "Your role can't add topics.")
            topic_form = TopicForm(request.POST, prefix="topic")
            if topic_form.is_valid():
                topic = topic_form.save()
                audit("topic.saved", user=user, obj=topic, created=True)
                messages.success(request, f"Topic “{topic.name}” added.")
                return redirect(reverse("studio:hosts") + "#topics")
            status = 400
        else:
            if not can(user, "webinars.add_host"):
                return forbidden(request, "Your role can't add hosts.")
            host_form = HostForm(request.POST, prefix="host")
            if host_form.is_valid():
                host = host_form.save()
                audit("host.saved", user=user, obj=host, created=True)
                messages.success(request, f"Host “{host.name}” added.")
                return redirect("studio:hosts")
            status = 400
    return render(
        request,
        "studio/content/hosts.html",
        {
            "hosts": Host.objects.annotate(event_count=Count("events")).order_by("name"),
            "topics": Topic.objects.annotate(event_count=Count("events")).order_by("name")
            if user.has_perm("webinars.view_topic")
            else None,
            "host_form": host_form,
            "topic_form": topic_form,
            "can_add_host": can(user, "webinars.add_host"),
            "can_add_topic": can(user, "webinars.add_topic"),
        },
        status=status,
    )


@studio_view(perm="webinars.view_host", write_perm="webinars.change_host")
def host_edit(request, slug):
    host = get_object_or_404(Host, slug=slug)
    form = HostForm(request.POST or None, instance=host, prefix="host")
    if request.method == "POST":
        if form.is_valid():
            host = form.save()
            audit("host.saved", user=request.user, obj=host, created=False)
            messages.success(request, "Host saved.")
            return redirect("studio:host-edit", slug=host.slug)
    return render(
        request,
        "studio/content/catalog_edit.html",
        {"obj": host, "form": form, "kind": "host", "readonly": not can(request.user, "webinars.change_host"),
         "events": host.events.order_by("internal_name")},
        status=400 if request.method == "POST" else 200,
    )


@studio_view(perm="webinars.view_topic", write_perm="webinars.change_topic")
def topic_edit(request, slug):
    topic = get_object_or_404(Topic, slug=slug)
    form = TopicForm(request.POST or None, instance=topic, prefix="topic")
    if request.method == "POST":
        if form.is_valid():
            topic = form.save()
            audit("topic.saved", user=request.user, obj=topic, created=False)
            messages.success(request, "Topic saved.")
            return redirect("studio:topic-edit", slug=topic.slug)
    return render(
        request,
        "studio/content/catalog_edit.html",
        {"obj": topic, "form": form, "kind": "topic", "readonly": not can(request.user, "webinars.change_topic"),
         "events": topic.events.order_by("internal_name")},
        status=400 if request.method == "POST" else 200,
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@studio_view(perm="webinars.view_reports", methods=("GET",))
def reports(request):
    summary = report_summary()
    rows = []
    totals = {"registered": 0, "attended": 0, "no_show": 0}
    for row in summary["occurrences"]:
        rows.append(
            {
                "id": row["occurrenceId"],
                "event": row["event"],
                "starts_at": parse_datetime(row["startsAt"]),
                "status": row["status"],
                "registered": row["registrations"]["registered"],
                "cancelled": row["registrations"]["cancelled"],
                "attended": row["attendance"]["attended"],
                "no_show": row["attendance"]["no_show"],
                "unknown": row["attendance"]["unknown"],
                "consents": row["consentsGranted"],
            }
        )
        totals["registered"] += row["registrations"]["registered"]
        totals["attended"] += row["attendance"]["attended"]
        totals["no_show"] += row["attendance"]["no_show"]
    return render(
        request,
        "studio/content/reports.html",
        {"rows": rows, "totals": totals,
         "outbox": [(label, summary["outbox"].get(value, 0)) for value, label in OutboxMessage.Status.choices],
         "can_view_sessions": can(request.user, "webinars.view_occurrence")},
    )
