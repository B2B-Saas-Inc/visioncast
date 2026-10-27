"""Occurrence capacity: the single seat check used by every registration path.

Contract (docs/tickets.md):

* ``Occurrence.capacity`` null means unlimited; nothing is counted and behaviour is
  unchanged.
* Seats taken = active registrations (``status=registered``) + ticket orders in
  ``TicketOrder.HOLDING`` (hold, checkout open, needs reconciliation). An order being
  fulfilled is excluded so its own hold does not block it.
* Counting is only meaningful under the occurrence row lock (``lock_occurrence``) in
  the same transaction as the insert it guards. ``registration._process`` (free API,
  direct proxy, trusted API, waiting room) and ``payments`` (hold creation and paid
  fulfillment) both take that lock before counting. Lock order everywhere:
  occurrence, then order, then person.
* A full occurrence answers 409 ``sold_out`` with a generic message: it never says who
  is registered. Re-submitting as an existing registrant still succeeds (no new seat).
"""
from django.core.exceptions import ValidationError
from django.db import transaction

from webinars.audit import audit
from webinars.models import Occurrence, Registration, TicketOrder

MAX_CAPACITY = 1_000_000
SOLD_OUT_MESSAGE = "This session is sold out."


class SoldOut(Exception):
    """No seat is available (raised by ``ensure_seat``)."""


def lock_occurrence(occurrence_id):
    """``SELECT ... FOR UPDATE`` the occurrence row (event and form loaded, not locked)."""
    return (
        Occurrence.objects.select_for_update(of=("self",))
        .select_related("event", "event__form_version")
        .get(pk=occurrence_id)
    )


def seats_taken(occ, *, exclude_order_id=None):
    registered = Registration.objects.filter(occurrence_id=occ.pk, status=Registration.Status.REGISTERED).count()
    holds = TicketOrder.objects.filter(occurrence_id=occ.pk, status__in=TicketOrder.HOLDING)
    if exclude_order_id is not None:
        holds = holds.exclude(pk=exclude_order_id)
    return registered + holds.count()


def has_seat(occ, *, exclude_order_id=None):
    if occ.capacity is None:
        return True
    return seats_taken(occ, exclude_order_id=exclude_order_id) < occ.capacity


def ensure_seat(occ, *, exclude_order_id=None):
    """Raise ``SoldOut`` when full. The caller must hold ``lock_occurrence(occ.pk)``."""
    if not has_seat(occ, exclude_order_id=exclude_order_id):
        raise SoldOut()


def remaining(occ):
    """Seats left (never negative), or None when unlimited."""
    if occ.capacity is None:
        return None
    return max(0, occ.capacity - seats_taken(occ))


def public_availability(occ):
    """Public projection: ``remaining`` only when the organiser opted in; ``soldOut`` always.

    ``soldOut`` for an unlimited session is always False, so the flag alone never
    reveals a count.
    """
    left = remaining(occ)
    return {
        "remaining": left if (occ.show_remaining and left is not None) else None,
        "soldOut": left == 0,
    }


def clean_capacity(value):
    """Validate a capacity input: None/'' (unlimited) or an integer 1..MAX_CAPACITY."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValidationError({"capacity": "Enter a whole number of seats, or leave blank for unlimited."})
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError({"capacity": "Enter a whole number of seats, or leave blank for unlimited."})
    if not 1 <= number <= MAX_CAPACITY:
        raise ValidationError({"capacity": f"Choose between 1 and {MAX_CAPACITY:,} seats."})
    return number


UNSET = object()


def update_ticket_settings(occ, *, capacity=UNSET, show_remaining=UNSET, price_minor=UNSET, currency=UNSET,
                           user=None, api_key=None):
    """Change capacity / show-remaining / price override on one occurrence (validated).

    ``price_minor=None`` clears the override (the event price, if any, applies). Price
    changes never affect existing orders: each order snapshots its price. Lowering the
    capacity below the seats already taken is allowed; it only blocks new seats.
    Returns the sorted list of changed model fields.
    """
    from webinars import payments

    errors = {}
    values = {}
    if capacity is not UNSET:
        try:
            values["capacity"] = clean_capacity(capacity)
        except ValidationError as exc:
            errors.update(exc.message_dict)
    if show_remaining is not UNSET:
        values["show_remaining"] = bool(show_remaining)
    if price_minor is not UNSET or currency is not UNSET:
        p = occ.ticket_price_minor if price_minor is UNSET else price_minor
        c = occ.ticket_currency if currency is UNSET else currency
        try:
            p, c = payments.clean_price(p, c)
        except ValidationError as exc:
            errors.update(exc.message_dict)
        else:
            values["ticket_price_minor"] = p
            values["ticket_currency"] = c
    if errors:
        raise ValidationError(errors)
    with transaction.atomic():
        locked = Occurrence.objects.select_for_update().get(pk=occ.pk)
        changed = sorted(k for k, v in values.items() if getattr(locked, k) != v)
        if changed:
            for k in changed:
                setattr(locked, k, values[k])
            locked.save(update_fields=changed + ["updated_at"])
            audit("occurrence.tickets_updated", user=user, api_key=api_key, obj=locked, fields=changed)
    for k in changed:
        setattr(occ, k, values[k])
    return changed
