"""Public catalogue queries shared by the HTTP API (``webinars.api.views``) and MCP.

One implementation of the public scoping rules so both transports return the same
results for the same filters:

* events: ``visibility=public`` for listings, ``public``/``unlisted`` for lookup by slug;
* ``q`` matches the public title or the summary (case-insensitive, max 200 chars);
* ``when=upcoming``: at least one *scheduled* occurrence starting in the future;
  ``when=recorded``: at least one occurrence with a published recording;
* filters are applied in SQL before pagination, so ``count`` is the total number of
  matches and pages are never short;
* ordering is ``-created_at, pk`` (stable);
* form schemas are visible only for active form versions or versions used by a
  public/unlisted event.

Nothing here touches private links or person data.
"""
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.utils import timezone

from webinars.models import Event, FormVersion, Occurrence

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_QUERY_LENGTH = 200
WHEN_VALUES = ("upcoming", "recorded")
LISTED_VISIBILITIES = (Event.Visibility.PUBLIC, Event.Visibility.UNLISTED)


class QueryError(ValueError):
    """Invalid filter or pagination input (maps to a 400 / MCP tool error)."""

    def __init__(self, message, fields):
        super().__init__(message)
        self.message = message
        self.fields = fields


def event_queryset():
    """Events with everything the public projection needs prefetched."""
    return Event.objects.select_related("form_version", "cover_image").prefetch_related(
        "hosts", "topics", Prefetch("occurrences", queryset=Occurrence.objects.order_by("starts_at"))
    )


def filter_events(qs, *, q="", topic="", host="", when="", search_internal_name=False, now=None):
    """Apply the catalogue filters to ``qs`` and return a distinct, ordered queryset.

    ``search_internal_name`` additionally matches ``internal_name`` (management
    projections only; the internal name is never public).
    """
    q = (q or "").strip()[:MAX_QUERY_LENGTH]
    if q:
        match = Q(public_title__icontains=q) | Q(summary__icontains=q)
        if search_internal_name:
            match |= Q(internal_name__icontains=q)
        qs = qs.filter(match)
    topic = (topic or "").strip()
    if topic:
        qs = qs.filter(topics__slug=topic)
    host = (host or "").strip()
    if host:
        qs = qs.filter(hosts__slug=host)

    when = (when or "").strip()
    now = now or timezone.now()
    if when == "upcoming":
        qs = qs.filter(
            Exists(Occurrence.objects.filter(event=OuterRef("pk"), status=Occurrence.Status.SCHEDULED, starts_at__gt=now))
        )
    elif when == "recorded":
        qs = qs.filter(
            Exists(
                Occurrence.objects.filter(event=OuterRef("pk"), recording_published_at__isnull=False).exclude(
                    recording_url=""
                )
            )
        )
    elif when:
        raise QueryError("Invalid filter.", {"when": "Must be upcoming or recorded."})
    return qs.distinct().order_by("-created_at", "pk")


def public_events(**filters):
    """Publicly listed events (``visibility=public``) matching ``filters``."""
    return filter_events(event_queryset().filter(visibility=Event.Visibility.PUBLIC), **filters)


def parse_page(limit=None, offset=None, *, max_limit=MAX_LIMIT):
    """Validate pagination from query strings or ints; return ``(limit, offset)``."""
    try:
        if isinstance(limit, bool) or isinstance(offset, bool):
            raise ValueError
        limit = DEFAULT_LIMIT if limit is None else int(limit)
        offset = 0 if offset is None else int(offset)
    except (TypeError, ValueError):
        raise QueryError("Invalid pagination.", {"limit": "Must be an integer."})
    if limit < 1 or limit > max_limit or offset < 0:
        raise QueryError("Invalid pagination.", {"limit": f"1-{max_limit}; offset >= 0."})
    return limit, offset


def page(qs, limit, offset):
    """Return ``(total_count, rows)`` for an already filtered and ordered queryset."""
    return qs.count(), list(qs[offset : offset + limit])


def public_event(slug):
    """A public or unlisted event by slug, or ``None``."""
    if not isinstance(slug, str) or not slug:
        return None
    return event_queryset().filter(slug=slug, visibility__in=LISTED_VISIBILITIES).first()


def public_form_version(version):
    """An active form version, or one used by a public/unlisted event; else ``None``."""
    if not isinstance(version, str) or not version:
        return None
    return (
        FormVersion.objects.filter(version=version)
        .filter(Q(is_active=True) | Q(events__visibility__in=LISTED_VISIBILITIES))
        .distinct()
        .first()
    )
