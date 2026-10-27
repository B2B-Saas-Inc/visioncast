# Run through `manage.py shell < kit-form.py` by fresh-install.sh.
# Creates a form version the kit components can render in full (canonical fields only, email +
# first/last name required, company optional) and attaches it to the demo-onboarding-office-hours
# event, so the "Confirm my RSVP" state is reachable. The seeded demo forms also contain select and
# custom.* fields, which the kit components do not render.
from webinars import services
from webinars.models import Event, FormVersion

fv = FormVersion.objects.filter(version="kit-v1").first()
if fv is None:
    rows = [
        {"key": "email", "label": "Work email", "help": "", "required": True, "options": []},
        {"key": "firstName", "label": "First name", "help": "", "required": True, "options": []},
        {"key": "lastName", "label": "Last name", "help": "", "required": True, "options": []},
        {"key": "company", "label": "Company", "help": "", "required": False, "options": []},
    ]
    fv, _ = services.save_form_version(rows, {"description": "kit test form", "is_active": True}, new_version="kit-v1")
event = Event.objects.get(slug="demo-onboarding-office-hours")
event.form_version = fv
event.save(update_fields=["form_version", "updated_at"])
print("kit-v1 attached to", event.slug)
