"""Two-step confirmation for API-key actions (MCP gated tools and REST publication).

``issue`` returns a signed token for a preview; ``consume`` accepts it once. A token is
bound to:

* the action name and the calling API key id (another key cannot use it);
* an HMAC (keyed with SECRET_KEY) of the exact arguments, so secret argument values
  are never readable from the token;
* an optional ``state`` fingerprint of the target (e.g. event revision), so a token
  previewed against old state is rejected after a concurrent change;
* a 10-minute lifetime and a random nonce recorded on use (``ConfirmationUse``), so
  it can be used once.

A token proves only that the holder of the key asked for the same change twice. It
is not independent human approval.
"""
import hashlib
import hmac
import json
import secrets

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction

SALT = "webinars.confirmation.v2"
MAX_AGE_SECONDS = 600
NOTE = (
    "confirmToken proves only that the holder of this API key requested the same change twice. "
    "It is not independent human approval; obtain that out of band if your process requires it."
)


class ConfirmationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _fingerprint(action, key, material, state):
    canonical = json.dumps([action, str(key.pk), material, state], sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(settings.SECRET_KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def issue(action, key, material, state=None):
    """Signed, timestamped token for ``action`` by ``key`` with exactly ``material``. No DB writes."""
    return signing.dumps({"f": _fingerprint(action, key, material, state), "n": secrets.token_hex(16)}, salt=SALT)


def consume(action, key, material, token, state=None):
    """Validate and burn ``token``. Raises ``ConfirmationError``."""
    from webinars.models import ConfirmationUse

    if not isinstance(token, str) or not token or len(token) > 2000:
        raise ConfirmationError("confirmation_required", "Preview first, then repeat with its confirmToken.")
    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE_SECONDS)
    except signing.SignatureExpired:
        raise ConfirmationError("confirmation_expired", "The confirmToken expired; preview again.")
    except signing.BadSignature:
        raise ConfirmationError("confirmation_invalid", "The confirmToken is invalid; preview again.")
    if not isinstance(data, dict) or not isinstance(data.get("n"), str):
        raise ConfirmationError("confirmation_invalid", "The confirmToken is invalid; preview again.")
    if not hmac.compare_digest(str(data.get("f", "")), _fingerprint(action, key, material, state)):
        raise ConfirmationError(
            "confirmation_mismatch",
            "The confirmToken does not match this key, these exact arguments or the target's current state; preview again.",
        )
    try:
        with transaction.atomic():
            ConfirmationUse.objects.create(nonce=data["n"], action=action[:64], api_key=key)
    except IntegrityError:
        raise ConfirmationError("confirmation_used", "This confirmToken was already used; preview again.")
