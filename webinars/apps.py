from django.apps import AppConfig


class WebinarsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "webinars"
    verbose_name = "Webinars"

    def ready(self):
        # Intentionally imports nothing heavy (no network, no crypto initialisation).
        from django.core import checks

        from webinars.calendar_links import check_public_event_url_template

        checks.register(check_public_event_url_template, "webinars")
