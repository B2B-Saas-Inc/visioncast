from webinars.studio import urls_content, urls_settings

app_name = "studio"

urlpatterns = urls_content.urlpatterns + urls_settings.urlpatterns
