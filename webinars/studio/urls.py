from webinars.studio import urls_content, urls_hosting, urls_settings, urls_tickets

app_name = "studio"

urlpatterns = urls_content.urlpatterns + urls_settings.urlpatterns + urls_hosting.urlpatterns + urls_tickets.urlpatterns
