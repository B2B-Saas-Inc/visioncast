"""IANA timezone validation and DST-aware local -> UTC conversion."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError


def validate_timezone(name):
    """Return the ``ZoneInfo`` for an IANA name or raise ``ValidationError``."""
    if not isinstance(name, str) or not name or len(name) > 64 or name != name.strip():
        raise ValidationError("Unknown timezone.", code="invalid_timezone")
    if name.startswith("/") or ".." in name or "\\" in name:
        raise ValidationError("Unknown timezone.", code="invalid_timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValidationError("Unknown timezone.", code="invalid_timezone")


def local_to_utc(local_naive, tz_name, fold):
    """Convert a naive wall-clock time in ``tz_name`` to an aware UTC datetime.

    Raises ``ValidationError(code="nonexistent")`` for times skipped by a
    spring-forward transition and ``ValidationError(code="ambiguous")`` for
    fall-back overlaps when ``fold`` is ``None``.
    """
    if not isinstance(local_naive, datetime):
        raise ValidationError("A local date and time is required.", code="invalid")
    if local_naive.tzinfo is not None:
        # Occurrence.local_start is a DateTimeField; with USE_TZ Django returns the
        # stored wall-clock value tagged as UTC. Only that zero-offset form is accepted.
        if local_naive.utcoffset() != timedelta(0):
            raise ValidationError("Local start must be a naive wall-clock time.", code="invalid")
        local_naive = local_naive.replace(tzinfo=None)
    if fold not in (None, 0, 1):
        raise ValidationError("fold must be 0, 1 or null.", code="invalid_fold")
    tz = validate_timezone(tz_name)
    wall = local_naive.replace(tzinfo=None, fold=0)

    candidates = []
    for f in (0, 1):
        aware = wall.replace(tzinfo=tz, fold=f)
        back = aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None, fold=0)
        candidates.append((aware, back == wall))

    if not candidates[0][1] and not candidates[1][1]:
        raise ValidationError(
            "This local time does not exist in %(tz)s (daylight saving gap).",
            code="nonexistent",
            params={"tz": tz_name},
        )

    off0 = candidates[0][0].utcoffset()
    off1 = candidates[1][0].utcoffset()
    if off0 != off1:
        if fold is None:
            raise ValidationError(
                "This local time occurs twice in %(tz)s; choose which occurrence (fold 0 or 1).",
                code="ambiguous",
                params={"tz": tz_name},
            )
        chosen = candidates[fold][0]
    else:
        chosen = candidates[0][0]
    return chosen.astimezone(timezone.utc)
