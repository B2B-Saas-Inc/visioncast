"""Preview URLconf: the normal routes plus a non-secret identity probe.

The managed lifecycle compares ``/__preview__/identity`` with its recorded run
name, proving that 127.0.0.1:4318 is served by the container it owns.
"""
from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path

from config.urls import urlpatterns as _base

admin.site.site_header = "PREVIEW: sample data only, removed automatically"
admin.site.site_title = "Webinar preview"


def preview_identity(request):
    response = JsonResponse({"preview": settings.PREVIEW_RUN})
    response["Cache-Control"] = "no-store"
    return response


urlpatterns = [path("__preview__/identity", preview_identity), *_base]
