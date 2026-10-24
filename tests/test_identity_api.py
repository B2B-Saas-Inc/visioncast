from datetime import timedelta

import pytest
from django.utils import timezone

from tests.conftest import (
    COOKIE,
    cookie_value,
    get_profile,
    make_occurrence,
    payload,
    post_registration,
    register_with_cookie,
    sha256,
)
from webinars.models import BrowserCapability, Person, Registration

pytestmark = pytest.mark.django_db

FULL_FORM = {"email", "firstName", "lastName", "company", "phone"}


def _no_pii(content: bytes, *values):
    text = content.decode("utf-8").lower()
    for v in values:
        assert v.lower() not in text, f"response leaked {v!r}"


def _assert_unbound_cookie(resp):
    """Cookie-less callers always get a Set-Cookie (anti-enumeration); for an existing
    email it is a random decoy that is not stored and authenticates nothing."""
    token = cookie_value(resp)
    assert token, "uniform Set-Cookie expected"
    assert not BrowserCapability.objects.filter(token_hash=sha256(token)).exists()


def test_anonymous_profile_requests_full_form(client, occurrence, proxy_key):
    resp = get_profile(client, occurrence, proxy_key)
    assert resp.status_code == 200
    data = resp.json()
    assert data["recognized"] is False
    assert data["formVersion"] == "f1"
    assert FULL_FORM <= set(data["missingFields"])
    assert set(data) == {"recognized", "missingFields", "formVersion"}


def test_profile_requires_proxy_key(client, occurrence):
    resp = client.get("/api/v1/profile", {"occurrenceId": str(occurrence.id), "formVersion": "f1"})
    assert resp.status_code == 401


def test_registration_issues_secure_cookie_and_stores_only_hash(client, occurrence, proxy_key):
    resp, token = register_with_cookie(client, occurrence, proxy_key)
    morsel = resp.cookies[COOKIE]
    assert morsel["httponly"]
    assert morsel["secure"]
    assert morsel["samesite"].lower() == "lax"
    assert morsel["path"] == "/"
    assert len(token) >= 32
    cap = BrowserCapability.objects.get()
    assert cap.token_hash == sha256(token)
    assert not BrowserCapability.objects.filter(token_hash=token).exists()
    # Raw token is not the person id / registration id.
    person = Person.objects.get()
    assert token != str(person.id)
    assert token not in {str(r.id) for r in Registration.objects.all()}


def test_profile_with_cookie_recognized_and_reveals_only_missing_keys(client, occurrence, proxy_key):
    _resp, token = register_with_cookie(client, occurrence, proxy_key)
    resp = get_profile(client, occurrence, proxy_key, cookie=token)
    assert resp.status_code == 200
    data = resp.json()
    assert data["recognized"] is True
    missing = set(data["missingFields"])
    assert "email" not in missing and "firstName" not in missing
    assert {"lastName", "company", "phone"} <= missing
    _no_pii(resp.content, "ada.lovelace", "example.com", '"Ada"')


def test_unknown_browser_with_existing_email_cannot_hijack(client, event, occurrence, proxy_key):
    _resp, token = register_with_cookie(
        client, occurrence, proxy_key, profile={"company": "Original Ltd"}
    )
    victim = Person.objects.get()
    occ2 = make_occurrence(event, days=20)

    # A fresh new-person registration for occ2, to compare bodies.
    fresh = post_registration(
        client, payload(occ2, profile={"email": "someone.new@example.org", "firstName": "New"}), proxy_key
    )
    assert fresh.status_code == 201

    attacker = post_registration(
        client,
        payload(occ2, profile={"firstName": "Mallory", "company": "Evil Corp", "lastName": "Hacker"}),
        proxy_key,
    )
    assert attacker.status_code == fresh.status_code
    assert attacker.json() == fresh.json()
    _assert_unbound_cookie(attacker)
    _no_pii(attacker.content, "Original Ltd", "ada.lovelace")

    victim.refresh_from_db()
    assert victim.first_name == "Ada"
    assert victim.company == "Original Ltd"
    assert victim.last_name == ""
    reg = Registration.objects.get(occurrence=occ2, person=victim)
    assert reg.identity_verified is False
    assert reg.submitted_profile.get("firstName") == "Mallory" or "Mallory" in str(reg.submitted_profile)
    # Victim's capability unaffected, attacker got no capability.
    assert BrowserCapability.objects.filter(person=victim).count() == 1


def test_unknown_browser_dedupe_on_same_occurrence_identical_body(client, occurrence, proxy_key):
    first, _token = register_with_cookie(client, occurrence, proxy_key)
    again = post_registration(client, payload(occurrence, profile={"firstName": "Mallory"}), proxy_key)
    assert again.status_code in (200, 201)
    assert again.json() == first.json()
    _assert_unbound_cookie(again)
    assert Registration.objects.count() == 1
    assert Person.objects.get().first_name == "Ada"


def test_capability_holder_registering_other_email_does_not_rebind(client, event, occurrence, proxy_key):
    _resp, token = register_with_cookie(client, occurrence, proxy_key)
    original = Person.objects.get()
    cap = BrowserCapability.objects.get()
    occ2 = make_occurrence(event, days=15)

    resp = post_registration(
        client,
        payload(occ2, profile={"email": "colleague@example.org", "firstName": "Colleague", "company": "Other"}),
        proxy_key,
        cookie=token,
    )
    assert resp.status_code == 201
    new_token = cookie_value(resp)
    assert new_token is None or new_token == token

    cap.refresh_from_db()
    assert cap.person_id == original.id
    assert cap.revoked_at is None
    original.refresh_from_db()
    assert original.first_name == "Ada"
    assert original.company == ""
    assert original.email_key.startswith("ada.lovelace")
    colleague = Person.objects.get(email_key="colleague@example.org")
    assert Registration.objects.get(occurrence=occ2).person_id == colleague.id
    assert not BrowserCapability.objects.filter(person=colleague).exists()
    # Cookie still resolves to the original person.
    assert get_profile(client, occurrence, proxy_key, cookie=token).json()["recognized"] is True


def test_capability_holder_registering_existing_other_person_does_not_modify_them(client, event, occurrence, proxy_key):
    # Person B exists (registered from another browser).
    register_with_cookie(client, occurrence, proxy_key, profile={"email": "bob@example.org", "firstName": "Bob"})
    # Person A on this browser.
    _r, token_a = register_with_cookie(client, occurrence, proxy_key)
    occ2 = make_occurrence(event, days=12)
    resp = post_registration(
        client, payload(occ2, profile={"email": "bob@example.org", "firstName": "Robert", "company": "X"}), proxy_key, cookie=token_a
    )
    assert resp.status_code in (200, 201)
    bob = Person.objects.get(email_key="bob@example.org")
    assert bob.first_name == "Bob"
    assert bob.company == ""
    assert Registration.objects.get(occurrence=occ2).identity_verified is False


def test_blank_fields_do_not_erase_but_new_values_update(client, event, occurrence, proxy_key):
    _resp, token = register_with_cookie(client, occurrence, proxy_key, profile={"company": "Acme"})
    occ2 = make_occurrence(event, days=14)
    resp = post_registration(
        client,
        payload(occ2, replace={"profile": {"email": "", "firstName": "  ", "company": "", "lastName": "Lovelace"}}),
        proxy_key,
        cookie=token,
    )
    assert resp.status_code in (200, 201), resp.content
    person = Person.objects.get()
    assert person.first_name == "Ada"
    assert person.company == "Acme"
    assert person.email.lower().startswith("ada.lovelace")
    assert person.last_name == "Lovelace"
    assert Registration.objects.get(occurrence=occ2).identity_verified is True


def test_reset_revokes_capability_and_expires_cookie(client, occurrence, proxy_key):
    _resp, token = register_with_cookie(client, occurrence, proxy_key)
    client.cookies[COOKIE] = token
    resp = client.post("/api/v1/reset", data="{}", content_type="application/json", headers={"Authorization": f"Bearer {proxy_key}"})
    assert resp.status_code == 200
    assert resp.json() == {"reset": True}
    morsel = resp.cookies[COOKIE]
    assert morsel.value == ""
    assert str(morsel["max-age"]) == "0" or "1970" in morsel["expires"]
    cap = BrowserCapability.objects.get()
    assert cap.revoked_at is not None
    later = get_profile(client, occurrence, proxy_key, cookie=token).json()
    assert later["recognized"] is False


def test_reset_requires_proxy_key(client):
    resp = client.post("/api/v1/reset", data="{}", content_type="application/json")
    assert resp.status_code == 401


def test_revoked_capability_ignored(client, occurrence, proxy_key):
    _resp, token = register_with_cookie(client, occurrence, proxy_key)
    BrowserCapability.objects.update(revoked_at=timezone.now())
    assert get_profile(client, occurrence, proxy_key, cookie=token).json()["recognized"] is False


def test_expired_capability_ignored(client, occurrence, proxy_key):
    _resp, token = register_with_cookie(client, occurrence, proxy_key)
    BrowserCapability.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert get_profile(client, occurrence, proxy_key, cookie=token).json()["recognized"] is False


def test_revoked_capability_does_not_update_profile(client, event, occurrence, proxy_key):
    _resp, token = register_with_cookie(client, occurrence, proxy_key)
    BrowserCapability.objects.update(revoked_at=timezone.now())
    occ2 = make_occurrence(event, days=13)
    post_registration(client, payload(occ2, profile={"company": "Injected"}), proxy_key, cookie=token)
    assert Person.objects.get().company == ""


def test_public_identifiers_cannot_authenticate(client, occurrence, proxy_key):
    _resp, token = register_with_cookie(client, occurrence, proxy_key)
    person = Person.objects.get()
    registration = Registration.objects.get()
    cap = BrowserCapability.objects.get()
    for forged in (str(person.id), person.email, person.email_key, str(registration.id), str(cap.id), cap.token_hash):
        data = get_profile(client, occurrence, proxy_key, cookie=forged).json()
        assert data["recognized"] is False, forged


def test_attribution_identifiers_never_authenticate(client, event, occurrence, proxy_key):
    register_with_cookie(
        client,
        occurrence,
        proxy_key,
        consents={"advertising": {"granted": True}, "analytics": {"granted": True}},
        attribution={"identifiers": {"fbp": "fb.1.1700000000.123456", "gaClientId": "GA1.1.111.222"}},
    )
    occ2 = make_occurrence(event, days=16)
    resp = post_registration(
        client,
        payload(
            occ2,
            profile={"firstName": "Mallory", "company": "Evil"},
            consents={"advertising": {"granted": True}, "analytics": {"granted": True}},
            attribution={"identifiers": {"fbp": "fb.1.1700000000.123456", "gaClientId": "GA1.1.111.222"}},
        ),
        proxy_key,
    )
    _assert_unbound_cookie(resp)
    person = Person.objects.get()
    assert person.first_name == "Ada"
    assert Registration.objects.get(occurrence=occ2).identity_verified is False
