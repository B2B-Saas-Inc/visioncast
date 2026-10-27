"""End-to-end operational acceptance: one complete webinar workflow through the public
interfaces (docs/release-checklist.md "E2E acceptance").

create (management API) -> provision (Zoom, preview/confirm) -> register/pay (hosted waiting
room, mailbox verification, Stripe Checkout, signed webhook) -> verify -> attend (doors open,
check-in, finalization) -> CRM follow-up (native HubSpot delivery and a signed generic
webhook, run by the worker's claim/deliver loop).

Every vendor is a fake transport: Zoom, Stripe, HubSpot and the webhook receiver. Mail goes to
the preview mailbox (stored, never sent). No network.
"""
import json
import re
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

import webinars.delivery
import webinars.ssrf
from tests.conftest import COOKIE, make_api_key, make_destination, make_form, sha256
from tests.test_hosting import TOKEN, ZOOM_SECRETS, Fake
from tests.test_tickets import SK, WHSEC, FakeStripe, _deliver, _event
from webinars.crypto import encrypt_secret
from webinars.hosting import base
from webinars.models import (
    AttendanceEvent,
    BrowserCapability,
    CheckIn,
    Destination,
    HostedSession,
    Occurrence,
    OutboxMessage,
    PreviewMessage,
    Registration,
    TicketOrder,
)

pytestmark = pytest.mark.django_db

BASE = "/api/v1/manage"
ZOOM_JOIN = "https://us02web.zoom.us/w/81234567890?tk=attendee-token"
ZOOM_HOST = "https://us02web.zoom.us/s/81234567890?zak=HOST-SECRET"
HUBSPOT = {
    "native_provider": "hubspot",
    "native_region": "global",
    "native_access_token": "hs-token-e2e-secret",
    "native_person_match": "email",
    "native_email_merge_ack": "confirmed",
    "native_participation_object_type": "2-123",
    "native_participation_id_property": "webinar_registration_id",
    "native_processing_ack": "confirmed",
}


@pytest.fixture(autouse=True)
def e2e_settings(settings):
    settings.EMAIL_BACKEND = "webinars.mail.PreviewMailboxBackend"
    settings.WAITING_ROOM_VERIFICATION_TTL_MINUTES = 15
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", "zoom.us", "api.zoom.us", "api.hubapi.com"]
    base.clear_token_cache()
    yield settings
    base.clear_token_cache()


@pytest.fixture
def stripe(monkeypatch):
    from webinars import payments

    fake = FakeStripe()
    monkeypatch.setattr(payments, "stripe_request", fake)
    payments.save_account(mode="test", secret_key=SK, webhook_secret=WHSEC, enabled=True)
    return fake


class HubSpotFake:
    """``ssrf.request_json_bounded`` stand-in: records every call, answers with record ids."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, body, headers, timeout, total, max_bytes=None, allowed_hosts=None, **kw):
        self.calls.append({"method": method, "url": url, "json": json.loads(body) if body else None,
                           "headers": dict(headers), "allowed": set(allowed_hosts or ())})
        return 200, json.dumps({"results": [{"id": str(len(self.calls))}]}).encode(), None, False


class WebhookFake:
    """``post_json_pinned`` stand-in for the generic signed webhook."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, data, headers, timeout):
        body = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        return 200, ""


def call(client, method, path, key, body=None, confirm=None):
    headers = {"Authorization": f"Bearer {key}"}
    if confirm:
        headers["Webinar-Confirm-Token"] = confirm
    if method == "GET":
        return client.get(path, headers=headers)
    return getattr(client, method.lower())(path, data=json.dumps(body or {}), content_type="application/json", headers=headers)


def confirmed(client, method, path, key, body):
    first = call(client, method, path, key, body)
    assert first.status_code == 428, first.content
    return call(client, method, path, key, body, confirm=first.json()["confirmToken"])


def _room(occ, suffix=""):
    return f"/w/{occ.waiting_room_code}/{suffix}"


def _verify_mailbox(browser, occ, email):
    before = set(PreviewMessage.objects.values_list("pk", flat=True))
    sent = browser.post(_room(occ, "identify"), {"email": email})
    assert sent.status_code == 303
    new = list(PreviewMessage.objects.exclude(pk__in=before))
    assert len(new) == 1
    code = re.search(r"\b(\d{6})\b", new[0].subject).group(1)
    verified = browser.post(_room(occ, "verify"), {"code": code})
    assert verified.status_code == 303 and verified["Location"] == _room(occ)
    cap = BrowserCapability.objects.get(token_hash=sha256(browser.cookies[COOKIE].value))
    assert cap.mailbox_verified_at is not None
    return cap


def _run_worker(limit=20):
    """Claim and deliver everything that is due, like ``run_worker`` does."""
    delivered = []
    for _ in range(limit):
        batch = webinars.delivery.claim_batch("e2e-worker")
        if not batch:
            break
        for msg in batch:
            webinars.delivery.deliver(msg)
            msg.refresh_from_db()
            delivered.append(msg)
    return delivered


def test_complete_operational_webinar_workflow(client, stripe, monkeypatch):
    form = make_form(version="e2e-v1")
    content_key = make_api_key(("content_write",), name="cms")
    publish_key = make_api_key(("content_write", "publication"), name="publisher")
    payments_key = make_api_key(("payments_admin", "content_write"), name="pricing")
    hosting_key = make_api_key(("hosting_admin", "hosting_provision"), name="hosting")
    read_key = make_api_key(("config_read",), name="agent")

    # ---- 1. Create: event, session, price, waiting room, publish (two-step confirmation)
    event = call(client, "POST", f"{BASE}/events", content_key, {
        "internalName": "E2E launch webinar", "slug": "e2e-launch", "publicTitle": "Launching with confidence",
        "formVersion": form.version, "learningOutcomes": ["Plan", "Ship"], "summary": "A complete launch walkthrough.",
        "attention": "Ship your next launch with confidence.", "action": "Register now",
    })
    assert event.status_code == 201, event.content
    event_id = event.json()["id"]
    local_start = (timezone.now() + timedelta(days=5)).strftime("%Y-%m-%dT12:00")
    session = call(client, "POST", f"{BASE}/events/{event_id}/occurrences", content_key,
                   {"timezone": "Europe/London", "localStart": local_start, "durationMinutes": 45})
    assert session.status_code == 201, session.content
    occ_id = session.json()["id"]
    priced = call(client, "PATCH", f"{BASE}/occurrences/{occ_id}/tickets", payments_key, {"priceMinor": 2500, "currency": "usd"})
    assert priced.status_code == 200, priced.content
    room = call(client, "PATCH", f"{BASE}/occurrences/{occ_id}/waiting-room", content_key, {"enabled": True, "admissionLeadMinutes": 15})
    assert room.status_code == 200 and room.json()["enabled"] is True, room.content
    assert call(client, "PATCH", f"{BASE}/events/{event_id}", content_key, {"visibility": "public"}).status_code == 403
    published = confirmed(client, "PATCH", f"{BASE}/events/{event_id}", publish_key, {"visibility": "public"})
    assert published.status_code == 200 and published.json()["visibility"] == "public", published.content
    public = client.get("/api/v1/events/e2e-launch")
    assert public.status_code == 200 and public.json()["occurrences"][0]["id"] == occ_id
    occ = Occurrence.objects.get(pk=occ_id)
    assert occ.join_url == "" and occ.waiting_room_code

    # ---- 2. Provision: connect Zoom, preview, confirm; the attendee link becomes the private join link
    account = call(client, "POST", f"{BASE}/hosting/accounts", hosting_key, {
        "provider": "zoom", "name": "Zoom (owner)", "enabled": True,
        "config": {"host_user_id": "host@example.com"}, "credentials": dict(ZOOM_SECRETS),
    })
    assert account.status_code == 201, account.content
    assert ZOOM_SECRETS["client_secret"] not in account.content.decode()
    account_id = account.json()["id"]
    preview = call(client, "POST", f"{BASE}/occurrences/{occ_id}/hosting/preview", hosting_key, {"accountId": account_id, "action": "create"})
    assert preview.status_code == 200 and preview.json()["problems"] == [], preview.content
    assert "client-secret" not in preview.content.decode()
    zoom = Fake(TOKEN, (201, {"id": 81234567890, "join_url": ZOOM_JOIN, "start_url": ZOOM_HOST}))
    monkeypatch.setattr(base, "default_transport", zoom)
    linked = confirmed(client, "POST", f"{BASE}/occurrences/{occ_id}/hosting/execute", hosting_key, {"accountId": account_id, "action": "create"})
    assert linked.status_code == 200 and linked.json()["state"] == "linked", linked.content
    assert "HOST-SECRET" not in linked.content.decode() and ZOOM_JOIN not in linked.content.decode()
    assert [c["method"] for c in zoom.calls] == ["POST", "POST"]
    assert zoom.calls[1]["url"] == "https://api.zoom.us/v2/users/host@example.com/webinars"
    occ.refresh_from_db()
    assert occ.join_url == ZOOM_JOIN
    hosted = HostedSession.objects.get(occurrence=occ)
    assert hosted.external_id == "81234567890" and hosted.host_url_encrypted
    state = call(client, "GET", f"{BASE}/occurrences/{occ_id}/hosting", read_key)
    assert state.status_code == 200 and state.json()["hasHostUrl"] is True and "HOST-SECRET" not in state.content.decode()

    # ---- CRM destinations: native HubSpot (email match, confirmed) and a signed generic webhook
    make_destination(name="HubSpot", url="https://api.hubapi.com", event_types=["registration.created", "attendance.recorded"],
                     credentials_encrypted=encrypt_secret(json.dumps(HUBSPOT)), required_consents=[])
    make_destination(name="Marketing automation", kind=Destination.Kind.EMAIL,
                     event_types=["registration.created", "attendance.recorded"], include_private_links=False)
    hubspot = HubSpotFake()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", hubspot)
    webhook = WebhookFake()
    monkeypatch.setattr(webinars.delivery, "post_json_pinned", webhook, raising=False)
    monkeypatch.setattr(webinars.ssrf, "post_json_pinned", webhook)

    # ---- 3. Register / pay: the hosted room verifies the mailbox, then Stripe Checkout; only the signed webhook registers
    browser = Client()
    assert browser.get(_room(occ)).status_code == 200
    cap = _verify_mailbox(browser, occ, "nia.buyer@example.com")
    checkout = browser.post(_room(occ, "register"), {"firstName": "Nia", "idempotency_key": "e2e-" + "a" * 32})
    assert checkout.status_code == 303 and checkout["Location"].startswith("https://checkout.stripe.com/")
    order = TicketOrder.objects.get(occurrence=occ)
    assert order.status == "checkout_open" and order.amount_minor == 2500
    assert not Registration.objects.exists()
    assert browser.post(_room(occ, "enter")).status_code == 303  # unpaid: never the join link
    assert not CheckIn.objects.exists()
    assert _deliver(browser, _event(order)).status_code == 200
    order.refresh_from_db()
    reg = Registration.objects.get(occurrence=occ, status="registered")
    assert order.status == "fulfilled" and order.registration_id == reg.pk and reg.person_id == cap.person_id
    orders = call(client, "GET", f"{BASE}/occurrences/{occ_id}/orders", payments_key)
    assert orders.status_code == 200 and orders.json()["results"][0]["status"] == "fulfilled"

    # ---- 4. Verify: the registration is bound to a mailbox-verified browser capability
    assert reg.identity_verified is True
    assert BrowserCapability.objects.get(pk=cap.pk).mailbox_verified_at is not None
    status = browser.get(_room(occ, "status")).json()
    assert status["viewer"] == {"recognized": True, "registered": True} and status["admit"] is False

    # The registration event reaches the CRM before the session (worker pass).
    first_pass = _run_worker()
    assert sorted(m.event_type for m in first_pass) == ["registration.created", "registration.created"]
    assert all(m.status == OutboxMessage.Status.DELIVERED for m in first_pass)
    contact = hubspot.calls[0]
    assert contact["url"] == "https://api.hubapi.com/crm/v3/objects/contacts/batch/upsert"
    assert contact["allowed"] == {"api.hubapi.com"} and contact["headers"]["Authorization"] == "Bearer hs-token-e2e-secret"
    assert contact["json"]["inputs"][0] == {"idProperty": "email", "id": "nia.buyer@example.com",
                                            "properties": {"email": "nia.buyer@example.com", "firstname": "Nia"}}
    [hook] = webhook.calls
    assert hook["url"] == "https://hooks.example.net/webhook" and "Webinar-Signature" in hook["headers"]
    assert ZOOM_JOIN not in hook["body"] and "HOST-SECRET" not in hook["body"]

    # ---- 5. Attend: doors open on the host's say-so, the room admits to the private Zoom link, then ends
    set_mode = call(client, "PATCH", f"{BASE}/occurrences/{occ_id}/waiting-room", content_key, {"admissionMode": "open"})
    assert set_mode.status_code == 200 and set_mode.json()["state"] == "open", set_mode.content
    entered = browser.post(_room(occ, "enter"))
    assert entered.status_code == 303 and entered["Location"] == ZOOM_JOIN
    assert CheckIn.objects.filter(registration=reg).count() == 1
    assert browser.post(_room(occ, "enter")).status_code == 303 and CheckIn.objects.count() == 1  # idempotent
    ended = call(client, "PATCH", f"{BASE}/occurrences/{occ_id}/waiting-room", content_key, {"admissionMode": "ended"})
    assert ended.status_code == 200 and ended.json()["state"] == "ended", ended.content
    reg.refresh_from_db()
    occ.refresh_from_db()
    assert reg.attendance == Registration.Attendance.ATTENDED and occ.attendance_finalized_at is not None
    assert AttendanceEvent.objects.filter(registration=reg, kind="attended", source="waiting_room").count() == 1

    # ---- 6. CRM follow-up: the attended event reaches HubSpot (participation record) and the webhook receiver
    second_pass = _run_worker()
    assert sorted(m.event_type for m in second_pass) == ["attendance.recorded", "attendance.recorded"]
    assert all(m.status == OutboxMessage.Status.DELIVERED for m in second_pass)
    participation = [c for c in hubspot.calls if "/objects/2-123/batch/upsert" in c["url"]]
    assert len(participation) == 2  # registration.created, then attendance.recorded, keyed by the same registration
    assert participation[-1]["json"]["inputs"][0]["id"] == str(reg.pk)
    assert participation[-1]["json"]["inputs"][0]["properties"]["webinar_attendance"] == "attended"
    assert participation[-1]["json"]["inputs"][0]["properties"]["webinar_last_event_type"] == "attendance.recorded"
    follow_up = json.loads(webhook.calls[-1]["body"])
    assert follow_up["eventType"] == "attendance.recorded" and follow_up["registration"]["attendance"] == "attended"
    everything_sent = json.dumps([c["json"] for c in hubspot.calls]) + "".join(c["body"] for c in webhook.calls)
    assert ZOOM_JOIN not in everything_sent and "HOST-SECRET" not in everything_sent and "hs-token" not in everything_sent

    # Nothing is left pending; a further worker pass sends nothing.
    assert _run_worker() == []
    assert not OutboxMessage.objects.exclude(status=OutboxMessage.Status.DELIVERED).exists()
    summary = call(client, "GET", f"{BASE}/events/{event_id}", read_key)
    assert summary.status_code == 200
