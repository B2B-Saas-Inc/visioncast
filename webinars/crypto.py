"""Secret encryption, token hashing and webhook signing.

The Fernet master key comes from ``settings.INTEGRATION_MASTER_KEY`` (environment,
never stored in the database). Rotation: set the new key as
``INTEGRATION_MASTER_KEY`` and list old keys in ``INTEGRATION_MASTER_KEY_PREVIOUS``;
decryption tries every key, encryption always uses the current one. Use
``rotate_secret(token)`` (or ``set_destination_secret``) to re-encrypt.
"""
import hashlib
import hmac
import secrets
import time

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

SIGNATURE_VERSION = "v1"


class DecryptionError(Exception):
    """Ciphertext could not be decrypted with any configured key."""


def _as_list(value):
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [v for v in value if v]


def _fernet():
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError as exc:  # pragma: no cover
        raise ImproperlyConfigured("The 'cryptography' package is required.") from exc

    current = getattr(settings, "INTEGRATION_MASTER_KEY", None)
    if not current:
        raise ImproperlyConfigured("INTEGRATION_MASTER_KEY is not configured.")
    keys = [current] + _as_list(getattr(settings, "INTEGRATION_MASTER_KEY_PREVIOUS", None))
    fernets = []
    for k in keys:
        try:
            fernets.append(Fernet(k.encode() if isinstance(k, str) else k))
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                "INTEGRATION_MASTER_KEY(_PREVIOUS) must be url-safe base64-encoded 32-byte Fernet keys."
            ) from exc
    return MultiFernet(fernets)


def encrypt_secret(plain: str) -> str:
    if plain is None:
        raise ValueError("Cannot encrypt None")
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    from cryptography.fernet import InvalidToken

    if not token:
        raise DecryptionError("No ciphertext")
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        raise DecryptionError("Secret could not be decrypted with the configured keys") from None


def rotate_secret(token: str) -> str:
    """Re-encrypt ``token`` under the current master key."""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().rotate(token.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError):
        raise DecryptionError("Secret could not be decrypted with the configured keys") from None


def hash_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _signed_content(delivery_id, timestamp, body: bytes) -> bytes:
    if isinstance(body, str):
        body = body.encode("utf-8")
    return f"{delivery_id}.{int(timestamp)}.".encode("utf-8") + body


def sign_webhook(secret: str, delivery_id: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), _signed_content(delivery_id, timestamp, body), hashlib.sha256)
    return f"{SIGNATURE_VERSION}={mac.hexdigest()}"


def verify_webhook_signature(secret, delivery_id, timestamp, body, header, tolerance=300, now=None) -> bool:
    """Verify a ``Webinar-Signature`` header. ``header`` may hold several
    comma/space separated ``v1=<hex>`` values (e.g. during secret rotation)."""
    if not header or not secret or not delivery_id:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = int(time.time()) if now is None else int(now)
    if abs(now - ts) > tolerance:
        return False
    expected = sign_webhook(secret, str(delivery_id), ts, body)
    ok = False
    for candidate in header.replace(",", " ").split():
        if hmac.compare_digest(candidate.strip().encode(), expected.encode()):
            ok = True
    return ok
