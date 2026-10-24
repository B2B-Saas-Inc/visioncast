"""Hashed, scoped API keys.

Key format: ``wbk_<prefix8>_<secret>``. Only the SHA-256 of the *whole* raw key is
stored (``ApiKey.key_hash``); the 8-hex-char prefix is stored in clear for lookup.
"""
import hmac
import secrets
from datetime import timedelta

from django.utils import timezone

from webinars.crypto import generate_token, hash_token
from webinars.models import ApiKey

KEY_PREFIX = "wbk"
LAST_USED_GRANULARITY = timedelta(minutes=1)
_VALID_SCOPES = {choice for choice, _ in ApiKey.Scope.choices}
# Write scopes never imply each other or any read/register scope (see docs/management-api.md).
WRITE_SCOPES = (ApiKey.Scope.CONTENT_WRITE, ApiKey.Scope.ATTENDANCE_WRITE)


def generate_api_key(name, scopes):
    """Create an ApiKey and return ``(api_key, raw_key)``. The raw key is shown once."""
    scopes = list(dict.fromkeys(scopes or []))
    unknown = [s for s in scopes if s not in _VALID_SCOPES]
    if unknown:
        raise ValueError(f"Unknown scope(s): {', '.join(unknown)}")
    for _ in range(10):
        prefix = secrets.token_hex(4)
        if not ApiKey.objects.filter(prefix=prefix).exists():
            break
    else:  # pragma: no cover - astronomically unlikely
        raise RuntimeError("Could not allocate a unique API key prefix")
    raw = f"{KEY_PREFIX}_{prefix}_{generate_token(32)}"
    key = ApiKey.objects.create(name=name, prefix=prefix, key_hash=hash_token(raw), scopes=scopes)
    return key, raw


def _parse(raw):
    if not raw or not isinstance(raw, str) or len(raw) > 512:
        return None
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX or len(parts[1]) != 8 or not parts[2]:
        return None
    return parts[1]


def _raw_from_request(request):
    auth = request.headers.get("Authorization", "")
    if auth:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    for header in ("X-Webinar-Proxy-Key", "X-Api-Key"):
        value = request.headers.get(header, "").strip()
        if value:
            return value
    return None


def authenticate_raw_key(raw):
    prefix = _parse(raw)
    if prefix is None:
        # Still burn a hash to keep timing roughly uniform.
        hash_token(raw or "")
        return None
    key = ApiKey.objects.filter(prefix=prefix).first()
    candidate = hash_token(raw)
    if key is None or not hmac.compare_digest(candidate, key.key_hash):
        return None
    if key.revoked_at is not None:
        return None
    now = timezone.now()
    if key.last_used_at is None or now - key.last_used_at >= LAST_USED_GRANULARITY:
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)
        key.last_used_at = now
    return key


def api_key_from_request(request):
    """Return the active ApiKey for the request, or None."""
    return authenticate_raw_key(_raw_from_request(request))


def has_scope(key, *scopes):
    """True when ``key`` is active and holds at least one of ``scopes``."""
    if key is None or key.revoked_at is not None:
        return False
    held = set(key.scopes or [])
    return any(s in held for s in scopes)
