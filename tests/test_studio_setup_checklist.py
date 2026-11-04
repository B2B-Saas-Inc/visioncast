"""The dashboard's setup checklist: hidden by hand, shown again, and dismissed once every step is done."""

import pytest
from django.contrib.auth.models import Group
from django.test import Client

from webinars.models import StudioPreference
from webinars.roles import ANALYST, EDITOR

PASSWORD = "correct horse battery staple"


def client_for(django_user_model, role, name=None):
    name = name or f"u-{role.lower()}"
    user = django_user_model.objects.create_user(name, f"{name}@example.com", PASSWORD)
    user.groups.add(Group.objects.get(name=role))
    c = Client()
    c.force_login(user)
    return c, user


@pytest.mark.django_db
def test_hide_and_show_are_per_user(django_user_model):
    c, user = client_for(django_user_model, EDITOR)
    page = c.get("/studio/").content.decode()
    assert "Setup checklist" in page and 'name="action" value="hide"' in page

    res = c.post("/studio/setup-checklist/", {"action": "hide"})
    assert res.status_code == 302 and res["Location"] == "/studio/"
    page = c.get("/studio/").content.decode()
    assert 'aria-labelledby="setup-title"' not in page
    assert "Setup checklist hidden" in page and 'name="action" value="show"' in page
    assert "Setup checklist hidden. You can show it again" in page
    assert StudioPreference.objects.get(user=user).setup_checklist_hidden is True

    # Someone else still sees it.
    other, _ = client_for(django_user_model, ANALYST)
    assert 'aria-labelledby="setup-title"' in other.get("/studio/").content.decode()

    c.post("/studio/setup-checklist/", {"action": "show"})
    assert 'aria-labelledby="setup-title"' in c.get("/studio/").content.decode()
    assert StudioPreference.objects.get(user=user).setup_checklist_hidden is False


@pytest.mark.django_db
def test_only_hide_or_show_and_only_by_post(django_user_model):
    c, _ = client_for(django_user_model, EDITOR)
    assert c.post("/studio/setup-checklist/", {"action": "drop"}).status_code == 403
    assert c.get("/studio/setup-checklist/").status_code == 405
    assert not StudioPreference.objects.exists()


@pytest.mark.django_db
def test_dismissed_once_every_step_is_done(django_user_model, monkeypatch):
    from webinars.studio import views_content

    c, user = client_for(django_user_model, EDITOR)
    assert 'aria-labelledby="setup-title"' in c.get("/studio/").content.decode()

    # Every step reports done.
    original = views_content._checklist_state
    monkeypatch.setattr(views_content, "_checklist_state", lambda request, complete: original(request, True))
    page = c.get("/studio/").content.decode()
    assert 'aria-labelledby="setup-title"' not in page
    assert "Setup complete. The checklist has been dismissed." in page
    assert "Setup checklist hidden" not in page, "nothing to show again once it's complete"
    done_at = StudioPreference.objects.get(user=user).setup_checklist_completed_at
    assert done_at is not None

    # Said once, and the time of completion is kept.
    page = c.get("/studio/").content.decode()
    assert "Setup complete" not in page
    assert StudioPreference.objects.get(user=user).setup_checklist_completed_at == done_at
