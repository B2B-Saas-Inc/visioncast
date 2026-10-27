"""Studio seats/tickets routes (appended to the "studio" namespace by webinars/studio/urls.py)."""
from django.urls import path

from webinars.studio import views_tickets as v

urlpatterns = [
    path("payments/", v.payments_settings, name="payments"),
    path("sessions/<uuid:pk>/tickets", v.session_tickets, name="session-tickets"),
]
