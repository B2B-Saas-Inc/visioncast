"""Opaque browser capability cookies and progressive-profiling missing-field computation.

The cookie value is a random secret; only its SHA-256 is stored. Public
person UUIDs, emails and ad identifiers are never accepted as authentication.
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from webinars.models import BrowserCapability
from webinars.normalization import CANONICAL_TO_MODEL, CUSTOM_PREFIX

COOKIE_NAME = getattr(settings, "CAPABILITY_COOKIE_NAME", "webinar_session")
MAX_TOKEN_LENGTH = 128
LAST_SEEN_INTERVAL = timedelta(hours=1)


def _cookie_name():
    return getattr(settings, "CAPABILITY_COOKIE_NAME", "webinar_session")


def _ttl():
    return timedelta(days=int(getattr(settings, "CAPABILITY_TTL_DAYS", 180)))


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_capability(person, *, mailbox_verified=False):
    """Create a capability for ``person`` and return the raw token (shown once).

    ``mailbox_verified`` only after an emailed code/link proved the address."""
    token = secrets.token_urlsafe(32)
    now = timezone.now()
    BrowserCapability.objects.create(
        person=person,
        token_hash=_hash(token),
        created_at=now,
        last_seen_at=now,
        expires_at=now + _ttl(),
        mailbox_verified_at=now if mailbox_verified else None,
    )
    return token


def unbound_token():
    """Random token with the same shape as a capability but never stored (anti-enumeration decoy)."""
    return secrets.token_urlsafe(32)


def resolve_capability(request):
    """Return the valid ``BrowserCapability`` for the request cookie, else ``None``."""
    token = request.COOKIES.get(_cookie_name())
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return None
    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        return None
    cap = (
        BrowserCapability.objects.select_related("person")
        .filter(token_hash=_hash(token))
        .first()
    )
    if cap is None:
        return None
    now = timezone.now()
    if cap.revoked_at is not None or cap.expires_at <= now or cap.person.erased_at is not None:
        return None
    if cap.last_seen_at is None or now - cap.last_seen_at >= LAST_SEEN_INTERVAL:
        BrowserCapability.objects.filter(pk=cap.pk).update(last_seen_at=now)
        cap.last_seen_at = now
    return cap


def revoke_capability(cap, reason):
    """Revoke ``cap`` (idempotent)."""
    if cap is None or cap.revoked_at is not None:
        return
    now = timezone.now()
    reason = (reason or "")[:64]
    BrowserCapability.objects.filter(pk=cap.pk, revoked_at__isnull=True).update(revoked_at=now, revoked_reason=reason)
    cap.revoked_at = now
    cap.revoked_reason = reason


def _cookie_kwargs():
    return {
        "path": "/",
        "secure": bool(getattr(settings, "CAPABILITY_COOKIE_SECURE", True)),
        "httponly": True,
        "samesite": "Lax",
    }


def set_capability_cookie(response, token):
    response.set_cookie(
        _cookie_name(),
        token,
        max_age=int(_ttl().total_seconds()),
        **_cookie_kwargs(),
    )
    return response


def clear_capability_cookie(response):
    response.set_cookie(
        _cookie_name(),
        "",
        max_age=0,
        expires="Thu, 01 Jan 1970 00:00:00 GMT",
        **_cookie_kwargs(),
    )
    return response


def _form_keys(form):
    """required + optional fields, then ``custom.<k>`` for each form custom field (deduped, ordered)."""
    keys = []
    candidates = list(form.required_fields or []) + list(form.optional_fields or [])
    candidates += [CUSTOM_PREFIX + k for k in (form.custom_fields or []) if isinstance(k, str)]
    for key in candidates:
        if isinstance(key, str) and key not in keys:
            keys.append(key)
    return keys


def missing_fields(person, form):
    """Return form field keys the person has not completed. No values are exposed."""
    keys = _form_keys(form)
    if person is None:
        return keys
    allowed_custom = set(form.custom_fields or [])
    stored_custom = person.custom_fields if isinstance(person.custom_fields, dict) else {}
    missing = []
    for key in keys:
        if key in CANONICAL_TO_MODEL:
            value = getattr(person, CANONICAL_TO_MODEL[key], "")
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(key)
        elif key.startswith(CUSTOM_PREFIX):
            bare = key[len(CUSTOM_PREFIX):]
            if bare in allowed_custom:
                value = stored_custom.get(key, stored_custom.get(bare))
                if value is None or (isinstance(value, str) and not value.strip()):
                    missing.append(key)
        else:
            missing.append(key)
    return missing
