"""Team invitations and membership changes (superuser-only at the caller).

Rules (unchanged from webinars/roles.py): role groups never carry auth write
permissions; only superusers manage users. Therefore:

* Only superusers create/revoke invitations or change roles/emails/active flags in the
  studio. Separately, a ``team_admin`` API key (granted only by a superuser) may do so
  for Editor/Analyst access only (``KEY_MANAGED_ROLES``); roles and groups are unchanged.
* Invitations grant exactly one of the three role groups, never superuser or staff
  beyond what the role needs (Administrators get ``is_staff`` for the maintenance
  admin; Editors/Analysts work in the studio only).
* Nobody changes their own role or deactivates themselves; superuser accounts are
  not edited here (use ``bootstrap_admin`` / Django admin as a superuser).
* Acceptance URL: ``/studio/invite/<token>/``; only SHA-256(token) is stored; the
  token expires after ``INVITATION_TTL_HOURS`` and is single-use. Acceptance locks
  the invitation row and creates the user in one transaction.
"""
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from webinars import mail
from webinars.audit import audit
from webinars.crypto import hash_token
from webinars.models import Invitation
from webinars.normalization import FieldError, normalize_email
from webinars.roles import ADMINISTRATOR, ANALYST, EDITOR

ROLES = (ADMINISTRATOR, EDITOR, ANALYST)


def _ttl():
    return timedelta(hours=int(getattr(settings, "INVITATION_TTL_HOURS", 72)))


def accept_path(token):
    return f"/studio/invite/{token}/"


def accept_url(token):
    return (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/") + accept_path(token)


def role_of(user):
    if user.is_superuser:
        return "Superuser"
    names = set(user.groups.values_list("name", flat=True))
    for role in ROLES:
        if role in names:
            return role
    return None


def _require_superuser(actor):
    if actor is None or not actor.is_active or not actor.is_superuser:
        raise PermissionDenied("Only superusers manage the team.")


# A ``team_admin`` API key (granted only by a superuser) may manage Editors and Analysts.
# It can never grant or touch Administrator, staff or superuser accounts, so a key can
# never create a person able to mint keys or manage users. It also cannot create a human
# with more authority than the key itself: granting (or changing an account holding) a
# role needs every scope that role's studio permissions amount to. Editors publish in the
# studio without the API confirmation step, so Editor needs content_write + publication;
# Analysts see registrations and reports, so Analyst needs reporting.
KEY_MANAGED_ROLES = (EDITOR, ANALYST)
ROLE_REQUIRED_SCOPES = {
    EDITOR: ("content_write", "publication"),
    ANALYST: ("reporting",),
}


def missing_scopes_for_role(api_key, role):
    """Scopes ``api_key`` lacks to grant or manage ``role`` (empty when allowed)."""
    held = set(api_key.scopes or [])
    return [s for s in ROLE_REQUIRED_SCOPES.get(role, ()) if s not in held]


def _require_actor(actor, api_key, role=None):
    """Superuser user, or an active ``team_admin`` key limited to KEY_MANAGED_ROLES and
    to roles whose privileges the key already holds."""
    if api_key is None:
        _require_superuser(actor)
        return
    from webinars.api.auth import has_scope
    from webinars.models import ApiKey

    if actor is not None or not has_scope(api_key, ApiKey.Scope.TEAM_ADMIN):
        raise PermissionDenied("This API key cannot manage the team.")
    if role is not None and role not in KEY_MANAGED_ROLES:
        raise PermissionDenied("API keys may only manage Editor and Analyst access.")
    if role is not None:
        missing = missing_scopes_for_role(api_key, role)
        if missing:
            raise PermissionDenied(f"Granting {role} needs this key to hold: {', '.join(missing)}.")


def key_may_manage(member, api_key=None):
    """True when a team_admin key may change ``member``: not superuser/staff, no direct
    permissions, groups limited to one KEY_MANAGED_ROLES group (or none) and, with
    ``api_key``, a current role whose privileges the key holds."""
    if member.is_superuser or member.is_staff or member.user_permissions.exists():
        return False
    groups = list(member.groups.values_list("name", flat=True))
    if len(groups) > 1 or any(g not in KEY_MANAGED_ROLES for g in groups):
        return False
    if api_key is not None and groups and missing_scopes_for_role(api_key, groups[0]):
        return False
    return True


def _clean_email(raw):
    try:
        return normalize_email(raw or "")
    except FieldError as exc:
        raise ValidationError({"email": exc.message})


def create_invitation(*, actor, email, role, api_key=None):
    """Returns ``(invitation, raw_token)``. Show the link once; it is never stored.

    With ``api_key`` (team_admin) the caller must discard the token: it is delivered
    only by email and never returned over the API.
    """
    _require_actor(actor, api_key, role if role in ROLES else None)
    if role not in ROLES:
        raise ValidationError({"role": "Choose Administrator, Editor or Analyst."})
    email = _clean_email(email)
    User = get_user_model()
    if User.objects.filter(email__iexact=email).exists():
        raise ValidationError({"email": "Someone on the team already uses this email."})
    token = secrets.token_urlsafe(32)
    now = timezone.now()
    with transaction.atomic():
        # One live invitation per address: older pending ones are revoked.
        Invitation.objects.filter(
            email__iexact=email, accepted_at__isnull=True, revoked_at__isnull=True, expires_at__gt=now
        ).update(revoked_at=now)
        invitation = Invitation.objects.create(
            email=email, role=role, token_hash=hash_token(token), invited_by=actor, created_at=now, expires_at=now + _ttl()
        )
        audit("invitation.created", user=actor, api_key=api_key, obj=invitation, role=role)
    sent = mail.send(
        email,
        "You're invited to the webinar studio",
        (
            f"You have been invited to join the webinar studio as {role}.\n\n"
            f"Accept the invitation and choose your password here:\n{accept_url(token)}\n\n"
            f"The link works once and expires in {int(_ttl().total_seconds() // 3600)} hours.\n"
        ),
    )
    if sent:
        Invitation.objects.filter(pk=invitation.pk).update(email_sent=True)
        invitation.email_sent = True
    return invitation, token


def revoke_invitation(*, actor, invitation, api_key=None):
    _require_actor(actor, api_key, invitation.role if api_key is not None else None)
    updated = Invitation.objects.filter(pk=invitation.pk, accepted_at__isnull=True, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
    if updated:
        audit("invitation.revoked", user=actor, api_key=api_key, obj=invitation)
    return bool(updated)


def find_pending(token):
    """The usable invitation for ``token`` or None (expired/used/revoked/unknown look the same)."""
    if not token or len(token) > 128:
        return None
    invitation = Invitation.objects.filter(token_hash=hash_token(token)).first()
    if invitation is None or invitation.status != "pending":
        return None
    return invitation


def accept_invitation(token, *, username, password, password_confirm):
    """Create the user and consume the invitation atomically. Returns the user."""
    username = (username or "").strip()
    errors = {}
    User = get_user_model()
    if not username or len(username) > 150:
        errors["username"] = "Choose a username (up to 150 characters)."
    elif not all(c.isalnum() or c in "@.+-_" for c in username):
        errors["username"] = "Use letters, digits and @ . + - _ only."
    if password != password_confirm:
        errors["password_confirm"] = "The passwords don't match."
    if errors:
        raise ValidationError(errors)
    try:
        with transaction.atomic():
            invitation = (
                Invitation.objects.select_for_update().filter(token_hash=hash_token(token or "")).first()
            )
            if invitation is None or invitation.status != "pending":
                raise ValidationError({"_": "This invitation link is no longer valid. Ask for a new one."})
            if User.objects.filter(username__iexact=username).exists():
                raise ValidationError({"username": "That username is taken."})
            if User.objects.filter(email__iexact=invitation.email).exists():
                raise ValidationError({"_": "An account already uses this email. Ask a superuser for help."})
            candidate = User(username=username, email=invitation.email)
            try:
                validate_password(password, user=candidate)
            except ValidationError as exc:
                raise ValidationError({"password": " ".join(exc.messages)})
            user = User.objects.create_user(username=username, email=invitation.email, password=password)
            user.is_staff = invitation.role == ADMINISTRATOR
            user.is_superuser = False
            user.save(update_fields=["is_staff", "is_superuser"])
            user.groups.set([Group.objects.get(name=invitation.role)])
            invitation.accepted_at = timezone.now()
            invitation.accepted_user = user
            invitation.save(update_fields=["accepted_at", "accepted_user"])
            audit("invitation.accepted", user=user, obj=invitation, role=invitation.role)
    except IntegrityError:
        raise ValidationError({"username": "That username is taken."})
    return user


def update_member(*, actor, member, role=None, email=None, is_active=None, api_key=None):
    """Change another (non-superuser) member's role, email or active flag."""
    _require_actor(actor, api_key, role)
    if api_key is None and member.pk == actor.pk:
        raise ValidationError({"_": "You can't change your own role or access here."})
    if member.is_superuser:
        raise ValidationError({"_": "Superuser accounts are managed with bootstrap_admin and the maintenance admin."})
    changed = []
    with transaction.atomic():
        User = get_user_model()
        member = User.objects.select_for_update().get(pk=member.pk)
        if api_key is not None and not key_may_manage(member, api_key):
            raise PermissionDenied(
                "API keys may only manage Editor/Analyst accounts without extra permissions, "
                "and only roles whose scopes the key holds."
            )
        if email is not None:
            email = _clean_email(email)
            if User.objects.filter(email__iexact=email).exclude(pk=member.pk).exists():
                raise ValidationError({"email": "Someone on the team already uses this email."})
            if email != member.email:
                member.email = email
                changed.append("email")
        if is_active is not None and bool(is_active) != member.is_active:
            member.is_active = bool(is_active)
            changed.append("is_active")
        if role is not None:
            if role not in ROLES:
                raise ValidationError({"role": "Choose Administrator, Editor or Analyst."})
            if role_of(member) != role:
                member.groups.set([Group.objects.get(name=role)])
                member.is_staff = role == ADMINISTRATOR
                changed += ["role", "is_staff"]
        if changed:
            member.save(update_fields=[f for f in changed if f in ("email", "is_active", "is_staff")] or None)
            audit("member.updated", user=actor, api_key=api_key, obj=member, fields=sorted(set(changed)), role=role_of(member))
    return member, changed
