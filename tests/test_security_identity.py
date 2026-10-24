"""Regression tests for security review findings L1, L2, L3, L4, L5, L6, L7 and B4."""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.conftest import (
    cookie_value,
    get_profile,
    make_destination,
    make_occurrence,
    payload,
    post_registration,
    register_with_cookie,
    sha256,
)
from webinars import attribution
from webinars.models import (
    AttributionTouch,
    BrowserCapability,
    ConsentRecord,
    IdempotencyRecord,
    OrganizationExternalId,
    OutboxMessage,
    Person,
    Registration,
    ReminderSchedule,
)

pytestmark = pytest.mark.django_db

VICTIM_CONSENTS = {
    "sms": {"granted": True, "disclosureVersion": "sms-v1"},
    "analytics": {"granted": True},
    "advertising": {"granted": True},
}


def _victim(client, occurrence, proxy_key):
    _r, token = register_with_cookie(
        client,
        occurrence,
        proxy_key,
        profile={"phone": "+447700900123"},
        consents=VICTIM_CONSENTS,
        attribution={"parameters": {"utm_source": "victim"}},
    )
    return Person.objects.get(), token


# ---------------------------------------------------------------- L2


def test_unverified_submission_records_no_consent_against_person(client, event, occurrence, proxy_key):
    victim, _token = _victim(client, occurrence, proxy_key)
    before = ConsentRecord.objects.count()
    occ2 = make_occurrence(event, days=20)
    resp = post_registration(
        client,
        payload(occ2, consents={"sms": {"granted": False}, "analytics": {"granted": False}, "advertising": {"granted": False}}),
        proxy_key,
    )
    assert resp.status_code == 201
    assert ConsentRecord.objects.count() == before
    assert not ConsentRecord.objects.filter(person=victim, withdrawn_at__isnull=False).exists()
    reg = Registration.objects.get(occurrence=occ2)
    assert reg.submitted_profile["consentClaims"]["sms"]["granted"] is False


# ---------------------------------------------------------------- L1


def test_unverified_payload_does_not_blend_victim_consent_org_or_first_touch(client, event, occurrence, proxy_key):
    from webinars.models import Destination

    victim, _token = _victim(client, occurrence, proxy_key)
    OrganizationExternalId.objects.create(
        person=victim, namespace="crm", value="REAL-1", source=OrganizationExternalId.Source.TRUSTED_API, verified=True
    )
    make_destination()
    sms_dest = make_destination(name="sms", required_consents=["sms"])
    occ2 = make_occurrence(event, days=20)
    resp = post_registration(
        client,
        payload(
            occ2,
            profile={"phone": "+447700900999", "firstName": "X"},
            organization={"externalIds": [{"namespace": "crm", "value": "ATTACKER"}]},
        ),
        proxy_key,
    )
    assert resp.status_code == 201
    msgs = OutboxMessage.objects.filter(event_type="registration.created", payload__registration__identityVerified=False)
    # Destination requiring SMS consent never receives the unverified submission.
    assert not msgs.filter(destination=sms_dest).exists()
    p = msgs.get().payload
    assert p["identity"]["verified"] is False
    assert p["profile"]["phone"] == "+447700900999"
    assert all(c["granted"] is False and c["recordedAt"] is None for c in p["consents"].values())
    assert p["organization"]["externalIds"] == [
        {"namespace": "crm", "value": "ATTACKER", "verified": False, "source": "browser"}
    ]
    assert p["attribution"]["firstTouch"] is None
    assert Destination.objects.count() == 2


# ---------------------------------------------------------------- L4 / L5


def test_unverified_duplicate_does_not_reactivate_or_mutate(client, occurrence, proxy_key):
    victim, _token = _victim(client, occurrence, proxy_key)
    reg = Registration.objects.get()
    Registration.objects.filter(pk=reg.pk).update(status=Registration.Status.CANCELLED)
    ReminderSchedule.objects.all().delete()
    make_destination(event_types=("registration.created", "registration.updated"))
    reg.refresh_from_db()
    snapshot = (reg.updated_at, reg.submitted_profile, reg.custom_fields, reg.form_version)
    touches = AttributionTouch.objects.count()

    resp = post_registration(
        client,
        payload(occurrence, profile={"firstName": "Mallory"}, consents={"analytics": {"granted": True}},
                attribution={"parameters": {"utm_source": "attacker"}}),
        proxy_key,
    )
    assert resp.status_code == 201
    reg.refresh_from_db()
    assert reg.status == Registration.Status.CANCELLED
    assert (reg.updated_at, reg.submitted_profile, reg.custom_fields, reg.form_version) == snapshot
    assert AttributionTouch.objects.count() == touches
    assert not ReminderSchedule.objects.exists()
    assert not OutboxMessage.objects.exists()
    victim_touch = AttributionTouch.objects.get(registration=reg, touch_type="registration_touch")
    assert victim_touch.parameters == {"utm_source": "victim"}


# ---------------------------------------------------------------- L3


def test_set_cookie_uniform_for_new_and_existing_email(client, event, occurrence, proxy_key):
    _victim(client, occurrence, proxy_key)
    occ2 = make_occurrence(event, days=20)
    new = post_registration(client, payload(occ2, profile={"email": "brand.new@example.org"}), proxy_key)
    existing = post_registration(client, payload(occ2), proxy_key)
    assert new.status_code == existing.status_code == 201
    new_token, decoy = cookie_value(new), cookie_value(existing)
    assert new_token and decoy and len(new_token) == len(decoy)
    assert set(new.cookies) == set(existing.cookies)
    assert BrowserCapability.objects.filter(token_hash=sha256(new_token)).exists()
    assert not BrowserCapability.objects.filter(token_hash=sha256(decoy)).exists()
    assert get_profile(client, occurrence, proxy_key, cookie=decoy).json()["recognized"] is False


# ---------------------------------------------------------------- L7


def test_retry_with_issued_cookie_replays_instead_of_409(client, occurrence, proxy_key):
    body = payload(occurrence)
    first = post_registration(client, body, proxy_key)
    token = cookie_value(first)
    retry = post_registration(client, body, proxy_key, cookie=token)
    assert retry.status_code == first.status_code
    assert retry.json() == first.json()
    assert Registration.objects.count() == 1


def test_replay_into_another_persons_context_conflicts(client, event, occurrence, proxy_key):
    body = payload(occurrence)
    assert post_registration(client, body, proxy_key).status_code == 201
    occ2 = make_occurrence(event, days=20)
    _r, other_token = register_with_cookie(client, occ2, proxy_key, profile={"email": "other@example.org"})
    resp = post_registration(client, body, proxy_key, cookie=other_token)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "idempotency_conflict"


def test_changed_payload_still_conflicts(client, occurrence, proxy_key):
    body = payload(occurrence)
    first = post_registration(client, body, proxy_key)
    resp = post_registration(client, dict(body, profile={"email": "x@example.org", "firstName": "X"}), proxy_key, cookie=cookie_value(first))
    assert resp.status_code == 409


# ---------------------------------------------------------------- L6


def test_lost_response_retry_replays_without_reissuing(client, occurrence, proxy_key):
    """Key + payload do not prove ownership: a cookie-less replay never gets a real
    capability and never revokes the original (recognition may be lost instead)."""
    body = payload(occurrence)
    first = post_registration(client, body, proxy_key)
    original = cookie_value(first)
    retry = post_registration(client, body, proxy_key)  # cookie never arrived
    assert retry.status_code == first.status_code
    assert retry.json() == first.json()
    decoy = cookie_value(retry)
    assert decoy and decoy != original
    assert not BrowserCapability.objects.filter(token_hash=sha256(decoy)).exists()
    assert BrowserCapability.objects.filter(revoked_at__isnull=True).count() == 1
    assert get_profile(client, occurrence, proxy_key, cookie=original).json()["recognized"] is True
    assert get_profile(client, occurrence, proxy_key, cookie=decoy).json()["recognized"] is False


def test_duplicate_submission_cannot_steal_victims_capability(client, event, occurrence, proxy_key):
    """Attacker re-submits the victim's email onto the victim's registration right away,
    then replays their own key cookie-less: only decoys come back, the victim keeps theirs."""
    victim, victim_token = _victim(client, occurrence, proxy_key)
    body = payload(occurrence)
    for _ in range(2):
        token = cookie_value(post_registration(client, body, proxy_key))
        assert not BrowserCapability.objects.filter(token_hash=sha256(token)).exists()
    assert list(BrowserCapability.objects.filter(revoked_at__isnull=True)) == list(victim.capabilities.all())
    assert get_profile(client, occurrence, proxy_key, cookie=victim_token).json()["recognized"] is True


def test_retry_after_registering_someone_else_replays(client, event, occurrence, proxy_key):
    """L7: a capability holder registers another person (rule 6); the identical retry
    with the same cookie replays the uniform body, with no cookie and no identity data."""
    _r, token = register_with_cookie(client, occurrence, proxy_key)
    occ2 = make_occurrence(event, days=20)
    body = payload(occ2, profile={"email": "colleague@example.org", "firstName": "Col"})
    first = post_registration(client, body, proxy_key, cookie=token)
    assert first.status_code == 201
    retry = post_registration(client, body, proxy_key, cookie=token)
    assert retry.status_code == first.status_code
    assert retry.json() == first.json()
    assert cookie_value(retry) is None
    assert "profile" not in retry.json() and "identity" not in retry.json()
    assert Registration.objects.filter(occurrence=occ2).count() == 1
    # A different capability holder replaying the same key still conflicts.
    _r2, other = register_with_cookie(client, occ2, proxy_key, profile={"email": "third@example.org"})
    assert post_registration(client, body, proxy_key, cookie=other).status_code == 409


def test_replay_sets_no_real_cookie_even_soon_after(client, occurrence, proxy_key):
    body = payload(occurrence, idempotencyKey="k" * 40)
    post_registration(client, body, proxy_key)
    IdempotencyRecord.objects.update(created_at=timezone.now() - timedelta(seconds=1))
    token = cookie_value(post_registration(client, body, proxy_key))
    assert not BrowserCapability.objects.filter(token_hash=sha256(token)).exists()


def test_no_reissue_for_unverified_existing_email(client, event, occurrence, proxy_key):
    _victim(client, occurrence, proxy_key)
    occ2 = make_occurrence(event, days=20)
    body = payload(occ2)
    post_registration(client, body, proxy_key)
    token = cookie_value(post_registration(client, body, proxy_key))
    assert not BrowserCapability.objects.filter(token_hash=sha256(token)).exists()
    assert BrowserCapability.objects.count() == 1


# ---------------------------------------------------------------- B4


def test_attribution_requires_consent():
    raw = {
        "parameters": {"utm_source": "x", "gclid": "g"},
        "identifiers": {"_ga": "GA1.1.1.2"},
        "pageUrl": "https://www.example.com/p?q=1",
        "referrer": "https://ref.example.net/",
    }
    none = attribution.clean_attribution(raw, {"analytics": {"granted": False}, "advertising": {"granted": False}})
    assert none == {"parameters": {}, "identifiers": {}, "pageUrl": "", "referrer": ""}
    analytics = attribution.clean_attribution(raw, {"analytics": {"granted": True}})
    assert analytics["parameters"] == {"utm_source": "x"}
    assert analytics["pageUrl"] == "https://www.example.com/p"


@pytest.mark.parametrize("value", ["{keyword}", "{{campaign}}", "abc{keyword}", "x}"])
def test_unpopulated_valuetrack_rejected(value):
    out = attribution.clean_attribution({"parameters": {"keyword": value}}, {"analytics": {"granted": True}})
    assert out["parameters"] == {}
