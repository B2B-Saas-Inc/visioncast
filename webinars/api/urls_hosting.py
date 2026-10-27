"""Hosting management routes (included at the root by webinars/urls.py)."""
from django.urls import path

from webinars.api import hosting

urlpatterns = [
    path("api/v1/manage/hosting/providers", hosting.hosting_providers, name="manage-hosting-providers"),
    path("api/v1/manage/hosting/accounts", hosting.hosting_accounts, name="manage-hosting-accounts"),
    path("api/v1/manage/hosting/accounts/<uuid:pk>", hosting.hosting_account_detail, name="manage-hosting-account"),
    path("api/v1/manage/occurrences/<uuid:pk>/hosting", hosting.occurrence_hosting, name="manage-hosting-occurrence"),
    path("api/v1/manage/occurrences/<uuid:pk>/hosting/preview", hosting.occurrence_hosting_preview,
         name="manage-hosting-preview"),
    path("api/v1/manage/occurrences/<uuid:pk>/hosting/execute", hosting.occurrence_hosting_execute,
         name="manage-hosting-execute"),
    path("api/v1/manage/occurrences/<uuid:pk>/hosting/reconcile", hosting.occurrence_hosting_reconcile,
         name="manage-hosting-reconcile"),
    path("api/v1/manage/occurrences/<uuid:pk>/hosting/registrants", hosting.occurrence_registrants,
         name="manage-hosting-registrants"),
    path("api/v1/manage/occurrences/<uuid:pk>/hosting/registrants/reconcile",
         hosting.occurrence_registrant_reconcile, name="manage-hosting-registrant-reconcile"),
    path("api/v1/manage/occurrences/<uuid:pk>/hosting/manual-link", hosting.occurrence_hosting_manual_link,
         name="manage-hosting-manual-link"),
]
