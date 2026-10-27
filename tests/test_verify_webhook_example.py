"""Receiver contract (release checklist A13, offline): ``examples/verify_webhook.py``
accepts deliveries signed by the platform's own signing code (``webinars.crypto``,
the same call ``webinars.delivery.deliver`` makes) and refuses the documented
failure cases: tampered body, stale timestamp, wrong secret, and a replayed
``Webinar-Delivery-Id`` is acknowledged without being processed twice.

No database, network or Django app state: the example is loaded from its file path
and exercised in-process. A receiver reachable over the internet is a separate,
owner-run check (release checklist C5).
"""
import importlib.util
import json
import pathlib
import uuid

import pytest

from webinars.crypto import SIGNATURE_VERSION, sign_webhook, verify_webhook_signature

EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / "examples" / "verify_webhook.py"
SECRET = "synthetic-signing-secret-not-a-live-credential"
NOW = 1_700_000_000


@pytest.fixture(scope="module")
def receiver():
    spec = importlib.util.spec_from_file_location("verify_webhook_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _platform_delivery(delivery_id=None, timestamp=NOW, event_type="registration.created", attempt=1):
    """Body and headers exactly as ``webinars.delivery.deliver`` builds them."""
    delivery_id = delivery_id or str(uuid.uuid4())
    payload = {
        "schemaVersion": "1",
        "eventType": event_type,
        "occurredAt": "2026-10-27T10:00:00Z",
        "identity": {"verified": False, "personId": str(uuid.uuid4())},
        "delivery_metadata": {"deliveryId": delivery_id, "attempt": attempt, "eventType": event_type},
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Webinar-Delivery-Id": delivery_id,
        "Webinar-Event-Type": event_type,
        "Webinar-Timestamp": str(timestamp),
        "Webinar-Signature": sign_webhook(SECRET, delivery_id, timestamp, body),
    }
    return delivery_id, headers, body


def test_example_signature_matches_platform_signing(receiver):
    delivery_id, headers, body = _platform_delivery()
    assert headers["Webinar-Signature"].startswith(f"{SIGNATURE_VERSION}=")
    assert receiver.compute_signature(SECRET, delivery_id, NOW, body) == headers["Webinar-Signature"]
    # The platform's own verifier agrees with the example's verifier.
    assert verify_webhook_signature(SECRET, delivery_id, headers["Webinar-Timestamp"], body, headers["Webinar-Signature"], now=NOW)
    assert receiver.verify_signature(SECRET, delivery_id, headers["Webinar-Timestamp"], body, headers["Webinar-Signature"], now=NOW)


def test_valid_delivery_is_processed_once(receiver):
    _, headers, body = _platform_delivery()
    store = receiver.SeenDeliveries()
    assert receiver.handle_delivery(SECRET, headers, body, store, now=NOW) == (200, "processed")


def test_tampered_body_is_rejected(receiver):
    _, headers, body = _platform_delivery()
    store = receiver.SeenDeliveries()
    tampered = body.replace(b'"verified":false', b'"verified":true')
    assert tampered != body
    status, message = receiver.handle_delivery(SECRET, headers, tampered, store, now=NOW)
    assert status == 401
    assert "signature" in message
    # A rejected delivery must not poison the replay store: the genuine one still processes.
    assert receiver.handle_delivery(SECRET, headers, body, store, now=NOW) == (200, "processed")


def test_stale_timestamp_is_rejected(receiver):
    _, headers, body = _platform_delivery(timestamp=NOW)
    store = receiver.SeenDeliveries()
    # Signature is valid for NOW; the receiver clock is 301 s later (tolerance 300 s).
    assert receiver.handle_delivery(SECRET, headers, body, store, now=NOW + receiver.TOLERANCE_SECONDS + 1)[0] == 401
    # ...and a future-dated timestamp is equally stale.
    assert receiver.handle_delivery(SECRET, headers, body, store, now=NOW - receiver.TOLERANCE_SECONDS - 1)[0] == 401
    # Inside the window it processes.
    assert receiver.handle_delivery(SECRET, headers, body, store, now=NOW + receiver.TOLERANCE_SECONDS) == (200, "processed")


def test_duplicate_delivery_id_is_acknowledged_not_processed(receiver):
    delivery_id, headers, body = _platform_delivery(attempt=1)
    store = receiver.SeenDeliveries()
    assert receiver.handle_delivery(SECRET, headers, body, store, now=NOW) == (200, "processed")
    # A platform retry keeps the delivery id but re-signs a fresh timestamp and attempt.
    _, retry_headers, retry_body = _platform_delivery(delivery_id=delivery_id, timestamp=NOW + 60, attempt=2)
    assert retry_body != body
    assert receiver.handle_delivery(SECRET, retry_headers, retry_body, store, now=NOW + 60) == (200, "duplicate delivery ignored")


def test_wrong_secret_is_rejected(receiver):
    _, headers, body = _platform_delivery()
    assert receiver.handle_delivery("other-secret", headers, body, receiver.SeenDeliveries(), now=NOW)[0] == 401


def test_rotation_header_with_two_signatures(receiver):
    delivery_id, headers, body = _platform_delivery()
    old = sign_webhook("retired-secret", delivery_id, NOW, body)
    headers = dict(headers, **{"Webinar-Signature": f"{old}, {headers['Webinar-Signature']}"})
    assert receiver.handle_delivery(SECRET, headers, body, receiver.SeenDeliveries(), now=NOW) == (200, "processed")


def test_self_test_still_passes(receiver, capsys):
    receiver._self_test()
    assert "self-test ok" in capsys.readouterr().out
