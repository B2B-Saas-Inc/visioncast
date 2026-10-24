"""PostgreSQL-only concurrency tests (row locks, SKIP LOCKED, unique constraints).

SQLite serialises writers and ignores SELECT ... FOR UPDATE, so results there are
not evidence; these tests are skipped unless the backend is PostgreSQL.
"""
import json
import threading
from datetime import timedelta

import pytest
from django.db import connection, connections, transaction
from django.test import Client
from django.utils import timezone

from tests.conftest import (
    make_api_key,
    make_destination,
    make_event,
    make_form,
    make_occurrence,
    make_registration,
    payload,
)
from webinars.models import IdempotencyRecord, OutboxMessage, Person, Registration

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL"),
]

N = 6


def _run_parallel(fn, n=N):
    barrier = threading.Barrier(n)
    results, errors = [None] * n, []

    def worker(i):
        try:
            barrier.wait(timeout=10)
            results[i] = fn(i)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    return results


def _setup():
    occ = make_occurrence(make_event(make_form(), slug="concurrent"))
    key = make_api_key(("proxy",))
    return occ, key


def _post(key, body):
    c = Client()
    resp = c.post(
        "/api/v1/registrations", data=json.dumps(body), content_type="application/json", headers={"Authorization": f"Bearer {key}"}
    )
    return resp.status_code, resp.json()


def test_same_email_same_occurrence_one_registration():
    occ, key = _setup()
    results = _run_parallel(lambda i: _post(key, payload(occ)))  # distinct idempotency keys
    statuses = [s for s, _ in results]
    assert all(s in (200, 201) for s in statuses), results
    bodies = [b for _, b in results]
    assert all(b == bodies[0] for b in bodies)
    assert Registration.objects.filter(occurrence=occ).count() == 1
    assert Person.objects.count() == 1


def test_same_idempotency_key_concurrently_one_registration():
    occ, key = _setup()
    body = payload(occ)
    results = _run_parallel(lambda i: _post(key, body))
    statuses = [s for s, _ in results]
    assert statuses.count(201) >= 1
    assert all(s in (200, 201) for s in statuses), results
    assert all(b == results[0][1] for _, b in results)
    assert Registration.objects.count() == 1
    assert IdempotencyRecord.objects.filter(key=body["idempotencyKey"]).count() == 1


def test_claim_batch_never_overlaps():
    from webinars.delivery import claim_batch
    from webinars.outbox import enqueue_event

    occ, _key = _setup()
    make_destination()
    for _ in range(30):
        reg = make_registration(occ)
        with transaction.atomic():
            enqueue_event("registration.created", registration=reg, occurrence=occ)
    assert OutboxMessage.objects.count() == 30

    results = _run_parallel(lambda i: [m.id for m in claim_batch(f"w{i}", limit=10)])
    claimed = [mid for batch in results for mid in batch]
    assert len(claimed) == len(set(claimed)), "a message was claimed by two workers"
    assert len(claimed) == 30
    owners = OutboxMessage.objects.values_list("lease_owner", flat=True)
    assert all(owners)


def test_expired_leases_reclaimed_once():
    from webinars.delivery import claim_batch
    from webinars.outbox import enqueue_event

    occ, _key = _setup()
    make_destination()
    for _ in range(10):
        reg = make_registration(occ)
        with transaction.atomic():
            enqueue_event("registration.created", registration=reg, occurrence=occ)
    claim_batch("crashed", limit=50)
    OutboxMessage.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=5))
    results = _run_parallel(lambda i: [m.id for m in claim_batch(f"r{i}", limit=5)], n=4)
    claimed = [mid for batch in results for mid in batch]
    assert len(claimed) == len(set(claimed)) == 10
