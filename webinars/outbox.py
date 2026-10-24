"""Transactional outbox: payload building and destination fan-out.

``enqueue_event`` must run inside the same ``transaction.atomic()`` block as the
business change so that a webhook exists if and only if the change committed.

Payload (schemaVersion "1") is grouped into ``identity``, ``profile``,
``organization``, ``consents``, ``attribution``, ``registration``, ``event`` and
``delivery_metadata``. Each destination receives only the groups listed in
``Destination.allowed_groups`` (``event`` and ``delivery_metadata`` always; when
``allowed_groups`` is empty: ``event``, ``registration`` and ``delivery_metadata``).
``Destination.field_mapping`` (``{"profile.email": "contact_email"}``) adds a
top-level ``mapped`` object built only from groups the destination may receive.

Links in ``event.occurrence`` (``occurrence_links``): when the occurrence's managed
waiting room is enabled only ``managedRoomUrl`` (the stable ``/w/<code>/`` URL) is
sent and the provider ``joinUrl``/``waitingRoomUrl`` never are, whatever the
destination allows; otherwise the provider links are sent only to destinations with
``include_private_links``.

Stored vs. sent: the stored ``OutboxMessage.payload`` is the enqueue-time snapshot.
Its groups are the canonical (unmapped) data the destination was allowed at enqueue;
``mapped`` is derived only. ``refilter_for_send`` rebuilds the body at send time from
those canonical groups against the *current* destination and occurrence, so turning
off ``include_private_links``, enabling the managed room, narrowing ``allowed_groups``
or changing ``field_mapping`` applies to messages already queued. Groups never widen
beyond the snapshot.
"""
import copy
import datetime
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from webinars.models import (
    AttributionTouch,
    ConsentRecord,
    Destination,
    Occurrence,
    OrganizationExternalId,
    OutboxMessage,
)

SCHEMA_VERSION = "1"

EVENT_TYPES = (
    "registration.created",
    "registration.updated",
    "occurrence.cancelled",
    "occurrence.rescheduled",
    "reminder.due",
    "recording.published",
    "attendance.recorded",
    # Waiting-room admission (webinars/waiting_room.py): exactly one per registration.
    "attendance.checked_in",
    "follow_up.requested",
)

ALWAYS_GROUPS = ("event", "delivery_metadata")
DEFAULT_GROUPS = ("event", "registration", "delivery_metadata")
PERSON_GROUPS = ("identity", "profile", "organization", "consents", "attribution")
TOP_LEVEL_KEYS = ("schemaVersion", "eventType", "occurredAt")
MAPPED_KEY = "mapped"

# Link keys inside ``event.occurrence``. The provider links are private; the managed
# room URL is attendee-facing (meant to be shared) and replaces them when the room is on.
PRIVATE_LINK_KEYS = ("joinUrl", "waitingRoomUrl")
MANAGED_ROOM_KEY = "managedRoomUrl"
LINK_KEYS = PRIVATE_LINK_KEYS + (MANAGED_ROOM_KEY,)

# Canonical camelCase -> Person model attribute.
PROFILE_FIELDS = {
    "email": "email",
    "phone": "phone",
    "firstName": "first_name",
    "lastName": "last_name",
    "name": "name",
    "company": "company",
    "jobTitle": "job_title",
    "companySize": "company_size",
}

CONSENT_PURPOSES = [p for p, _ in ConsentRecord.Purpose.choices]


def _iso_utc(dt):
    if dt is None:
        return None
    return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Consent helpers
# --------------------------------------------------------------------------


def latest_consents(person):
    """Return {purpose: ConsentRecord|None} using the latest record per purpose."""
    result = {p: None for p in CONSENT_PURPOSES}
    if person is None:
        return result
    for record in ConsentRecord.objects.filter(person=person).order_by("-recorded_at", "-id"):
        if result.get(record.purpose) is None:
            result[record.purpose] = record
        if all(result.values()):
            break
    return result


def _consent_granted(record):
    return record is not None and record.granted and record.withdrawn_at is None


def consents_satisfied(destination, person, consents=None):
    required = list(destination.required_consents or [])
    if not required:
        return True
    if person is None:
        return False
    consents = consents if consents is not None else latest_consents(person)
    return all(_consent_granted(consents.get(purpose)) for purpose in required)


# --------------------------------------------------------------------------
# Group builders
# --------------------------------------------------------------------------


def _profile_from_person(person):
    profile = {key: getattr(person, attr) or None for key, attr in PROFILE_FIELDS.items()}
    profile["customFields"] = dict(person.custom_fields or {})
    return profile


def _profile_from_submission(registration):
    submitted = registration.submitted_profile or {}
    profile = {key: (submitted.get(key) or submitted.get(attr) or None) for key, attr in PROFILE_FIELDS.items()}
    custom = dict(registration.custom_fields or {})
    if isinstance(submitted.get("customFields"), dict):
        custom = {**submitted["customFields"], **custom}
    profile["customFields"] = custom
    return profile


def _organization(person, verified_only):
    qs = OrganizationExternalId.objects.filter(person=person).order_by("namespace", "value", "source")
    if verified_only:
        qs = qs.filter(verified=True)
    return {
        "externalIds": [
            {"namespace": o.namespace, "value": o.value, "verified": o.verified, "source": o.source} for o in qs
        ]
    }


def _consent_group(consents):
    group = {}
    for purpose in CONSENT_PURPOSES:
        record = consents.get(purpose)
        if record is None:
            group[purpose] = {
                "granted": False,
                "disclosureVersion": None,
                "recordedAt": None,
                "source": None,
                "serverVerified": False,
            }
        else:
            group[purpose] = {
                "granted": _consent_granted(record),
                "disclosureVersion": record.disclosure_version or None,
                "recordedAt": _iso_utc(record.recorded_at),
                "withdrawnAt": _iso_utc(record.withdrawn_at),
                "source": record.source,
                "serverVerified": bool(record.server_verified),
            }
    return group


def _touch(t):
    if t is None:
        return None
    return {
        "parameters": dict(t.parameters or {}),
        "identifiers": dict(t.identifiers or {}),
        "pageUrl": t.page_url or None,
        "referrer": t.referrer or None,
        "recordedAt": _iso_utc(t.created_at),
    }


def _attribution(person, registration, include_first_touch=True):
    first = None
    if include_first_touch:
        first = AttributionTouch.objects.filter(person=person, touch_type=AttributionTouch.TouchType.FIRST_TOUCH).first()
    reg_touch = None
    if registration is not None:
        reg_touch = (
            AttributionTouch.objects.filter(
                registration=registration, touch_type=AttributionTouch.TouchType.REGISTRATION_TOUCH
            )
            .order_by("-created_at")
            .first()
        )
    return {"firstTouch": _touch(first), "registrationTouch": _touch(reg_touch)}


def _submitted_organization(registration):
    submitted = (registration.submitted_profile or {}).get("organization") or {}
    ids = submitted.get("externalIds") if isinstance(submitted, dict) else None
    return {
        "externalIds": [
            {"namespace": i.get("namespace"), "value": i.get("value"), "verified": False, "source": OrganizationExternalId.Source.BROWSER}
            for i in (ids or [])
            if isinstance(i, dict)
        ]
    }


def registration_consents(registration):
    """Consents that may be paired with ``registration``'s profile.

    An unverified registration carries a submitted (unauthenticated) profile, so the
    person's real consent records must never be attached to it: every purpose is reported
    as not granted and destinations that require consent do not receive it.
    """
    if registration is not None and not registration.identity_verified:
        return {p: None for p in CONSENT_PURPOSES}
    return latest_consents(registration.person if registration is not None else None)


def _registration_group(registration):
    return {
        "id": str(registration.id),
        "status": registration.status,
        "attendance": registration.attendance,
        "formVersion": registration.form_version,
        "identityVerified": bool(registration.identity_verified),
        "createdAt": _iso_utc(registration.created_at),
        "updatedAt": _iso_utc(registration.updated_at),
    }


def occurrence_links(occurrence, include_private_links):
    """Link fields for ``event.occurrence``; the one place the link policy lives.

    * Managed waiting room enabled: only ``managedRoomUrl``. The provider links are
      never included, even for ``include_private_links`` destinations, so every
      attendee goes through the room (admission, check-in, the stable URL).
    * Otherwise: ``joinUrl``/``waitingRoomUrl`` only when ``include_private_links``.
    """
    if occurrence is None:
        return {}
    if occurrence.waiting_room_enabled:
        # Lazy import: webinars.waiting_room imports this module.
        from webinars.waiting_room import room_url

        url = room_url(occurrence)
        return {MANAGED_ROOM_KEY: url} if url else {}
    if include_private_links:
        return {"joinUrl": occurrence.join_url or None, "waitingRoomUrl": occurrence.waiting_room_url or None}
    return {}


def _event_group(occurrence, include_private_links):
    event = occurrence.event
    if event.thank_you_mode == event.ThankYouMode.REDIRECT:
        thank_you = {"mode": "redirect", "redirectUrl": event.thank_you_url}
    else:
        thank_you = {"mode": "message", "message": event.thank_you_message}
    occ = {
        "id": str(occurrence.id),
        "startsAt": _iso_utc(occurrence.starts_at),
        "endsAt": _iso_utc(occurrence.ends_at),
        "timezone": occurrence.timezone,
        "status": occurrence.status,
        "icsUid": occurrence.ics_uid,
        "icsSequence": occurrence.ics_sequence,
        "cancelledAt": _iso_utc(occurrence.cancelled_at),
    }
    if occurrence.recording_published:
        occ["recordingUrl"] = occurrence.recording_url
        occ["recordingPublishedAt"] = _iso_utc(occurrence.recording_published_at)
    occ.update(occurrence_links(occurrence, include_private_links))
    return {
        "id": str(event.id),
        "slug": event.slug,
        "title": event.public_title,
        "summary": event.summary,
        "heroImageUrl": event.hero_url or None,
        "aida": {
            "attention": event.attention,
            "interest": event.interest,
            "decision": event.decision,
            "action": event.action,
        },
        "learningOutcomes": list(event.learning_outcomes or []),
        "hosts": [
            {"name": h.name, "slug": h.slug, "title": h.title, "imageUrl": h.image_url or None}
            for h in event.hosts.all().order_by("name")
        ],
        "topics": [{"name": t.name, "slug": t.slug} for t in event.topics.all().order_by("name")],
        "thankYou": thank_you,
        "cta": event.cta,
        "revision": event.revision,
        "occurrence": occ,
    }


def build_full_payload(event_type, *, registration, occurrence, destination, delivery_id, consents=None):
    """Unfiltered payload (all groups). Use ``build_payload`` for delivery."""
    if occurrence is None and registration is not None:
        occurrence = registration.occurrence
    if occurrence is None:
        raise ValueError("An occurrence or registration is required")
    now = timezone.now()
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "eventType": event_type,
        "occurredAt": _iso_utc(now),
    }
    if registration is not None:
        person = registration.person
        verified = bool(registration.identity_verified)
        profile = _profile_from_person(person) if verified else _profile_from_submission(registration)
        payload["identity"] = {
            "personId": str(person.id),
            "email": profile.get("email"),
            "verified": verified,
        }
        payload["profile"] = profile
        if verified:
            payload["organization"] = _organization(person, verified_only=False)
            payload["consents"] = _consent_group(consents if consents is not None else latest_consents(person))
        else:
            # Unverified: only what this submission supplied. The person's real consents,
            # org IDs and first touch are never paired with a submitted profile.
            payload["organization"] = _submitted_organization(registration)
            payload["consents"] = _consent_group(registration_consents(registration))
        payload["attribution"] = _attribution(person, registration, include_first_touch=verified)
        payload["registration"] = _registration_group(registration)
    payload["event"] = _event_group(occurrence, bool(destination.include_private_links))
    payload["delivery_metadata"] = {
        "deliveryId": str(delivery_id),
        "destinationId": str(destination.id),
        "destinationKind": destination.kind,
        "eventType": event_type,
        "createdAt": _iso_utc(now),
    }
    return payload


def _get_path(data, path):
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return copy.deepcopy(current)


def filter_for_destination(payload, destination):
    """Keep the groups ``destination`` may receive and add ``mapped`` from its mapping.

    ``payload`` must be canonical: any ``mapped`` object in it is ignored (it is not a
    group), and mapping sources resolve only against canonical group paths, so mapping
    is applied exactly once and never chains (``a -> b`` and ``b -> c`` do not compose).
    """
    allowed = [g for g in (destination.allowed_groups or []) if g in Destination.GROUPS]
    keep = set(ALWAYS_GROUPS) | set(allowed) if allowed else set(DEFAULT_GROUPS)
    out = {k: payload[k] for k in TOP_LEVEL_KEYS if k in payload}
    for group, value in payload.items():
        if group in TOP_LEVEL_KEYS:
            continue
        if group in keep:
            out[group] = value
    mapping = destination.field_mapping or {}
    if isinstance(mapping, dict) and mapping:
        mapped = {}
        for source_path, target in mapping.items():
            if not isinstance(source_path, str) or not isinstance(target, str) or not target:
                continue
            # Only paths inside groups this destination may receive can be mapped.
            if source_path.split(".", 1)[0] not in out:
                continue
            mapped[target] = _get_path(out, source_path)
        out[MAPPED_KEY] = mapped
    return out


def _current_occurrence(occurrence_id):
    if not occurrence_id:
        return None
    try:
        return Occurrence.objects.filter(pk=occurrence_id).first()
    except (ValueError, ValidationError):
        return None


def refilter_for_send(payload, destination):
    """Body for a queued message, re-filtered against the *current* state.

    ``payload`` is the stored enqueue-time snapshot. Its groups are canonical
    (unmapped); the stored ``mapped`` object is derived data and is discarded, then
    rebuilt once from the canonical groups with the destination's current
    ``field_mapping``. Rows queued before this function existed have the same shape,
    so a private link stored under a mapped name (``mapped.join_link``) disappears with
    the old ``mapped`` object and is only re-derived if the current policy allows it.

    Then:

    * occurrence links are recomputed from the current occurrence and destination
      (``occurrence_links``): managed room on -> ``managedRoomUrl`` only; room off ->
      provider links only with ``include_private_links``; occurrence gone -> none;
    * groups are narrowed to the current ``allowed_groups`` (never widened beyond the
      snapshot: data the destination was not allowed at enqueue was never stored).

    The delivery id and event type are untouched; consent/erasure checks happen in
    ``webinars.delivery.send_time_block_reason`` before this is called.
    """
    if not isinstance(payload, dict):
        return payload
    canonical = copy.deepcopy(payload)
    canonical.pop(MAPPED_KEY, None)
    event = canonical.get("event")
    if isinstance(event, dict):
        occ = event.get("occurrence")
        if isinstance(occ, dict):
            for key in LINK_KEYS:
                occ.pop(key, None)
            current = _current_occurrence(occ.get("id"))
            occ.update(occurrence_links(current, bool(destination.include_private_links)))
    return filter_for_destination(canonical, destination)


def build_payload(event_type, *, registration, occurrence, destination, delivery_id, consents=None):
    full = build_full_payload(
        event_type,
        registration=registration,
        occurrence=occurrence,
        destination=destination,
        delivery_id=delivery_id,
        consents=consents,
    )
    return filter_for_destination(full, destination)


def enqueue_event(event_type, *, registration=None, occurrence=None):
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("enqueue_event must be called inside transaction.atomic()")
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}")
    if occurrence is None and registration is not None:
        occurrence = registration.occurrence
    if occurrence is None:
        raise ValueError("enqueue_event requires a registration or an occurrence")

    person = registration.person if registration is not None else None
    if person is not None and person.erased_at is not None:
        return []

    destinations = [
        d for d in Destination.objects.filter(enabled=True).order_by("created_at") if event_type in (d.event_types or [])
    ]
    if not destinations:
        return []
    consents = registration_consents(registration) if person is not None else None
    messages = []
    for destination in destinations:
        if not consents_satisfied(destination, person, consents):
            continue
        delivery_id = uuid.uuid4()
        payload = build_payload(
            event_type,
            registration=registration,
            occurrence=occurrence,
            destination=destination,
            delivery_id=delivery_id,
            consents=consents,
        )
        messages.append(
            OutboxMessage.objects.create(
                id=delivery_id,
                destination=destination,
                event_type=event_type,
                payload=payload,
                person=person,
                status=OutboxMessage.Status.PENDING,
                next_attempt_at=timezone.now(),
            )
        )
    return messages
