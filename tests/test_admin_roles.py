import io

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command

from tests.conftest import make_registration

pytestmark = pytest.mark.django_db

ROLE_NAMES = ("Administrator", "Editor", "Analyst")


def _staff(group_name, username):
    user = get_user_model().objects.create_user(username=username, password="Correct-Horse-Battery-9", is_staff=True)
    user.groups.add(Group.objects.get(name=group_name))
    return user


def test_role_groups_created_by_migrations():
    for name in ROLE_NAMES:
        assert Group.objects.filter(name=name).exists(), name


def test_editor_has_no_person_access_but_can_edit_events(client):
    client.force_login(_staff("Editor", "editor"))
    assert client.get("/admin/webinars/person/").status_code == 403
    assert client.get("/admin/webinars/event/").status_code == 200
    assert client.get("/admin/webinars/apikey/").status_code == 403
    assert client.get("/admin/webinars/destination/").status_code == 403


def test_editor_permissions():
    editor = _staff("Editor", "editor2")
    assert editor.has_perm("webinars.change_event")
    assert editor.has_perm("webinars.change_occurrence")
    assert not editor.has_perm("webinars.view_person")
    assert not editor.has_perm("webinars.view_consentrecord")
    assert not editor.has_perm("webinars.view_apikey")


def test_analyst_sees_registrations_with_masked_email(client, occurrence):
    from tests.conftest import make_person

    person = make_person(email="secret.person@example.org", first_name="Secretname")
    reg = make_registration(occurrence, person=person, submitted_profile={"email": "secret.person@example.org"})
    analyst = _staff("Analyst", "analyst")
    assert not analyst.has_perm("webinars.change_registration")
    client.force_login(analyst)
    listing = client.get("/admin/webinars/registration/")
    assert listing.status_code == 200
    assert b"secret.person@example.org" not in listing.content
    detail = client.get(f"/admin/webinars/registration/{reg.pk}/change/")
    assert detail.status_code == 200
    assert b"secret.person@example.org" not in detail.content
    assert client.get("/admin/webinars/person/").status_code == 403


def test_analyst_cannot_modify_events(client):
    analyst = _staff("Analyst", "analyst2")
    assert analyst.has_perm("webinars.view_event")
    assert not analyst.has_perm("webinars.change_event")
    assert not analyst.has_perm("webinars.delete_event")


def test_administrator_can_manage_people_and_keys():
    admin = _staff("Administrator", "admin1")
    for perm in ("view_person", "change_destination", "add_apikey", "view_auditlog"):
        assert admin.has_perm(f"webinars.{perm}"), perm


def _clear_bootstrap_env(monkeypatch):
    for name in (
        "BOOTSTRAP_ADMIN_USERNAME",
        "BOOTSTRAP_ADMIN_EMAIL",
        "BOOTSTRAP_ADMIN_PASSWORD",
        "DJANGO_SUPERUSER_USERNAME",
        "DJANGO_SUPERUSER_EMAIL",
        "DJANGO_SUPERUSER_PASSWORD",
        "ADMIN_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


def test_bootstrap_admin_generates_password_and_refuses_second_run(monkeypatch):
    _clear_bootstrap_env(monkeypatch)
    User = get_user_model()
    out = io.StringIO()
    call_command("bootstrap_admin", username="root-admin", email="ops@example.org", stdout=out, stderr=io.StringIO())
    admins = User.objects.filter(is_superuser=True)
    assert admins.count() == 1
    user = admins.get()
    assert user.has_usable_password()
    assert user.groups.filter(name="Administrator").exists()
    for weak in ("admin", "password", "changeme", "admin123", "", user.username):
        assert not user.check_password(weak), weak
    generated = out.getvalue().strip().splitlines()[-1].strip()
    assert len(generated) >= 16
    assert user.check_password(generated)
    # Password is never written to the audit log.
    from webinars.models import AuditLog

    assert generated not in str(list(AuditLog.objects.values_list("metadata", flat=True)))

    with pytest.raises((CommandError, SystemExit)):
        call_command("bootstrap_admin", username="second", email="x@example.org", stdout=io.StringIO(), stderr=io.StringIO())
    assert User.objects.filter(is_superuser=True).count() == 1
    assert not User.objects.filter(username="second").exists()


def test_bootstrap_admin_rejects_weak_env_password(monkeypatch):
    _clear_bootstrap_env(monkeypatch)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "password")
    with pytest.raises(CommandError):
        call_command("bootstrap_admin", username="root", email="ops@example.org", stdout=io.StringIO())
    assert not get_user_model().objects.exists()
