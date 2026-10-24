#!/usr/bin/env python3
"""Replay-safe webhook verification example (Python standard library only).

Verifies the ``Webinar-Signature`` header produced by webinar-platform:

    signed   = f"{delivery_id}.{timestamp}.".encode() + raw_body
    expected = "v1=" + hmac_sha256_hex(secret, signed)

and enforces:

1. constant-time signature comparison (``hmac.compare_digest``),
2. a timestamp tolerance window (default 300 s) against the receiver clock,
3. replay protection: each ``Webinar-Delivery-Id`` is accepted at most once while
   it could still pass the timestamp check.

Delivery ids are stable across retries, so a retry of a delivery you already
processed is acknowledged (2xx) without being processed twice.

The in-memory ``SeenDeliveries`` store is for demonstration only. In production use
a durable shared store, e.g. a database table with a UNIQUE constraint on the
delivery id inserted in the same transaction as your side effects.

Run a local demo receiver (do not expose publicly as-is):

    WEBHOOK_SECRET=... python3 examples/verify_webhook.py --serve 8080

Or a self-test:

    python3 examples/verify_webhook.py --self-test
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 1024 * 1024


def compute_signature(secret: str, delivery_id: str, timestamp: int, body: bytes) -> str:
    signed = f"{delivery_id}.{int(timestamp)}.".encode("utf-8") + body
    return "v1=" + hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def verify_signature(secret, delivery_id, timestamp, body, header, tolerance=TOLERANCE_SECONDS, now=None) -> bool:
    if not (secret and delivery_id and header and timestamp):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    now = time.time() if now is None else now
    if abs(now - ts) > tolerance:
        return False
    expected = compute_signature(secret, delivery_id, ts, body)
    # The header may contain several space/comma separated signatures during rotation.
    return any(hmac.compare_digest(c.strip(), expected) for c in header.replace(",", " ").split())


class SeenDeliveries:
    """In-memory replay store with expiry. Use a database table in production."""

    def __init__(self, ttl=2 * TOLERANCE_SECONDS + 60):
        self.ttl = ttl
        self._seen = {}
        self._lock = threading.Lock()

    def check_and_add(self, delivery_id: str, now=None) -> bool:
        """Return True if ``delivery_id`` is new (and record it), False if replayed."""
        now = time.time() if now is None else now
        with self._lock:
            for key in [k for k, exp in self._seen.items() if exp <= now]:
                del self._seen[key]
            if delivery_id in self._seen:
                return False
            self._seen[delivery_id] = now + self.ttl
            return True


SEEN = SeenDeliveries()


def handle_delivery(secret, headers, body, store=SEEN, now=None):
    """Return (http_status, message). Process the event only when status == 200 and message == 'processed'."""
    delivery_id = headers.get("Webinar-Delivery-Id", "")
    timestamp = headers.get("Webinar-Timestamp", "")
    signature = headers.get("Webinar-Signature", "")
    if not verify_signature(secret, delivery_id, timestamp, body, signature, now=now):
        return 401, "invalid signature or stale timestamp"
    if not store.check_and_add(delivery_id, now=now):
        return 200, "duplicate delivery ignored"
    try:
        payload = json.loads(body)
    except ValueError:
        return 400, "invalid json"
    if payload.get("schemaVersion") != "1":
        return 400, "unsupported schema version"
    # ... process payload["eventType"] here (idempotently, keyed by delivery id) ...
    return 200, "processed"


class _Handler(BaseHTTPRequestHandler):
    secret = ""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(length)  # verify the RAW bytes, before any JSON parsing
        status, message = handle_delivery(self.secret, self.headers, body)
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(message.encode())

    def log_message(self, fmt, *args):  # avoid logging payloads
        sys.stderr.write("%s %s\n" % (self.command, args[1] if len(args) > 1 else ""))


def _self_test():
    secret = "test-secret-not-real-0123456789"
    body = b'{"eventType":"registration.created","schemaVersion":"1"}'
    now = 1_700_000_000
    did = "8d3f1c3e-0000-4000-8000-000000000001"
    headers = {
        "Webinar-Delivery-Id": did,
        "Webinar-Timestamp": str(now),
        "Webinar-Signature": compute_signature(secret, did, now, body),
    }
    store = SeenDeliveries()
    assert handle_delivery(secret, headers, body, store, now=now) == (200, "processed")
    assert handle_delivery(secret, headers, body, store, now=now + 1) == (200, "duplicate delivery ignored")
    assert handle_delivery(secret, headers, body + b" ", store, now=now)[0] == 401
    assert handle_delivery(secret, headers, body, SeenDeliveries(), now=now + 301)[0] == 401
    assert handle_delivery("wrong-secret", headers, body, SeenDeliveries(), now=now)[0] == 401
    print("self-test ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
    elif "--serve" in sys.argv:
        port = int(sys.argv[sys.argv.index("--serve") + 1])
        _Handler.secret = os.environ.get("WEBHOOK_SECRET", "")
        if not _Handler.secret:
            sys.exit("Set WEBHOOK_SECRET")
        HTTPServer(("127.0.0.1", port), _Handler).serve_forever()
    else:
        print(__doc__)
