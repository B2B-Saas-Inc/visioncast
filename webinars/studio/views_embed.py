"""Studio "Embed on your site": a copyable SDK form snippet for one session.

The snippet is built server-side from the event's form version
(``forms_schema.public_schema``) and CTA labels. Every user-controlled string
(labels, help, options, disclosure text, CTA labels) goes through
``django.utils.html.escape``, so nothing can break out of an attribute or
element (``</script`` becomes ``&lt;/script``). It contains only public
identifiers: never API keys, join URLs or waiting-room URLs.
"""
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.templatetags.static import static
from django.utils.html import escape

from webinars import forms_schema
from webinars.models import Occurrence
from webinars.studio.access import render, studio_view

SDK_STATIC_PATH = "webinar-sdk/webinar-form.js"
DEFAULT_CTA = {"register": "Register", "confirm": "Confirm my RSVP"}


def _cta(event):
    try:
        labels = forms_schema.cta_labels(event)
    except AttributeError:  # model fields not migrated yet
        labels = {}
    return {k: (labels.get(k) or v) for k, v in DEFAULT_CTA.items()}


def sdk_script_url():
    try:
        path = static(SDK_STATIC_PATH)
    except ValueError:  # manifest storage without a collected SDK
        path = settings.STATIC_URL.rstrip("/") + "/" + SDK_STATIC_PATH
    if path.startswith(("http://", "https://")):
        return path
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    return base + path


def _field_html(field):
    key = field["name"]
    fid = "wf-" + key.replace(".", "-")
    e_key, e_id = escape(key), escape(fid)
    req = " required" if field.get("required") else ""
    auto = f' autocomplete="{escape(field["autocomplete"])}"' if field.get("autocomplete") else ""
    help_id = f"{fid}-help"
    described = f' aria-describedby="{escape(help_id)}"' if field.get("help") else ""
    help_html = f'\n    <p id="{escape(help_id)}">{escape(field["help"])}</p>' if field.get("help") else ""
    label = escape(field["label"])
    ftype = field["type"]
    if ftype == "checkbox":
        return (
            f"  <div data-webinar-field>\n"
            f'    <label><input type="checkbox" id="{e_id}" name="{e_key}" value="true"{req}{described}> {label}</label>'
            f"{help_html}\n  </div>"
        )
    if ftype == "select":
        options = "".join(
            f'\n      <option value="{escape(o)}">{escape(o)}</option>' for o in (field.get("options") or [])
        )
        control = (
            f'<select id="{e_id}" name="{e_key}"{auto}{req}{described}>\n'
            f'      <option value="">Choose…</option>{options}\n    </select>'
        )
    elif ftype == "textarea":
        control = f'<textarea id="{e_id}" name="{e_key}" rows="3"{req}{described}></textarea>'
    else:
        input_type = ftype if ftype in ("email", "tel") else "text"
        control = f'<input id="{e_id}" name="{e_key}" type="{input_type}"{auto}{req}{described}>'
    return (
        f"  <div data-webinar-field>\n"
        f'    <label for="{e_id}">{label}</label>{help_html}\n'
        f"    {control}\n  </div>"
    )


def build_snippet(occurrence):
    """Return the embed HTML (plain ``str``, not marked safe) for ``occurrence``."""
    event = occurrence.event
    schema = forms_schema.public_schema(event.form_version)
    cta = _cta(event)
    lines = [
        "<form data-webinar-form"
        f'\n      data-occurrence-id="{escape(str(occurrence.pk))}"'
        f'\n      data-form-version="{escape(schema["formVersion"])}"'
        f'\n      data-confirm-label="{escape(cta["confirm"])}">',
        "  <div data-webinar-error></div>",
    ]
    lines.extend(_field_html(f) for f in schema["fields"])
    sms = schema.get("sms")
    if sms:
        lines.append(
            "  <div data-webinar-field>\n"
            f'    <label><input type="checkbox" name="consents.sms" '
            f'data-disclosure-version="{escape(sms["disclosureVersion"])}"> '
            f'{escape(sms.get("disclosureText") or "Text me reminders about this session.")}</label>\n'
            "  </div>"
        )
    lines.append(f'  <button type="submit">{escape(cta["register"])}</button>')
    lines.append("  <div data-webinar-status></div>")
    lines.append("</form>")
    lines.append(f'<script src="{escape(sdk_script_url())}" defer></script>')
    return str("\n".join(str(line) for line in lines))


@studio_view(perm="webinars.view_occurrence", methods=("GET",))
def session_embed(request, pk):
    occ = get_object_or_404(Occurrence.objects.select_related("event", "event__form_version"), pk=pk)
    event = occ.event
    return render(
        request,
        "studio/content/session_embed.html",
        {
            "occ": occ,
            "event": event,
            "form_version": event.form_version.version,
            "snippet": build_snippet(occ),
            "script_url": sdk_script_url(),
            "cta": _cta(event),
        },
    )
