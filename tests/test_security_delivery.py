"""Regression tests for review findings A1 (batch lease duplicates), A3 (send-time
consent/erasure/destination re-check) and A4 (PII surviving the purge)."""
from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from tests.conftest import make_destination, make_person, make_registration
from webinars.models import ConsentRecord, OutboxMessage, Person

pytestmark = pytest.mark.django_db
S = OutboxMessage.Status


class FakeTransport:
    def __init__(self, status=200, on_call=None):
        self.status = status
        self.calls = []
        self.on_call = on_call

    def __call__(self, url, body, headers, timeout):
        self.calls.append(headers["Webinar-Delivery-Id"])
        if self.on_call:
            self.on_call(headers["Webinar-Delivery-Id"])
        return self.status, ""


@pytest.fixture
def transport(monkeypatch):
    fake = FakeTransport()
    import webinars.delivery
    import webinars.ssrf

    monkeypatch.setattr(webinars.delivery, "post_json_pinned", fake, raising=False)
    monkeypatch.setattr(webinars.ssrf, "post_json_pinned", fake)
    return fake


def _enqueue(occurrence, person=None, new_destination=True, **dest_kw):
    from webinars.outbox import enqueue_event

    if new_destination:
        make_destination(**dest_kw)
    reg = make_registration(occurrence, person=person)
    with transaction.atomic():
        msgs = enqueue_event("registration.created", registration=reg, occurrence=occurrence)
    assert msgs
    return msgs[0]


def _past(**kw):
    return timezone.now() - timedelta(**kw)


# ---------------------------------------------------------------- A1


def test_lease_expired_mid_batch_is_not_sent_twice(transport, occurrence):
    """Worker A's batch lease expires while it is busy; worker B re-claims the tail.
    A must not send the tail too (it previously did, then lost only its _finish)."""
    from webinars.delivery import claim_batch, deliver

    first = _enqueue(occurrence)
    tail = _enqueue(occurrence, new_destination=False)

    batch_a = claim_batch("worker-a")
    assert {m.id for m in batch_a} == {first.id, tail.id}
    by_id = {m.id: m for m in batch_a}

    deliver(by_id[first.id])
    # Simulate the slow first send: the shared lease ran out and B re-claimed the tail.
    OutboxMessage.objects.filter(pk=tail.pk).update(lease_expires_at=_past(seconds=1))
    batch_b = claim_batch("worker-b")
    assert [m.id for m in batch_b] == [tail.id]

    deliver(by_id[tail.id])  # A reaches the tail: must skip, not send
    assert transport.calls.count(str(tail.id)) == 0
    deliver(batch_b[0])
    assert transport.calls.count(str(tail.id)) == 1
    tail.refresh_from_db()
    assert tail.status == S.DELIVERED


def test_same_worker_id_reclaim_is_distinguished(transport, occurrence):
    """A restarted worker reusing its id must not let the stale process send too."""
    from webinars.delivery import claim_batch, deliver

    msg = _enqueue(occurrence)
    stale = claim_batch("host-1")[0]
    OutboxMessage.objects.filter(pk=msg.pk).update(lease_expires_at=_past(seconds=1))
    fresh = claim_batch("host-1")[0]
    deliver(stale)
    assert transport.calls == []
    deliver(fresh)
    assert transport.calls == [str(msg.id)]


def test_lease_renewed_before_each_send(transport, occurrence):
    from webinars.delivery import claim_batch, deliver

    msg = _enqueue(occurrence)
    claimed = claim_batch("w")[0]
    near_expiry = timezone.now() + timedelta(seconds=1)
    OutboxMessage.objects.filter(pk=msg.pk).update(lease_expires_at=near_expiry)
    claimed.lease_expires_at = near_expiry
    seen = {}

    def check(_delivery_id):
        seen["lease"] = OutboxMessage.objects.get(pk=msg.pk).lease_expires_at

    transport.on_call = check
    deliver(claimed)
    assert seen["lease"] > near_expiry + timedelta(seconds=30)


# ---------------------------------------------------------------- A3


def test_consent_withdrawn_after_enqueue_is_not_sent(transport, occurrence):
    from webinars.delivery import claim_batch, deliver

    person = make_person(email="ads@example.com")
    ConsentRecord.objects.create(person=person, purpose="advertising", granted=True)
    msg = _enqueue(occurrence, person=person, required_consents=["advertising"])
    ConsentRecord.objects.create(person=person, purpose="advertising", granted=False)

    deliver(claim_batch("w")[0])
    assert transport.calls == []
    msg.refresh_from_db()
    assert msg.status == S.CANCELLED
    assert msg.last_error == "consent_not_granted"
    assert msg.payload == {"purged": True}


def test_destination_restricted_after_enqueue_is_rechecked(transport, occurrence):
    from webinars.delivery import claim_batch, deliver

    msg = _enqueue(occurrence)
    msg.destination.required_consents = ["sms"]
    msg.destination.save()
    deliver(claim_batch("w")[0])
    assert transport.calls == []
    msg.refresh_from_db()
    assert msg.status == S.CANCELLED


def test_destination_disabled_after_claim_is_not_sent(transport, occurrence):
    from webinars.delivery import claim_batch, deliver

    msg = _enqueue(occurrence)
    claimed = claim_batch("w")[0]
    msg.destination.enabled = False
    msg.destination.save()
    deliver(claimed)  # claimed copy still has enabled=True cached
    assert transport.calls == []
    msg.refresh_from_db()
    assert msg.status == S.PENDING
    assert msg.attempts == 0


def test_erased_after_claim_is_not_sent(transport, occurrence):
    from webinars.delivery import claim_batch, deliver
    from webinars.management.commands.purge_personal_data import erase_person

    msg = _enqueue(occurrence)
    claimed = claim_batch("w")[0]
    erase_person(msg.person_id)
    deliver(claimed)
    assert transport.calls == []
    msg.refresh_from_db()
    assert msg.status == S.CANCELLED
    assert msg.payload == {"purged": True}


def test_erased_person_message_requeued_is_not_sent(transport, occurrence):
    from webinars.delivery import claim_batch, deliver

    msg = _enqueue(occurrence)
    Person.objects.filter(pk=msg.person_id).update(erased_at=timezone.now())
    deliver(claim_batch("w")[0])
    assert transport.calls == []
    msg.refresh_from_db()
    assert msg.status == S.CANCELLED
    assert msg.last_error == "person_erased"


def test_deleted_person_message_is_not_sent(transport, occurrence):
    from webinars.delivery import claim_batch, deliver

    msg = _enqueue(occurrence)
    claimed = claim_batch("w")[0]
    Person.objects.filter(pk=msg.person_id).delete()  # PII outbox rows cascade.
    assert not OutboxMessage.objects.filter(pk=msg.pk).exists()
    deliver(claimed)
    assert transport.calls == []


# ---------------------------------------------------------------- A4


def test_housekeeping_purges_stale_pending_for_disabled_destination(occurrence):
    from webinars.management.commands.purge_personal_data import housekeeping

    msg = _enqueue(occurrence)
    msg.destination.enabled = False
    msg.destination.save()
    OutboxMessage.objects.filter(pk=msg.pk).update(created_at=_past(days=400))
    fresh = _enqueue(occurrence, name="fresh")

    result = housekeeping()
    assert result["outbox_stale_pending_cancelled"] == 1
    msg.refresh_from_db()
    assert msg.status == S.CANCELLED
    assert msg.payload == {"purged": True}
    fresh.refresh_from_db()
    assert fresh.status == S.PENDING
    assert fresh.payload != {"purged": True}


def test_housekeeping_purges_orphaned_person_payloads(occurrence):
    from webinars.management.commands.purge_personal_data import housekeeping

    pending = _enqueue(occurrence)
    delivered = _enqueue(occurrence, new_destination=False)
    OutboxMessage.objects.filter(pk=delivered.pk).update(status=S.DELIVERED, delivered_at=timezone.now())
    # Simulate orphaned rows from pre-CASCADE versions.
    OutboxMessage.objects.filter(pk__in=[pending.pk, delivered.pk]).update(person=None)

    housekeeping()
    pending.refresh_from_db()
    delivered.refresh_from_db()
    assert pending.status == S.CANCELLED
    assert pending.payload == {"purged": True}
    assert delivered.status == S.DELIVERED
    assert delivered.payload == {"purged": True}


def test_housekeeping_keeps_occurrence_level_messages(occurrence):
    from webinars.management.commands.purge_personal_data import housekeeping
    from webinars.outbox import enqueue_event

    make_destination(event_types=("occurrence.cancelled",))
    with transaction.atomic():
        msg = enqueue_event("occurrence.cancelled", occurrence=occurrence)[0]
    housekeeping()
    msg.refresh_from_db()
    assert msg.status == S.PENDING
    assert msg.payload != {"purged": True}
