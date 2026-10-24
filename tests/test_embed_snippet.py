"""Studio "Embed on your site": generated SDK snippet per session."""
import html

import pytest
from django.contrib.auth.models import Group
from django.test import Client

from tests.conftest import JOIN_URL, WAITING_URL, make_api_key, make_event, make_form, make_occurrence
from webinars.models import Event
from webinars.roles import ANALYST, EDITOR
from webinars.studio.views_embed import build_snippet

pytestmark = pytest.mark.django_db

PASSWORD = "studio-pass-Quiet-Harbor-7"
XSS_SCRIPT = "</script><script>alert(1)</script>"
XSS_ATTR = '"><img src=x onerror=alert(1)>'
LAYOUT = [
    {"key": "email", "label": "Work email", "help": "We send the invite here.", "options": []},
    {"key": "firstName", "label": "First name " + XSS_SCRIPT, "help": "", "options": []},
    {"key": "custom.plan", "label": "Plan", "help": "", "type": "select", "options": ["Starter", "Pro " + XSS_ATTR]},
    {"key": "custom.notes", "label": "Notes", "help": "", "type": "textarea", "options": []},
    {"key": "custom.optin", "label": "Send me the slides", "help": "", "type": "checkbox", "options": []},
]


def client_for(django_user_model, role):
    user = django_user_model.objects.create_user(f"u-{role.lower()}", f"{role.lower()}@example.com", PASSWORD)
    user.groups.add(Group.objects.get(name=role))
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def occ(db):
    form = make_form(
        version="embed-v2",
        required_fields=["email", "firstName"],
        optional_fields=[],
        custom_fields=["plan", "notes", "optin"],
        field_layout=LAYOUT,
    )
    event = make_event(form)
    Event.objects.filter(pk=event.pk).update(register_label="Save my seat", confirm_label=XSS_ATTR)
    return make_occurrence(Event.objects.get(pk=event.pk))


def _snippet(response):
    """The copyable snippet as the marketer would paste it (template-unescaped)."""
    body = response.content.decode()
    start = body.index('<code id="embed-snippet">') + len('<code id="embed-snippet">')
    return html.unescape(body[start:body.index("</code>", start)])


def test_snippet_contains_real_identifiers_fields_consent_and_labels(occ):
    s = build_snippet(occ)
    assert f'data-occurrence-id="{occ.pk}"' in s
    assert 'data-form-version="embed-v2"' in s
    assert 'name="email" type="email" autocomplete="email" required' in s
    assert 'name="firstName" type="text" autocomplete="given-name" required' in s
    assert '<option value="Starter">Starter</option>' in s
    assert '<textarea id="wf-custom-notes" name="custom.notes"' in s
    assert 'type="checkbox" id="wf-custom-optin" name="custom.optin" value="true"' in s
    assert 'name="consents.sms" data-disclosure-version="sms-v1"' in s
    assert "Reply STOP to opt out." in s
    assert '<button type="submit">Save my seat</button>' in s
    assert "<div data-webinar-status></div>" in s
    from webinars.studio.views_embed import sdk_script_url
    assert f'{sdk_script_url()}" defer></script>' in s


def test_user_strings_are_escaped_in_text_and_attributes(occ):
    s = build_snippet(occ)
    assert XSS_SCRIPT not in s and XSS_ATTR not in s
    assert s.count("</script") == 1  # only the real closing tag of the SDK script
    assert "<img" not in s
    assert "First name &lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in s
    assert 'data-confirm-label="&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"' in s
    assert 'value="Pro &quot;&gt;&lt;img src=x onerror=alert(1)&gt;"' in s


def test_studio_page_shows_snippet_without_secrets(occ, django_user_model):
    make_api_key()
    resp = client_for(django_user_model, ANALYST).get(f"/studio/sessions/{occ.pk}/embed/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert XSS_ATTR not in body and XSS_SCRIPT not in body
    assert "<img src=x" not in body
    assert JOIN_URL not in body and WAITING_URL not in body
    assert "wbk_" not in body
    assert "Nothing is deployed to your site automatically" in body
    assert "PROXY_API_KEY" in body
    pasted = _snippet(resp)
    assert pasted == build_snippet(occ)
    assert f'data-occurrence-id="{occ.pk}"' in pasted


def test_session_page_links_to_embed(occ, django_user_model):
    resp = client_for(django_user_model, EDITOR).get(f"/studio/sessions/{occ.pk}/")
    assert f"/studio/sessions/{occ.pk}/embed/" in resp.content.decode()


def test_anonymous_is_redirected_to_login(occ):
    resp = Client().get(f"/studio/sessions/{occ.pk}/embed/")
    assert resp.status_code == 302
    assert "/studio/login/" in resp["Location"]


def test_post_not_allowed(occ, django_user_model):
    resp = client_for(django_user_model, EDITOR).post(f"/studio/sessions/{occ.pk}/embed/")
    assert resp.status_code == 405
