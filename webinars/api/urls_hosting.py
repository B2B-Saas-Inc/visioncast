"""Hosting management routes (included at the root by webinars/urls.py)."""
from django.urls import path

from webinars.api import attendance_imports as attendance
from webinars.api import hosting

urlpatterns = [
    path("api/v1/manage/occurrences/<uuid:pk>/attendance/imports", attendance.occurrence_attendance_imports,
         name="manage-attendance-imports"),
    path("api/v1/manage/occurrences/<uuid:pk>/attendance/imports/<uuid:import_id>", attendance.attendance_import_detail,
         name="manage-attendance-import"),
    path("api/v1/manage/occurrences/<uuid:pk>/attendance/imports/<uuid:import_id>/apply",
         attendance.attendance_import_apply, name="manage-attendance-import-apply"),
    path("api/v1/manage/occurrences/<uuid:pk>/attendance/imports/<uuid:import_id>/rows/<uuid:row_id>/resolve",
         attendance.attendance_import_row_resolve, name="manage-attendance-import-resolve"),
    path("api/v1/manage/occurrences/<uuid:pk>/attendance/imports/<uuid:import_id>/no-shows",
         attendance.attendance_import_no_shows, name="manage-attendance-import-no-shows"),
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
