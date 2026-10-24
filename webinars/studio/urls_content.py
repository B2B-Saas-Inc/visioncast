"""Studio content-area routes. Namespace ("studio") comes from webinars/studio/urls.py."""
from django.urls import path

from webinars.studio import views_content as v
from webinars.studio import views_embed

urlpatterns = [
    path("login/", v.login_view, name="login"),
    path("logout/", v.logout_view, name="logout"),
    path("", v.dashboard, name="dashboard"),
    path("events/", v.events, name="events"),
    path("events/new/", v.event_new, name="event-new"),
    path("events/<uuid:pk>/", v.event_edit, name="event-edit"),
    path("events/<uuid:pk>/publish", v.event_publish, name="event-publish"),
    path("events/<uuid:pk>/delete", v.event_delete, name="event-delete"),
    path("events/<uuid:pk>/preview", v.event_preview, name="event-preview"),
    path("events/<uuid:pk>/sessions/new", v.session_new, name="session-new"),
    path("sessions/<uuid:pk>/", v.session, name="session"),
    path("sessions/<uuid:pk>/embed/", views_embed.session_embed, name="session-embed"),
    path("sessions/<uuid:pk>/waiting-room", v.session_waiting_room, name="session-waiting-room"),
    path("sessions/<uuid:pk>/admission", v.session_admission, name="session-admission"),
    path("sessions/<uuid:pk>/cancel", v.session_cancel, name="session-cancel"),
    path("sessions/<uuid:pk>/recording", v.session_recording, name="session-recording"),
    path("sessions/<uuid:pk>/live.json", v.session_live, name="session-live"),
    path("sessions/<uuid:pk>/registrations", v.session_registrations, name="session-registrations"),
    path("registrations/<uuid:pk>/attendance", v.registration_attendance, name="registration-attendance"),
    path("forms/", v.forms, name="forms"),
    path("forms/new/", v.form_new, name="form-new"),
    path("forms/<str:version>/", v.form_edit, name="form-edit"),
    path("hosts/", v.hosts, name="hosts"),
    path("hosts/<slug:slug>/", v.host_edit, name="host-edit"),
    path("topics/<slug:slug>/", v.topic_edit, name="topic-edit"),
    path("reports/", v.reports, name="reports"),
]
