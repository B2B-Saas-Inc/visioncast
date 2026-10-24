from django.urls import include, path

from django.views.decorators.http import require_safe

from webinars import covers, developers, mcp, waiting_room_views
from webinars.api import management, views

urlpatterns = [
    # Marketer studio (session auth, Django permissions; webinars/studio/)
    path("studio/", include("webinars.studio.urls")),
    # Hosted waiting room (public; docs/waiting-room.md)
    path("w/<str:code>/", waiting_room_views.room, name="wr-room"),
    path("w/<str:code>/status", waiting_room_views.room_status, name="wr-status"),
    path("w/<str:code>/identify", waiting_room_views.room_identify, name="wr-identify"),
    path("w/<str:code>/verify", waiting_room_views.room_verify, name="wr-verify"),
    path("w/<str:code>/verify/<str:token>", waiting_room_views.room_verify_link, name="wr-verify-link"),
    path("w/<str:code>/register", waiting_room_views.room_register, name="wr-register"),
    path("w/<str:code>/enter", waiting_room_views.room_enter, name="wr-enter"),
    path("w/<str:code>/reset", waiting_room_views.room_reset, name="wr-reset"),
    # Developers: OpenAPI 3.1, human-readable docs, MCP (streamable HTTP, stateless)
    path("api/v1/openapi.json", developers.openapi_json, name="openapi-json"),
    path("developers/", developers.docs_index, name="developers"),
    path("developers/api/", developers.api_reference, name="developers-api"),
    path("developers/mcp/", developers.mcp_guide, name="developers-mcp"),
    path("mcp", mcp.endpoint, name="mcp"),
    path("api/v1/forms/<str:version>", views.form_schema, name="form-schema"),
    # Management API (content_write / attendance_write keys; docs/management-api.md)
    path("api/v1/manage/events", management.events, name="manage-events"),
    path("api/v1/manage/events/<uuid:pk>", management.event_detail, name="manage-event"),
    path("api/v1/manage/events/<uuid:pk>/occurrences", management.event_occurrences, name="manage-event-occurrences"),
    path("api/v1/manage/occurrences/<uuid:pk>", management.occurrence_detail, name="manage-occurrence"),
    path("api/v1/manage/occurrences/<uuid:pk>/cancel", management.occurrence_cancel, name="manage-occurrence-cancel"),
    path(
        "api/v1/manage/occurrences/<uuid:pk>/recording",
        management.occurrence_recording,
        name="manage-occurrence-recording",
    ),
    path(
        "api/v1/manage/occurrences/<uuid:pk>/waiting-room",
        management.occurrence_waiting_room,
        name="manage-occurrence-waiting-room",
    ),
    path(
        "api/v1/manage/registrations/<uuid:pk>/attendance",
        management.registration_attendance,
        name="manage-registration-attendance",
    ),
    path("api/v1/manage/hosts", management.hosts, name="manage-hosts"),
    path("api/v1/manage/hosts/<slug:slug>", management.host_detail, name="manage-host"),
    path("api/v1/manage/topics", management.topics, name="manage-topics"),
    path("api/v1/manage/topics/<slug:slug>", management.topic_detail, name="manage-topic"),
    path("api/v1/manage/forms", management.form_versions, name="manage-forms"),
    path("api/v1/manage/forms/<str:version>", management.form_version_detail, name="manage-form"),
    # Public
    path("api/v1/events", views.events_list, name="events-list"),
    path("api/v1/events/<slug:slug>", views.event_detail, name="event-detail"),
    path("api/v1/occurrences/<uuid:pk>/calendar.ics", views.occurrence_ics, name="occurrence-ics"),
    # Uploaded event covers (database-stored, immutable; webinars/covers.py)
    path("media/covers/<uuid:pk>.<str:ext>", require_safe(covers.cover_response), name="cover-image"),
    # Registration (register/proxy/trusted key) and proxy-target (proxy key)
    path("api/v1/registrations", views.api_registrations, name="api-registrations"),
    path("api/v1/profile", views.api_profile, name="api-profile"),
    path("api/v1/reset", views.api_reset, name="api-reset"),
    path("api/v1/my-registration", views.api_my_registration, name="api-my-registration"),
    # Same-origin direct proxy (no API key; origin/header CSRF defence)
    path("webinar-api/profile", views.direct_profile, name="direct-profile"),
    path("webinar-api/registrations", views.direct_registrations, name="direct-registrations"),
    path("webinar-api/reset", views.direct_reset, name="direct-reset"),
    path("webinar-api/my-registration", views.direct_my_registration, name="direct-my-registration"),
    # Public ICS for direct mode (no cookie, key or origin check; docs/continuation-api.md)
    path("webinar-api/calendar.ics", views.direct_calendar, name="direct-calendar"),
    # Health
    path("healthz", views.healthz, name="healthz"),
]
