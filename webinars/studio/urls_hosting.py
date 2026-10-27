"""Studio hosting routes (appended to the "studio" namespace by webinars/studio/urls.py)."""
from django.urls import path

from webinars.studio import views_hosting as v

urlpatterns = [
    path("hosting/", v.hosting_accounts, name="hosting-accounts"),
    path("hosting/new/", v.hosting_account_new, name="hosting-account-new"),
    path("hosting/<uuid:pk>/", v.hosting_account_edit, name="hosting-account-edit"),
    path("sessions/<uuid:pk>/hosting", v.session_hosting, name="session-hosting"),
    path("sessions/<uuid:pk>/hosting/registrants", v.session_registrants, name="session-registrants"),
]
