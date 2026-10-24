import json
from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from tests.conftest import (
    JOIN_URL,
    WEBHOOK_SECRET,
    make_destination,
    make_registration,
    payload,
    post_registration,
)
from webinars.crypto import verify_webhook_signature
from webinars.models import OutboxMessage

pytestmark = pytest.mark.django_db
S = OutboxMessage.Status


class FakeTransport:
    """Replaces the SSRF-pinned POST; records each call."""

    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": dict(headers), "timeout": timeout})
        return self.status, ""


@pytest.fixture
def transport(monkeypatch):
    fake = FakeTransport()
    import webinars.delivery
    import webinars.ssrf

    # Patch where delivery looks the name up, and the source module as a fallback.
    monkeypatch.setattr(webinars.delivery, "post_json_pinned", fake, raising=False)
    monkeypatch.setattr(webinars.ssrf, "post_json_pinned", fake)
    return fake


def _register(client, occurrence, key, **overrides):
    resp = post_registration(client, payload(occurrence, **overrides), key)
    assert resp.status_code == 201, resp.content
    return resp


def _msg(occurrence, **dest_kw):
    from webinars.outbox import enqueue_event

    dest = make_destination(**dest_kw)
    reg = make_registration(occurrence)
    with transaction.atomic():
        msgs = enqueue_event("registration.created", registration=reg, occurrence=occurrence)
    assert len(msgs) == 1
    return msgs[0], dest


@pytest.mark.django_db(transaction=True)
def test_enqueue_outside_atomic_raises():
    from tests.conftest import make_event, make_form, make_occurrence
    from webinars.outbox import enqueue_event

    occ = make_occurrence(make_event(make_form()))
    make_destination()
    reg = make_registration(occ)
    with pytest.raises(RuntimeError):
        enqueue_event("registration.created", registration=reg, occurrence=occ)
    assert OutboxMessage.objects.count() == 0


def test_enqueue_inside_atomic_creates_rows(occurrence):
    msg, dest = _msg(occurrence)
    assert msg.status == S.PENDING
    assert msg.destination_id == dest.id
    assert msg.event_type == "registration.created"


def test_registration_creates_outbox_row(client, occurrence, proxy_key):
    dest = make_destination()
    make_destination(name="disabled", enabled=False)
    make_destination(name="other-type", event_types=["occurrence.cancelled"])
    _register(client, occurrence, proxy_key)
    rows = OutboxMessage.objects.all()
    assert rows.count() == 1
    row = rows.get()
    assert row.destination_id == dest.id
    assert row.event_type == "registration.created"
    p = row.payload
    for group in ("identity", "profile", "organization", "consents", "attribution", "registration", "event", "delivery_metadata"):
        assert group in p, group
    assert p["event"]["slug"] == occurrence.event.slug


def test_failed_registration_creates_no_outbox_row(client, occurrence, proxy_key):
    make_destination()
    resp = post_registration(client, payload(occurrence, profile={"email": "bad"}), proxy_key)
    assert resp.status_code == 400
    assert OutboxMessage.objects.count() == 0


def test_required_consent_gates_destination(client, event, occurrence, proxy_key):
    from tests.conftest import make_occurrence

    make_destination(name="ads", required_consents=["advertising"])
    _register(client, occurrence, proxy_key)
    assert OutboxMessage.objects.count() == 0
    occ2 = make_occurrence(event, days=21)
    _register(client, occ2, proxy_key, profile={"email": "consenting@example.org"}, consents={"advertising": {"granted": True}})
    assert OutboxMessage.objects.count() == 1


def test_allowed_groups_filter_removes_profile(client, occurrence, proxy_key):
    make_destination(allowed_groups=["registration", "event"])
    _register(client, occurrence, proxy_key)
    p = OutboxMessage.objects.get().payload
    assert "profile" not in p and "identity" not in p and "attribution" not in p
    assert "registration" in p and "event" in p
    dumped = json.dumps(p).lower()
    assert "ada.lovelace" not in dumped
    assert '"ada"' not in dumped


def test_private_links_excluded_by_default(client, occurrence, proxy_key):
    make_destination(include_private_links=False)
    _register(client, occurrence, proxy_key)
    dumped = json.dumps(OutboxMessage.objects.get().payload)
    assert JOIN_URL not in dumped
    assert "joinUrl" not in dumped


def test_private_links_included_when_enabled(client, occurrence, proxy_key):
    make_destination(include_private_links=True)
    _register(client, occurrence, proxy_key)
    assert JOIN_URL in json.dumps(OutboxMessage.objects.get().payload)


def test_unverified_submission_payload_flags_identity(client, event, occurrence, proxy_key):
    from tests.conftest import make_occurrence

    _register(client, occurrence, proxy_key)
    make_destination()
    occ2 = make_occurrence(event, days=19)
    _register(client, occ2, proxy_key, profile={"firstName": "Mallory"})
    p = OutboxMessage.objects.get().payload
    assert p["identity"]["verified"] is False
    assert p["registration"]["identityVerified"] is False


def test_deliver_success_signed(transport, occurrence):
    from webinars.delivery import claim_batch, deliver

    msg, _dest = _msg(occurrence)
    claimed = claim_batch("w1")
    assert [m.id for m in claimed] == [msg.id]
    deliver(claimed[0])
    msg.refresh_from_db()
    assert msg.status == S.DELIVERED
    assert msg.delivered_at is not None
    assert msg.lease_owner == ""
    call = transport.calls[0]
    h = call["headers"]
    assert h["Webinar-Delivery-Id"] == str(msg.id)
    assert h["Webinar-Signature"].startswith("v1=")
    assert verify_webhook_signature(WEBHOOK_SECRET, h["Webinar-Delivery-Id"], h["Webinar-Timestamp"], call["body"], h["Webinar-Signature"])
    assert not verify_webhook_signature("wrong-secret", h["Webinar-Delivery-Id"], h["Webinar-Timestamp"], call["body"], h["Webinar-Signature"])
    assert call["url"] == "https://hooks.example.net/webhook"
    body = json.loads(call["body"])
    assert body["delivery_metadata"]["deliveryId"] == str(msg.id)


def test_deliver_failure_schedules_retry(transport, occurrence):
    from webinars.delivery import claim_batch, deliver

    transport.status = 500
    msg, _ = _msg(occurrence)
    deliver(claim_batch("w1")[0])
    msg.refresh_from_db()
    assert msg.status == S.PENDING
    assert msg.attempts == 1
    assert msg.last_status_code == 500
    assert msg.next_attempt_at > timezone.now()
    assert claim_batch("w1") == []  # not due yet


def test_redirect_is_failure(transport, occurrence):
    from webinars.delivery import claim_batch, deliver

    transport.status = 302
    msg, _ = _msg(occurrence)
    deliver(claim_batch("w1")[0])
    msg.refresh_from_db()
    assert msg.status != S.DELIVERED


def test_max_attempts_dead_letter_and_requeue(transport, occurrence):
    from webinars.delivery import claim_batch, deliver, requeue_message

    transport.status = 503
    msg, _ = _msg(occurrence, max_attempts=2)
    deliver(claim_batch("w1")[0])
    OutboxMessage.objects.filter(pk=msg.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    deliver(claim_batch("w1")[0])
    msg.refresh_from_db()
    assert msg.status == S.DEAD
    assert msg.attempts == 2
    assert claim_batch("w1") == []
    assert requeue_message(msg)
    msg.refresh_from_db()
    assert msg.status == S.PENDING
    transport.status = 200
    deliver(claim_batch("w1")[0])
    msg.refresh_from_db()
    assert msg.status == S.DELIVERED


def test_delivery_id_stable_across_retries(transport, occurrence):
    from webinars.delivery import claim_batch, deliver

    transport.status = 500
    msg, _ = _msg(occurrence)
    deliver(claim_batch("w1")[0])
    OutboxMessage.objects.filter(pk=msg.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    transport.status = 200
    deliver(claim_batch("w2")[0])
    ids = {c["headers"]["Webinar-Delivery-Id"] for c in transport.calls}
    assert ids == {str(msg.id)}
    assert len(transport.calls) == 2


def test_claim_does_not_double_claim(occurrence):
    from webinars.delivery import claim_batch

    msg, _ = _msg(occurrence)
    first = claim_batch("a")
    assert [m.id for m in first] == [msg.id]
    assert claim_batch("b") == []
    msg.refresh_from_db()
    assert msg.status == S.IN_FLIGHT
    assert msg.lease_owner == "a"
    assert msg.lease_expires_at > timezone.now()


def test_expired_lease_reclaimable_and_old_worker_cannot_overwrite(transport, occurrence):
    from webinars.delivery import claim_batch, deliver

    msg, _ = _msg(occurrence)
    stale = claim_batch("crashed")[0]
    OutboxMessage.objects.filter(pk=msg.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    reclaimed = claim_batch("restarted")
    assert [m.id for m in reclaimed] == [msg.id]
    msg.refresh_from_db()
    assert msg.lease_owner == "restarted"
    # The crashed worker wakes up: it must not record an outcome over the new lease.
    transport.status = 500
    deliver(stale)
    msg.refresh_from_db()
    assert msg.status == S.IN_FLIGHT
    assert msg.lease_owner == "restarted"
    transport.status = 200
    deliver(reclaimed[0])
    msg.refresh_from_db()
    assert msg.status == S.DELIVERED


def test_run_once_delivers(transport, occurrence):
    from webinars.delivery import run_once

    msg, _ = _msg(occurrence)
    assert run_once("w1") == 1
    msg.refresh_from_db()
    assert msg.status == S.DELIVERED


def test_last_error_never_contains_response_body(monkeypatch, occurrence):
    import webinars.delivery

    def leaky(url, body, headers, timeout):
        return 400, "error for ada.lovelace@example.com"

    monkeypatch.setattr(webinars.delivery, "post_json_pinned", leaky, raising=False)
    monkeypatch.setattr("webinars.ssrf.post_json_pinned", leaky)
    from webinars.delivery import claim_batch, deliver

    msg, _ = _msg(occurrence)
    deliver(claim_batch("w1")[0])
    msg.refresh_from_db()
    assert "ada" not in msg.last_error.lower()


def test_unsafe_destination_dead_letters(monkeypatch, occurrence):
    import webinars.delivery
    from webinars.ssrf import UnsafeDestination

    def unsafe(url, body, headers, timeout):
        raise UnsafeDestination("private address")

    monkeypatch.setattr(webinars.delivery, "post_json_pinned", unsafe, raising=False)
    monkeypatch.setattr("webinars.ssrf.post_json_pinned", unsafe)
    from webinars.delivery import claim_batch, deliver

    msg, _ = _msg(occurrence)
    deliver(claim_batch("w1")[0])
    msg.refresh_from_db()
    assert msg.status == S.DEAD
