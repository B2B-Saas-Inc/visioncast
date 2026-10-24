"""Regression tests for rebuild review M5 and the managed-room outbox note.

* A managed waiting room replaces the provider links in every outbox payload.
* Queued messages are re-filtered at send time against the current destination
  (include_private_links, allowed_groups, field_mapping) and room state.
* field_mapping is applied exactly once; legacy rows with mapped private links are stripped.
"""
import json

import pytest
from django.db import transaction

from tests.conftest import JOIN_URL, WAITING_URL, WEBHOOK_SECRET, make_destination, make_registration
from webinars import waiting_room
from webinars.crypto import verify_webhook_signature
from webinars.models import Destination, OutboxMessage

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

    monkeypatch.setattr(webinars.delivery, "post_json_pinned", fake, raising=False)
    monkeypatch.setattr(webinars.ssrf, "post_json_pinned", fake)
    return fake


def _enqueue(occurrence, event_type="registration.created", **dest_kw):
    from webinars.outbox import enqueue_event

    dest = make_destination(event_types=(event_type,), **dest_kw)
    reg = make_registration(occurrence)
    with transaction.atomic():
        msgs = enqueue_event(event_type, registration=reg, occurrence=occurrence)
    assert len(msgs) == 1
    return msgs[0], dest


def _enable_room(occurrence):
    waiting_room.configure(occurrence, enabled=True)
    occurrence.refresh_from_db()
    url = waiting_room.room_url(occurrence)
    assert url and url.startswith("https://webinars.example.com/w/")
    return url


def _deliver_one(transport):
    from webinars.delivery import claim_batch, deliver

    claimed = claim_batch("w1")
    assert len(claimed) == 1
    deliver(claimed[0])
    assert transport.calls, "message was not sent"
    call = transport.calls[-1]
    return json.loads(call["body"]), call


def _update_destination(dest, **fields):
    Destination.objects.filter(pk=dest.pk).update(**fields)


def _assert_no_provider_links(blob):
    text = blob if isinstance(blob, str) else json.dumps(blob)
    assert JOIN_URL not in text
    assert WAITING_URL not in text
    assert "secret-join-token" not in text
    assert '"joinUrl"' not in text
    assert '"waitingRoomUrl"' not in text


# ---------------------------------------------------------------- managed room at enqueue


@pytest.mark.parametrize("event_type", ["registration.created", "attendance.checked_in"])
def test_managed_room_replaces_provider_links_even_with_private_links(transport, occurrence, event_type):
    room = _enable_room(occurrence)
    msg, _dest = _enqueue(occurrence, event_type=event_type, include_private_links=True)

    _assert_no_provider_links(msg.payload)
    assert msg.payload["event"]["occurrence"]["managedRoomUrl"] == room

    body, _call = _deliver_one(transport)
    _assert_no_provider_links(body)
    assert body["event"]["occurrence"]["managedRoomUrl"] == room
    assert body["eventType"] == event_type


def test_managed_room_url_sent_without_private_links(occurrence):
    room = _enable_room(occurrence)
    msg, _dest = _enqueue(occurrence, include_private_links=False)
    _assert_no_provider_links(msg.payload)
    assert msg.payload["event"]["occurrence"]["managedRoomUrl"] == room


def test_room_disabled_keeps_documented_private_link_behaviour(transport, occurrence):
    msg, _dest = _enqueue(occurrence, include_private_links=True)
    assert msg.payload["event"]["occurrence"]["joinUrl"] == JOIN_URL
    assert "managedRoomUrl" not in msg.payload["event"]["occurrence"]
    body, _call = _deliver_one(transport)
    assert body["event"]["occurrence"]["joinUrl"] == JOIN_URL
    assert body["event"]["occurrence"]["waitingRoomUrl"] == WAITING_URL


# ---------------------------------------------------------------- send-time re-filtering


def test_private_links_disabled_after_queueing_are_not_sent(transport, occurrence):
    msg, dest = _enqueue(occurrence, include_private_links=True)
    assert JOIN_URL in json.dumps(msg.payload)  # stored snapshot still has it
    _update_destination(dest, include_private_links=False)

    body, call = _deliver_one(transport)
    _assert_no_provider_links(call["body"].decode("utf-8"))
    assert "managedRoomUrl" not in body["event"]["occurrence"]
    msg.refresh_from_db()
    assert msg.status == S.DELIVERED


def test_room_enabled_after_queueing_strips_provider_links(transport, occurrence):
    msg, _dest = _enqueue(occurrence, include_private_links=True)
    assert JOIN_URL in json.dumps(msg.payload)
    room = _enable_room(occurrence)

    body, call = _deliver_one(transport)
    _assert_no_provider_links(call["body"].decode("utf-8"))
    assert body["event"]["occurrence"]["managedRoomUrl"] == room


def test_room_disabled_after_queueing_drops_stale_room_url(transport, occurrence):
    _enable_room(occurrence)
    _msg, _dest = _enqueue(occurrence, include_private_links=False)
    waiting_room.configure(occurrence, enabled=False)

    body, call = _deliver_one(transport)
    assert "managedRoomUrl" not in body["event"]["occurrence"]
    _assert_no_provider_links(call["body"].decode("utf-8"))


def test_allowed_groups_narrowed_after_queueing_applies(transport, occurrence):
    msg, dest = _enqueue(occurrence, field_mapping={"profile.firstName": "first_name"})
    assert "profile" in msg.payload and "identity" in msg.payload
    assert msg.payload["mapped"] == {"first_name": "Pat"}
    _update_destination(dest, allowed_groups=["registration", "event"])

    body, call = _deliver_one(transport)
    for group in ("identity", "profile", "organization", "consents", "attribution"):
        assert group not in body, group
    assert "registration" in body and "event" in body and "delivery_metadata" in body
    # The mapping source is no longer a permitted group, so it is not mapped either.
    assert body["mapped"] == {}
    assert "Pat" not in call["body"].decode("utf-8")


def test_allowed_groups_never_widen_beyond_snapshot(transport, occurrence):
    msg, dest = _enqueue(occurrence, allowed_groups=["registration", "event"])
    assert "profile" not in msg.payload
    _update_destination(dest, allowed_groups=list(Destination.GROUPS))

    body, _call = _deliver_one(transport)
    assert "profile" not in body and "identity" not in body


# ---------------------------------------------------------------- field_mapping exactly once


def test_field_mapping_is_applied_once_and_never_chains(transport, occurrence):
    mapping = {"profile.firstName": "given", "given": "chained", "mapped.given": "chained2"}
    msg, _dest = _enqueue(occurrence, field_mapping=mapping)
    assert msg.payload["mapped"] == {"given": "Pat"}

    body, _call = _deliver_one(transport)
    assert body["mapped"] == {"given": "Pat"}
    assert "mapped" not in body["mapped"]
    assert body["profile"]["firstName"] == "Pat"  # canonical groups are kept, not renamed


def test_field_mapping_changed_after_queueing_uses_current_mapping(transport, occurrence):
    msg, dest = _enqueue(occurrence, field_mapping={"profile.firstName": "old_name"})
    assert msg.payload["mapped"] == {"old_name": "Pat"}
    _update_destination(dest, field_mapping={"profile.firstName": "new_name"})

    body, _call = _deliver_one(transport)
    assert body["mapped"] == {"new_name": "Pat"}


def test_field_mapping_removed_after_queueing_drops_mapped(transport, occurrence):
    _msg, dest = _enqueue(occurrence, field_mapping={"profile.firstName": "given"})
    _update_destination(dest, field_mapping={})

    body, _call = _deliver_one(transport)
    assert "mapped" not in body


def test_mapped_private_link_stripped_when_flag_disabled(transport, occurrence):
    msg, dest = _enqueue(
        occurrence, include_private_links=True, field_mapping={"event.occurrence.joinUrl": "join_link"}
    )
    assert msg.payload["mapped"] == {"join_link": JOIN_URL}
    _update_destination(dest, include_private_links=False)

    body, call = _deliver_one(transport)
    _assert_no_provider_links(call["body"].decode("utf-8"))
    assert body["mapped"] == {"join_link": None}


# ---------------------------------------------------------------- legacy rows


def _make_legacy(msg, *, mapped):
    """Simulate a row queued by older code: private links inside the canonical group and
    under mapped names, whatever the destination's current settings."""
    legacy = json.loads(json.dumps(msg.payload))
    legacy["event"]["occurrence"]["joinUrl"] = JOIN_URL
    legacy["event"]["occurrence"]["waitingRoomUrl"] = WAITING_URL
    legacy["mapped"] = mapped
    OutboxMessage.objects.filter(pk=msg.pk).update(payload=legacy)


def test_legacy_mapped_private_link_is_stripped(transport, occurrence):
    msg, _dest = _enqueue(
        occurrence, include_private_links=False, field_mapping={"event.occurrence.joinUrl": "join_link"}
    )
    _make_legacy(msg, mapped={"join_link": JOIN_URL, "stale_target": WAITING_URL})

    body, call = _deliver_one(transport)
    _assert_no_provider_links(call["body"].decode("utf-8"))
    assert body["mapped"] == {"join_link": None}


def test_legacy_mapped_private_link_stripped_when_room_enabled(transport, occurrence):
    msg, _dest = _enqueue(
        occurrence, include_private_links=True, field_mapping={"event.occurrence.joinUrl": "join_link"}
    )
    _make_legacy(msg, mapped={"join_link": JOIN_URL})
    room = _enable_room(occurrence)

    body, call = _deliver_one(transport)
    _assert_no_provider_links(call["body"].decode("utf-8"))
    assert body["mapped"] == {"join_link": None}
    assert body["event"]["occurrence"]["managedRoomUrl"] == room


# ---------------------------------------------------------------- signature and idempotency


def test_refiltered_body_is_signed_and_delivery_id_stable(transport, occurrence):
    msg, dest = _enqueue(occurrence, include_private_links=True)
    _update_destination(dest, include_private_links=False)

    body, call = _deliver_one(transport)
    h = call["headers"]
    assert h["Webinar-Delivery-Id"] == str(msg.id)
    assert body["delivery_metadata"]["deliveryId"] == str(msg.id)
    assert verify_webhook_signature(
        WEBHOOK_SECRET, h["Webinar-Delivery-Id"], h["Webinar-Timestamp"], call["body"], h["Webinar-Signature"]
    )
