"""Basic bounded fixed-window rate limiting for public endpoints.

Uses Django's cache. The default cache is per-process ``LocMemCache`` (bounded by
``MAX_ENTRIES``), so limits apply per Gunicorn worker: a coarse brake, not a
global quota. Production deployments must also rate-limit ``/w/`` and ``/mcp`` at
the reverse proxy (docs/operations.md). Client IPs are hashed before use as keys.
Per-email limits for verification codes are enforced in the database instead
(``webinars.waiting_room``), which is global.
"""
import hashlib
import time

from django.conf import settings
from django.core.cache import cache

# action -> (max requests, window seconds)
LIMITS = {
    "wr_status": (120, 60),
    "wr_identify": (10, 600),
    "wr_verify": (20, 600),
    "wr_register": (10, 600),
    "wr_enter": (30, 60),
    "mcp": (120, 60),
    "mcp_key": (300, 60),  # per API key (ratelimit.hit_key), across client addresses
    "invite_accept": (20, 600),
    "studio_login": (20, 600),
}


def _trusted_proxy_count():
    try:
        return max(1, int(getattr(settings, "RATE_LIMIT_TRUSTED_PROXY_COUNT", 1)))
    except (TypeError, ValueError):
        return 1


def client_ip(request):
    """The client address used as the rate-limit key.

    With ``RATE_LIMIT_TRUST_X_FORWARDED_FOR`` on, each of the
    ``RATE_LIMIT_TRUSTED_PROXY_COUNT`` (default 1) trusted proxies appends the address
    it received the request from. The client controls everything to the left of those
    entries, so the address is the Nth entry from the right, never the leftmost.
    """
    if getattr(settings, "RATE_LIMIT_TRUST_X_FORWARDED_FOR", False):
        hops = [h.strip() for h in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if h.strip()]
        if hops:
            count = _trusted_proxy_count()
            # Fewer hops than proxies: the chain is shorter than configured; the
            # leftmost entry is then the one the outermost proxy appended.
            return (hops[-count] if len(hops) >= count else hops[0])[:64]
    return (request.META.get("REMOTE_ADDR") or "unknown")[:64]


def hit_key(action, key, *, limit=None, window=None):
    """Count one request in ``action``'s bucket for an arbitrary identifier (for example
    an API key id for a per-key ``/mcp`` budget); return True when within the limit.

    ``limit``/``window`` default to ``LIMITS[action]``. The identifier is hashed.
    """
    if not getattr(settings, "RATE_LIMIT_ENABLED", True):
        return True
    default_limit, default_window = LIMITS.get(action, (None, None))
    limit = default_limit if limit is None else int(limit)
    window = default_window if window is None else int(window)
    if limit is None or window is None:
        raise KeyError(f"No rate limit configured for {action!r}")
    ident = hashlib.sha256(f"key|{key}".encode("utf-8")).hexdigest()[:32]
    return _count(f"rl:{action}:{int(time.time() // window)}:{ident}", limit, window)


def hit(request, action, *, key_suffix=""):
    """Count one request; return True when it is within the limit."""
    if not getattr(settings, "RATE_LIMIT_ENABLED", True):
        return True
    limit, window = LIMITS[action]
    bucket = int(time.time() // window)
    ident = hashlib.sha256(f"{client_ip(request)}|{key_suffix}".encode("utf-8")).hexdigest()[:32]
    return _count(f"rl:{action}:{bucket}:{ident}", limit, window)


def _count(key, limit, window):
    if cache.add(key, 1, timeout=window + 5):
        return True
    try:
        count = cache.incr(key)
    except ValueError:  # evicted between add and incr
        cache.add(key, 1, timeout=window + 5)
        return True
    return count <= limit
