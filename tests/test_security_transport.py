"""Webhook transport hardening: hard total deadline (A2), send-time gate for messages
queued for unverified registrations, and OutboxMessage.person CASCADE (A4).

No test performs DNS or network I/O: child targets below are stand-ins that never touch
sockets, and delivery tests replace the bounded transport.

Keep module-level imports light: spawned children import this module to find targets.
"""
import multiprocessing
import os
import time

import pytest

from webinars import ssrf

URL = "https://hooks.example.net/webhook"


# Child targets (same signature as ssrf._bounded_child). Must stay module-level.
def _hang_child(conn, *args):
    time.sleep(120)


def _ok_child(conn, host, port, path, body, headers, timeout):
    conn.send(("ok", 204, f"{host}|{port}|{path}|{timeout}"))
    conn.close()


def _tls_child(conn, *args):
    conn.send(("tls", "SSLCertVerificationError", ""))
    conn.close()


def _unsafe_child(conn, *args):
    conn.send(("unsafe", "Host resolves to a non-public address", ""))
    conn.close()


def _crash_child(conn, *args):
    os._exit(3)


@pytest.fixture
def allow(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["example.net"]
    settings.WEBHOOK_ALLOWED_PORTS = [443]


# ---------------------------------------------------------------- total deadline


def test_hanging_send_is_killed_at_total_deadline(allow):
    start = time.monotonic()
    with pytest.raises(ssrf.DeadlineExceeded):
        ssrf.post_json_bounded(URL, b"{}", {}, 10, 0.5, _target=_hang_child)
    assert time.monotonic() - start < 0.5 + 2 * ssrf.TERMINATE_GRACE_SECONDS + 10  # generous spawn allowance
    assert not [p for p in multiprocessing.active_children() if p.is_alive()]


def test_deadline_is_a_timeout_error():
    assert issubclass(ssrf.DeadlineExceeded, TimeoutError)


def test_child_result_returned_and_per_op_timeout_capped(allow):
    status, snippet = ssrf.post_json_bounded(URL + "?a=1", b"{}", {}, 10, 3, _target=_ok_child)
    assert status == 204
    assert snippet == "hooks.example.net|443|/webhook?a=1|3.0"


@pytest.mark.parametrize(
    "target,exc",
    [(_tls_child, ssrf.ssl.SSLError), (_unsafe_child, ssrf.UnsafeDestination), (_crash_child, OSError)],
)
def test_child_errors_are_mapped(allow, target, exc):
    with pytest.raises(exc):
        ssrf.post_json_bounded(URL, b"{}", {}, 10, 20, _target=target)


def test_policy_checked_in_parent_before_spawn(settings, monkeypatch):
    settings.WEBHOOK_HOST_ALLOWLIST = ["example.net"]
    spawned = []
    monkeypatch.setattr(multiprocessing, "get_context", lambda *a: spawned.append(a) or None)
    for bad in ("http://hooks.example.net/x", "https://evil.example.org/x", "https://127.0.0.1/x"):
        with pytest.raises(ssrf.UnsafeDestination):
            ssrf.post_json_bounded(bad, b"{}", {}, 10, 5, _target=_ok_child)
    assert spawned == []


# ---------------------------------------------------------------- delivery integration


class FakeBounded:
    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    def __call__(self, url, body, headers, timeout, total_timeout):
        self.calls.append(total_timeout)
        if self.raises:
            raise self.raises
        return 200, ""


def _enqueue(occurrence, **dest_kw):
    from django.db import transaction

    from tests.conftest import make_destination, make_registration
    from webinars.outbox import enqueue_event

    make_destination(**dest_kw)
    reg = make_registration(occurrence)
    with transaction.atomic():
        return enqueue_event("registration.created", registration=reg, occurrence=occurrence)[0]


@pytest.mark.django_db
def test_delivery_uses_bounded_send_within_lease(monkeypatch, settings, occurrence):
    import webinars.delivery as delivery

    settings.OUTBOX_LEASE_SECONDS = 120
    settings.WEBHOOK_TOTAL_TIMEOUT_SECONDS = 30
    fake = FakeBounded()
    monkeypatch.setattr(delivery, "post_json_bounded", fake)
    msg = _enqueue(occurrence)
    delivery.deliver(delivery.claim_batch("w")[0])
    assert len(fake.calls) == 1 and 1.0 <= fake.calls[0] <= 30
    msg.refresh_from_db()
    assert msg.status == msg.Status.DELIVERED


@pytest.mark.django_db
def test_total_timeout_never_exceeds_lease(monkeypatch, settings, occurrence):
    import webinars.delivery as delivery

    settings.OUTBOX_LEASE_SECONDS = 20
    settings.WEBHOOK_TOTAL_TIMEOUT_SECONDS = 300
    fake = FakeBounded()
    monkeypatch.setattr(delivery, "post_json_bounded", fake)
    _enqueue(occurrence)
    delivery.deliver(delivery.claim_batch("w")[0])
    assert fake.calls and fake.calls[0] <= 20 - delivery.LEASE_SAFETY_SECONDS


@pytest.mark.django_db
def test_deadline_exceeded_recorded_as_timeout_and_retried(monkeypatch, occurrence):
    import webinars.delivery as delivery

    monkeypatch.setattr(delivery, "post_json_bounded", FakeBounded(raises=ssrf.DeadlineExceeded("x")))
    msg = _enqueue(occurrence)
    delivery.deliver(delivery.claim_batch("w")[0])
    msg.refresh_from_db()
    assert msg.status == msg.Status.PENDING
    assert msg.last_error == "timeout"
    assert msg.lease_owner == ""


# ---------------------------------------------------------------- send-time unverified gate


@pytest.mark.django_db
def test_old_unverified_message_blocked_at_send_time(monkeypatch, occurrence):
    """A message enqueued before the enqueue-time rule (unverified registration paired
    with the person's real consent) is cancelled at send time, not sent."""
    from django.db import transaction

    import webinars.delivery as delivery
    from tests.conftest import make_destination, make_registration
    from webinars.models import ConsentRecord, Registration
    from webinars.outbox import enqueue_event

    fake = FakeBounded()
    monkeypatch.setattr(delivery, "post_json_bounded", fake)
    dest = make_destination(required_consents=["advertising"])
    reg = make_registration(occurrence)
    ConsentRecord.objects.create(person=reg.person, purpose="advertising", granted=True)
    with transaction.atomic():
        msg = enqueue_event("registration.created", registration=reg, occurrence=occurrence)[0]
    assert msg.destination_id == dest.id
    # Simulate the legacy state: the registration is unverified but the message was queued.
    Registration.objects.filter(pk=reg.pk).update(identity_verified=False)
    delivery.deliver(delivery.claim_batch("w")[0])
    assert fake.calls == []
    msg.refresh_from_db()
    assert msg.status == msg.Status.CANCELLED
    assert msg.last_error == "consent_not_granted"
    assert msg.payload == {"purged": True}


@pytest.mark.django_db
def test_payload_marked_unverified_blocked_even_without_registration_lookup(monkeypatch, occurrence):
    import webinars.delivery as delivery
    from webinars.models import ConsentRecord, OutboxMessage

    fake = FakeBounded()
    monkeypatch.setattr(delivery, "post_json_bounded", fake)
    msg = _enqueue(occurrence)
    ConsentRecord.objects.create(person_id=msg.person_id, purpose="advertising", granted=True)
    msg.destination.required_consents = ["advertising"]
    msg.destination.save()
    legacy = dict(msg.payload)
    legacy["identity"] = dict(legacy.get("identity") or {}, verified=False)
    OutboxMessage.objects.filter(pk=msg.pk).update(payload=legacy)
    delivery.deliver(delivery.claim_batch("w")[0])
    assert fake.calls == []
    msg.refresh_from_db()
    assert msg.status == msg.Status.CANCELLED


@pytest.mark.django_db
def test_verified_message_with_consent_still_sent(monkeypatch, occurrence):
    import webinars.delivery as delivery
    from webinars.models import ConsentRecord

    fake = FakeBounded()
    monkeypatch.setattr(delivery, "post_json_bounded", fake)
    msg = _enqueue(occurrence)
    ConsentRecord.objects.create(person_id=msg.person_id, purpose="advertising", granted=True)
    msg.destination.required_consents = ["advertising"]
    msg.destination.save()
    delivery.deliver(delivery.claim_batch("w")[0])
    assert len(fake.calls) == 1


# ---------------------------------------------------------------- A4 CASCADE


@pytest.mark.django_db
def test_deleting_person_deletes_their_outbox_messages(occurrence):
    from webinars.models import OutboxMessage, Person

    msg = _enqueue(occurrence)
    Person.objects.filter(pk=msg.person_id).delete()
    assert not OutboxMessage.objects.filter(pk=msg.pk).exists()
