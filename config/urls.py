from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Webinar marketing admin"
admin.site.site_title = "Webinar marketing admin"
admin.site.index_title = "Webinar marketing"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("webinars.urls")),
]
