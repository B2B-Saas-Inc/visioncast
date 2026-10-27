"""Native config changed between vendor calls (.runs/brevo-review-result.md F2).

``execute`` plans every step from the config read before the first call; ``_native_step_check``
compares the refreshed destination with that snapshot and stops instead of sending the stale plan.
Mocked only: no network.
"""
import json

import pytest

import webinars.delivery as delivery
import webinars.ssrf
from tests.test_native_integrations import HUBSPOT, VENDOR_HOSTS, _claimed
from tests.test_native_review_fixes import FakeRequest
from webinars.crypto import encrypt_secret
from webinars.models import Destination, OutboxMessage

pytestmark = pytest.mark.django_db
S = OutboxMessage.Status


@pytest.fixture(autouse=True)
def vendor_allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = list(VENDOR_HOSTS)


def _run(occurrence, monkeypatch, mutate):
    msg, _reg = _claimed(occurrence, HUBSPOT, required_consents=["analytics"])
    fake = FakeRequest((lambda: Destination.objects.filter(pk=msg.destination_id).update(**mutate)) if mutate else None)
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    delivery.deliver(msg)
    msg.refresh_from_db()
    return msg, fake


@pytest.mark.parametrize(
    "mutate",
    [
        dict(allowed_groups=["identity", "registration", "event"]),
        dict(credentials_encrypted=encrypt_secret(json.dumps({**HUBSPOT, "native_access_token": "hs-token-NEW"}))),
        dict(field_mapping={"profile.firstName": "first_name_alt"}),
        dict(include_private_links=True),
        dict(event_types=["registration.created", "registration.cancelled"]),
    ],
    ids=["allowed_groups", "credentials", "field_mapping", "private_links", "event_types"],
)
def test_config_change_after_first_call_stops_before_next(occurrence, monkeypatch, mutate):
    msg, fake = _run(occurrence, monkeypatch, mutate)
    assert len(fake.calls) == 1  # the first write is not recalled; no stale second request
    assert msg.status == S.DEAD and msg.last_error == "native_config_changed_mid_sequence"
    assert "hs-token" not in msg.last_error


def test_unchanged_config_still_completes(occurrence, monkeypatch):
    msg, fake = _run(occurrence, monkeypatch, None)
    assert msg.status == S.DELIVERED and len(fake.calls) > 1


def test_consent_and_disable_keep_their_outcomes(occurrence, monkeypatch):
    # Disabled is checked before the snapshot: parked, not dead, even if config also changed.
    msg, fake = _run(occurrence, monkeypatch, dict(enabled=False, allowed_groups=["identity"]))
    assert len(fake.calls) == 1 and msg.status == S.PENDING and msg.last_error == "destination_disabled_mid_sequence"


def test_consent_tightening_still_cancels(occurrence, monkeypatch):
    msg, fake = _run(occurrence, monkeypatch, dict(required_consents=["analytics", "sms"]))
    assert len(fake.calls) == 1 and msg.status == S.CANCELLED and msg.last_error == "consent_not_granted_mid_sequence"


def test_checker_without_snapshot_stays_compatible(occurrence):
    msg, _reg = _claimed(occurrence, HUBSPOT, required_consents=["analytics"])
    assert delivery._native_step_check(msg, "hubspot") is None
    dest = Destination.objects.get(pk=msg.destination_id)
    assert delivery._native_step_check(msg, "hubspot", delivery._native_plan_snapshot(dest)) is None
