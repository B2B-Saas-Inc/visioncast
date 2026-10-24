"""B5 regression: no role may manage auth users/groups/permissions."""
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission

from webinars.roles import ADMINISTRATOR, ROLE_PERMISSIONS, apply_roles

pytestmark = pytest.mark.django_db

_FORBIDDEN_ACTIONS = ("add_", "change_", "delete_")


def test_role_mapping_has_no_auth_write_permissions():
    for role, perms in ROLE_PERMISSIONS.items():
        for perm in perms:
            app_label, codename = perm.split(".", 1)
            if app_label == "auth":
                assert not codename.startswith(_FORBIDDEN_ACTIONS), (role, perm)


def test_migrated_groups_have_no_auth_write_permissions():
    for group in Group.objects.filter(name__in=ROLE_PERMISSIONS):
        bad = group.permissions.filter(content_type__app_label="auth").exclude(codename__startswith="view_")
        assert not bad.exists(), (group.name, list(bad.values_list("codename", flat=True)))


def test_sync_removes_stale_change_user_from_administrator():
    group = Group.objects.get(name=ADMINISTRATOR)
    stale = Permission.objects.get(content_type__app_label="auth", codename="change_user")
    group.permissions.add(stale)
    apply_roles()
    assert not group.permissions.filter(pk=stale.pk).exists()


def test_administrator_cannot_escalate_via_admin(client):
    User = get_user_model()
    admin = User.objects.create_user(username="adm-esc", password="Correct-Horse-Battery-9", is_staff=True)
    admin.groups.add(Group.objects.get(name=ADMINISTRATOR))
    admin = User.objects.get(pk=admin.pk)
    for perm in ("auth.change_user", "auth.add_user", "auth.change_group", "auth.add_group", "auth.change_permission"):
        assert not admin.has_perm(perm), perm
    client.force_login(admin)
    response = client.post(
        f"/admin/auth/user/{admin.pk}/change/",
        {"username": admin.username, "is_staff": "on", "is_active": "on", "is_superuser": "on"},
    )
    # View-only access: the change form is read-only and POST is refused.
    assert response.status_code == 403
    admin.refresh_from_db()
    assert not admin.is_superuser
