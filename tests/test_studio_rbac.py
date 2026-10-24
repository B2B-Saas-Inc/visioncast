"""Studio role-based access (HTTP). Paths follow the studio URL contract."""
import re

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.test import Client

from webinars.crypto import hash_token
from webinars.models import ApiKey, Invitation

pytestmark = pytest.mark.django_db

PASSWORD = "studio-pass-Quiet-Harbor-7"


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []


def make_user(django_user_model, username, role=None, superuser=False):
    if superuser:
        return django_user_model.objects.create_superuser(username, f"{username}@example.com", PASSWORD)
    user = django_user_model.objects.create_user(username, f"{username}@example.com", PASSWORD)
    if role:
        user.groups.add(Group.objects.get(name=role))
        if role == "Administrator":
            user.is_staff = True
            user.save(update_fields=["is_staff"])
    return user


def client_for(user, **kw):
    c = Client(**kw)
    c.force_login(user)
    return c


def test_anonymous_redirected_to_login(client):
    resp = client.get("/studio/")
    assert resp.status_code == 302
    assert resp["Location"].startswith("/studio/login/")


def test_anonymous_settings_page_redirected(client):
    resp = client.get("/studio/api-keys/")
    assert resp.status_code == 302
    assert resp["Location"].startswith("/studio/login/")


def test_login_page_public(client):
    assert client.get("/studio/login/").status_code == 200


def test_user_without_role_forbidden(django_user_model):
    nobody = make_user(django_user_model, "nobody")
    assert client_for(nobody).get("/studio/").status_code == 403


def test_analyst_reads_events_but_cannot_create(django_user_model):
    analyst = client_for(make_user(django_user_model, "analyst", "Analyst"))
    assert analyst.get("/studio/").status_code == 200
    assert analyst.get("/studio/events/").status_code == 200
    resp = analyst.post("/studio/events/new/", {"internal_name": "Should not exist"})
    assert resp.status_code == 403


def test_editor_cannot_see_api_keys(django_user_model):
    editor = client_for(make_user(django_user_model, "editor", "Editor"))
    assert editor.get("/studio/events/").status_code == 200
    assert editor.get("/studio/api-keys/").status_code == 403
    assert editor.post("/studio/api-keys/new", {"name": "x", "scopes": ["public_read"]}).status_code == 403
    assert ApiKey.objects.count() == 0


def test_administrator_creates_api_key_reveal_once(django_user_model):
    admin = client_for(make_user(django_user_model, "admin", "Administrator"))
    assert admin.get("/studio/api-keys/").status_code == 200
    resp = admin.post("/studio/api-keys/new", {"name": "Marketing site", "scopes": ["public_read"]})
    assert resp.status_code == 200
    assert "no-store" in resp.get("Cache-Control", "")
    body = resp.content.decode()
    keys = set(re.findall(r"wbk_[0-9a-f]{8}_[A-Za-z0-9_\-]+", body))
    assert len(keys) == 1, "the raw key is revealed exactly once"
    raw = keys.pop()
    key = ApiKey.objects.get(name="Marketing site")
    assert key.key_hash == hash_token(raw)
    assert raw not in repr({f.attname: getattr(key, f.attname) for f in ApiKey._meta.concrete_fields})
    assert list(key.scopes) == ["public_read"]
    # Listing afterwards never shows the raw key again.
    listing = admin.get("/studio/api-keys/").content.decode()
    assert raw not in listing


def test_administrator_cannot_invite(django_user_model):
    admin = client_for(make_user(django_user_model, "admin2", "Administrator"))
    resp = admin.post("/studio/team/invite", {"email": "new@example.com", "role": "Editor"})
    assert resp.status_code == 403
    assert Invitation.objects.count() == 0


def test_administrator_reads_team(django_user_model):
    admin = client_for(make_user(django_user_model, "admin3", "Administrator"))
    assert admin.get("/studio/team/").status_code == 200


def test_superuser_invite_shows_link_once(django_user_model):
    root = client_for(make_user(django_user_model, "root", superuser=True))
    resp = root.post("/studio/team/invite", {"email": "new.editor@example.com", "role": "Editor"})
    assert resp.status_code == 200
    assert "no-store" in resp.get("Cache-Control", "")
    body = resp.content.decode()
    assert "/studio/invite/" in body
    match = re.search(r"/studio/invite/([A-Za-z0-9_\-]+)/", body)
    assert match
    inv = Invitation.objects.get(email="new.editor@example.com")
    assert inv.token_hash == hash_token(match.group(1))
    assert inv.role == "Editor"


def test_invite_accept_page_public_and_logs_in(django_user_model, client):
    from webinars import invitations

    root = make_user(django_user_model, "root2", superuser=True)
    _inv, token = invitations.create_invitation(actor=root, email="joiner@example.com", role="Analyst")
    exchanged = client.get(f"/studio/invite/{token}/")
    assert exchanged.status_code == 302
    assert exchanged["Location"] == "/studio/invite/"
    assert client.get("/studio/invite/").status_code == 200
    resp = client.post(
        f"/studio/invite/{token}/",
        {"username": "joiner", "password": PASSWORD, "password_confirm": PASSWORD},
    )
    assert resp.status_code == 302
    assert resp["Location"].rstrip("/").endswith("/studio") or resp["Location"] == "/studio/"
    assert client.get("/studio/").status_code == 200
    user = django_user_model.objects.get(username="joiner")
    assert not user.is_superuser


def test_csrf_enforced_on_studio_post(django_user_model):
    admin = make_user(django_user_model, "admin4", "Administrator")
    c = Client(enforce_csrf_checks=True)
    c.force_login(admin)
    resp = c.post("/studio/api-keys/new", {"name": "No token", "scopes": ["public_read"]})
    assert resp.status_code == 403
    assert not ApiKey.objects.filter(name="No token").exists()


def test_logout_requires_post(django_user_model):
    editor = client_for(make_user(django_user_model, "editor5", "Editor"))
    assert editor.get("/studio/logout/").status_code in (405, 302)
    resp = editor.post("/studio/logout/")
    assert resp.status_code == 302
    assert editor.get("/studio/").status_code == 302
