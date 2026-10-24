"""Audit logging. Never pass PII (emails, names, phones) or secrets in metadata."""
from webinars.models import AuditLog

_FORBIDDEN_KEYS = {"email", "phone", "first_name", "last_name", "name", "secret", "token", "password", "key", "url"}


def audit(action, *, user=None, api_key=None, obj=None, **metadata):
    clean = {k: v for k, v in metadata.items() if k.lower() not in _FORBIDDEN_KEYS}
    return AuditLog.objects.create(
        actor_user=user if (user is not None and getattr(user, "is_authenticated", False)) else None,
        actor_api_key=api_key,
        action=action,
        object_type=type(obj).__name__ if obj is not None else "",
        object_id=str(obj.pk) if obj is not None else "",
        metadata=clean,
    )
