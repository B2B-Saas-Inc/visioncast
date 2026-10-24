"""Regression tests for rebuild review findings M1, M2 and S6 (Studio)."""
import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client

from tests.conftest import JOIN_URL, make_event, make_form, make_occurrence
from webinars import invitations, services
from webinars.crypto import encrypt_secret
from webinars.mail import PREVIEW_BACKEND
from webinars.models import Occurrence, PreviewMessage
from webinars.roles import ADMINISTRATOR, EDITOR, ROLE_PERMISSIONS
from webinars.studio.views_settings import INVITE_SESSION_KEY, ROLE_HELP

pytestmark = pytest.mark.django_db

PASSWORD = "studio-pass-Quiet-Harbor-7"


def make_user(django_user_model, username, role=None, superuser=False):
    if superuser:
        return django_user_model.objects.create_superuser(username, f"{username}@example.com", PASSWORD)
    user = django_user_model.objects.create_user(username, f"{username}@example.com", PASSWORD)
    if role:
        user.groups.add(Group.objects.get(name=role))
        if role == ADMINISTRATOR:
            user.is_staff = True
            user.save(update_fields=["is_staff"])
    return user


def client_for(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def session_occ(db):
    return make_occurrence(make_event(make_form(version="fix-f1")))


# ---------------------------------------------------------------------------
# M1: preview mailbox is superuser-only
# ---------------------------------------------------------------------------


@pytest.fixture
def preview_message(settings):
    settings.EMAIL_BACKEND = PREVIEW_BACKEND
    return PreviewMessage.objects.create(
        to="bob@example.com",
        subject="You're invited",
        body_encrypted=encrypt_secret("Accept: https://webinars.example.com/studio/invite/secret-token/"),
    )


def test_no_role_grants_preview_message_permissions():
    for role, perms in ROLE_PERMISSIONS.items():
        assert not [p for p in perms if p.endswith("_previewmessage")], role
    group = Group.objects.get(name=ADMINISTRATOR)
    assert not group.permissions.filter(content_type__model="previewmessage").exists()


def test_administrator_cannot_open_mailbox(django_user_model, preview_message):
    admin = client_for(make_user(django_user_model, "mbx-admin", ADMINISTRATOR))
    assert admin.get("/studio/mailbox/").status_code in (403, 404)
    resp = admin.get(f"/studio/mailbox/{preview_message.pk}/")
    assert resp.status_code in (403, 404)
    assert b"secret-token" not in resp.content
    assert b'href="/studio/mailbox/"' not in admin.get("/studio/").content


def test_stale_previewmessage_grant_does_not_open_mailbox(django_user_model, preview_message):
    user = make_user(django_user_model, "mbx-stale", ADMINISTRATOR)
    user.user_permissions.add(Permission.objects.get(content_type__model="previewmessage", codename="view_previewmessage"))
    resp = client_for(user).get(f"/studio/mailbox/{preview_message.pk}/")
    assert resp.status_code in (403, 404)


def test_superuser_can_open_mailbox(django_user_model, preview_message):
    root = client_for(make_user(django_user_model, "mbx-root", superuser=True))
    assert root.get("/studio/mailbox/").status_code == 200
    resp = root.get(f"/studio/mailbox/{preview_message.pk}/")
    assert resp.status_code == 200
    assert b"secret-token" in resp.content


# ---------------------------------------------------------------------------
# M2: join URL only for users who can change the session
# ---------------------------------------------------------------------------


def test_analyst_session_page_has_no_join_url(django_user_model, session_occ):
    analyst = client_for(make_user(django_user_model, "m2-analyst", "Analyst"))
    resp = analyst.get(f"/studio/sessions/{session_occ.pk}/")
    assert resp.status_code == 200
    assert JOIN_URL.encode() not in resp.content
    assert b'name="join_url"' not in resp.content


def test_writer_session_page_shows_join_url(django_user_model, session_occ):
    editor = client_for(make_user(django_user_model, "m2-editor", EDITOR))
    resp = editor.get(f"/studio/sessions/{session_occ.pk}/")
    assert resp.status_code == 200
    assert JOIN_URL.encode() in resp.content


def _schedule_post(occ, **extra):
    data = services.occurrence_form_data(occ)
    post = {
        "timezone": data["timezone"],
        "local_start": data["local_start"],
        "dst_fold": str(data["dst_fold"]),
        "duration_minutes": str(data["duration_minutes"]),
        "registration_open": "on",
    }
    post.update(extra)
    return post


def test_writer_can_update_join_url(django_user_model, session_occ):
    editor = client_for(make_user(django_user_model, "m2-writer", EDITOR))
    new_url = "https://join.example.org/s/new-link"
    resp = editor.post(f"/studio/sessions/{session_occ.pk}/", _schedule_post(session_occ, join_url=new_url))
    assert resp.status_code == 302, resp.content
    assert Occurrence.objects.get(pk=session_occ.pk).join_url == new_url


def test_post_without_join_url_field_keeps_stored_url(django_user_model, session_occ):
    editor = client_for(make_user(django_user_model, "m2-keep", EDITOR))
    resp = editor.post(f"/studio/sessions/{session_occ.pk}/", _schedule_post(session_occ))
    assert resp.status_code == 302, resp.content
    assert Occurrence.objects.get(pk=session_occ.pk).join_url == JOIN_URL


def test_analyst_cannot_post_session(django_user_model, session_occ):
    analyst = client_for(make_user(django_user_model, "m2-analyst-post", "Analyst"))
    resp = analyst.post(f"/studio/sessions/{session_occ.pk}/", _schedule_post(session_occ, join_url=""))
    assert resp.status_code == 403
    assert Occurrence.objects.get(pk=session_occ.pk).join_url == JOIN_URL


# ---------------------------------------------------------------------------
# S6: role help text, email change re-auth, invite token exchange
# ---------------------------------------------------------------------------


def test_editor_help_text_matches_permissions():
    assert "webinars.view_registration" not in ROLE_PERMISSIONS[EDITOR]
    help_text = ROLE_HELP[EDITOR]
    assert "Sees registrations" not in help_text
    assert "Can't see registrations" in help_text


def test_email_change_requires_current_password(django_user_model):
    user = make_user(django_user_model, "s6-email", EDITOR)
    c = client_for(user)
    resp = c.post("/studio/account/", {"action": "email", "email": "new.address@example.com"})
    assert resp.status_code == 400
    user.refresh_from_db()
    assert user.email == "s6-email@example.com"

    resp = c.post(
        "/studio/account/",
        {"action": "email", "email": "new.address@example.com", "current_password": "wrong-password"},
    )
    assert resp.status_code == 400
    user.refresh_from_db()
    assert user.email == "s6-email@example.com"

    resp = c.post(
        "/studio/account/",
        {"action": "email", "email": "new.address@example.com", "current_password": PASSWORD},
    )
    assert resp.status_code == 302
    user.refresh_from_db()
    assert user.email == "new.address@example.com"


def test_invite_link_is_exchanged_for_tokenless_url(django_user_model, client):
    root = make_user(django_user_model, "s6-root", superuser=True)
    _inv, token = invitations.create_invitation(actor=root, email="s6.joiner@example.com", role="Analyst")

    resp = client.get(f"/studio/invite/{token}/")
    assert resp.status_code == 302
    assert resp["Location"] == "/studio/invite/"
    assert token not in resp["Location"]
    assert resp["Referrer-Policy"] == "same-origin"  # preserve same-origin form CSRF; no external referrer
    assert "no-store" in resp["Cache-Control"]

    page = client.get("/studio/invite/")
    assert page.status_code == 200
    assert b"s6.joiner@example.com" in page.content
    assert token.encode() not in page.content

    resp = client.post("/studio/invite/", {"username": "s6joiner", "password": PASSWORD, "password_confirm": PASSWORD})
    assert resp.status_code == 302
    assert django_user_model.objects.filter(username="s6joiner").exists()
    assert INVITE_SESSION_KEY not in client.session


def test_tokenless_invite_url_without_session_is_invalid(client):
    assert client.get("/studio/invite/").status_code == 404
    resp = client.post("/studio/invite/", {"username": "x", "password": PASSWORD, "password_confirm": PASSWORD})
    assert resp.status_code == 404


def test_unknown_invite_token_is_not_stored(client):
    resp = client.get("/studio/invite/not-a-real-token/")
    assert resp.status_code == 404
    assert INVITE_SESSION_KEY not in client.session
