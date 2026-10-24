"""Team invitations and member management (webinars.invitations)."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from webinars import invitations
from webinars.crypto import hash_token
from webinars.models import Invitation

pytestmark = pytest.mark.django_db

STRONG = "correct-horse-battery-staple-42"


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []


@pytest.fixture
def superuser(django_user_model):
    return django_user_model.objects.create_superuser("root", "root@example.com", STRONG)


def member(django_user_model, username, role):
    user = django_user_model.objects.create_user(username, f"{username}@example.com", STRONG)
    user.groups.add(Group.objects.get(name=role))
    return user


def test_non_superuser_cannot_invite(django_user_model):
    admin = member(django_user_model, "admin", "Administrator")
    admin.is_staff = True
    admin.save()
    with pytest.raises(PermissionDenied):
        invitations.create_invitation(actor=admin, email="new@example.com", role="Editor")
    with pytest.raises(PermissionDenied):
        invitations.create_invitation(actor=None, email="new@example.com", role="Editor")
    assert Invitation.objects.count() == 0
    assert len(mail.outbox) == 0


def test_superuser_invite_stores_hash_only_and_emails(superuser):
    inv, token = invitations.create_invitation(actor=superuser, email="New.Hire@Example.com", role="Editor")
    inv.refresh_from_db()
    assert inv.token_hash == hash_token(token)
    assert inv.token_hash != token
    stored = {f.name: getattr(inv, f.attname) for f in Invitation._meta.concrete_fields}
    assert token not in repr(stored)
    assert inv.status == "pending"
    assert len(mail.outbox) == 1
    assert invitations.accept_path(token) in mail.outbox[0].body
    assert inv.email_sent is True


def test_invite_rejects_unknown_role(superuser):
    with pytest.raises(ValidationError):
        invitations.create_invitation(actor=superuser, email="x@example.com", role="Superuser")


@pytest.mark.parametrize("role, staff", [("Administrator", True), ("Editor", False), ("Analyst", False)])
def test_accept_creates_user_with_role(superuser, role, staff):
    _inv, token = invitations.create_invitation(actor=superuser, email=f"{role.lower()}@example.com", role=role)
    user = invitations.accept_invitation(token, username=f"{role.lower()}1", password=STRONG, password_confirm=STRONG)
    user.refresh_from_db()
    assert user.email == f"{role.lower()}@example.com"
    assert list(user.groups.values_list("name", flat=True)) == [role]
    assert user.is_staff is staff
    assert user.is_superuser is False
    assert user.check_password(STRONG)
    assert invitations.role_of(user) == role


def test_token_is_single_use(superuser):
    _inv, token = invitations.create_invitation(actor=superuser, email="once@example.com", role="Analyst")
    invitations.accept_invitation(token, username="once", password=STRONG, password_confirm=STRONG)
    assert invitations.find_pending(token) is None
    with pytest.raises(ValidationError) as exc:
        invitations.accept_invitation(token, username="twice", password=STRONG, password_confirm=STRONG)
    assert "_" in exc.value.message_dict


def test_expired_invitation_fails(superuser, django_user_model):
    inv, token = invitations.create_invitation(actor=superuser, email="slow@example.com", role="Editor")
    Invitation.objects.filter(pk=inv.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert invitations.find_pending(token) is None
    with pytest.raises(ValidationError):
        invitations.accept_invitation(token, username="slow", password=STRONG, password_confirm=STRONG)
    assert not django_user_model.objects.filter(username="slow").exists()


def test_revoked_invitation_fails(superuser, django_user_model):
    inv, token = invitations.create_invitation(actor=superuser, email="nope@example.com", role="Editor")
    assert invitations.revoke_invitation(actor=superuser, invitation=inv) is True
    inv.refresh_from_db()
    assert inv.status == "revoked"
    with pytest.raises(ValidationError):
        invitations.accept_invitation(token, username="nope", password=STRONG, password_confirm=STRONG)
    assert not django_user_model.objects.filter(username="nope").exists()


def test_reinvite_revokes_older_pending(superuser):
    first, first_token = invitations.create_invitation(actor=superuser, email="again@example.com", role="Editor")
    _second, second_token = invitations.create_invitation(actor=superuser, email="again@example.com", role="Analyst")
    assert invitations.find_pending(first_token) is None
    assert invitations.find_pending(second_token) is not None


def test_mismatched_passwords(superuser, django_user_model):
    _inv, token = invitations.create_invitation(actor=superuser, email="typo@example.com", role="Editor")
    with pytest.raises(ValidationError) as exc:
        invitations.accept_invitation(token, username="typo", password=STRONG, password_confirm=STRONG + "x")
    assert "password_confirm" in exc.value.message_dict
    assert not django_user_model.objects.filter(username="typo").exists()
    assert invitations.find_pending(token) is not None


def test_unknown_token_fails():
    with pytest.raises(ValidationError):
        invitations.accept_invitation("not-a-real-token", username="ghost", password=STRONG, password_confirm=STRONG)


def test_update_member_cannot_edit_self_or_superuser(superuser, django_user_model):
    other_super = django_user_model.objects.create_superuser("root2", "root2@example.com", STRONG)
    with pytest.raises(ValidationError):
        invitations.update_member(actor=superuser, member=superuser, role="Editor")
    with pytest.raises(ValidationError):
        invitations.update_member(actor=superuser, member=other_super, is_active=False)
    other_super.refresh_from_db()
    assert other_super.is_active and other_super.is_superuser


def test_update_member_requires_superuser(django_user_model):
    admin = member(django_user_model, "admin2", "Administrator")
    editor = member(django_user_model, "editor2", "Editor")
    with pytest.raises(PermissionDenied):
        invitations.update_member(actor=admin, member=editor, role="Administrator")


def test_update_member_role_change(superuser, django_user_model):
    editor = member(django_user_model, "editor3", "Editor")
    updated, changed = invitations.update_member(actor=superuser, member=editor, role="Administrator")
    updated.refresh_from_db()
    assert "role" in changed
    assert invitations.role_of(updated) == "Administrator"
    assert updated.is_staff is True
    assert updated.is_superuser is False

    updated, _ = invitations.update_member(actor=superuser, member=updated, role="Analyst", is_active=False)
    updated.refresh_from_db()
    assert invitations.role_of(updated) == "Analyst"
    assert updated.is_staff is False
    assert updated.is_active is False
