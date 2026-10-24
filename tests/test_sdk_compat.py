"""Compatibility with the browser SDK / proxy in sdk/ (cookie names, CSRF, error shape, identifiers)."""
import json

import pytest

from tests.conftest import (
    COOKIE,
    ORIGIN,
    get_profile,
    make_person,
    payload,
    post_registration,
    register_with_cookie,
)

CSRF_COOKIE = "__Host-webinar_csrf"
CSRF_TOKEN = "A" * 43

pytestmark = pytest.mark.django_db


def _direct_post(client, body, **headers):
    client.cookies[CSRF_COOKIE] = CSRF_TOKEN
    base = {"Origin": ORIGIN, "X-Webinar-CSRF": CSRF_TOKEN}
    base.update(headers)
    return client.post("/webinar-api/registrations", data=json.dumps(body), content_type="application/json", headers=base)


def test_default_capability_cookie_matches_proxy_session_cookie(settings):
    from webinars import identity

    # The proxy (sdk/proxy/server.js) forwards only PROXY_SESSION_COOKIE, default "webinar_session".
    del settings.CAPABILITY_COOKIE_NAME  # pytest-django restores it after the test
    assert identity._cookie_name() == "webinar_session"


def test_direct_post_accepts_sdk_double_submit_token(client, occurrence):
    resp = _direct_post(client, payload(occurrence))
    assert resp.status_code == 201, resp.content


def test_direct_post_rejects_mismatched_double_submit_token(client, occurrence):
    client.cookies[CSRF_COOKIE] = CSRF_TOKEN
    resp = client.post(
        "/webinar-api/registrations",
        data=json.dumps(payload(occurrence)),
        content_type="application/json",
        headers={"Origin": ORIGIN, "X-Webinar-CSRF": "B" * 43},
    )
    assert resp.status_code == 403


def test_direct_profile_issues_csrf_cookie(client, occurrence):
    resp = client.get(
        "/webinar-api/profile",
        {"occurrenceId": str(occurrence.id), "formVersion": "f1"},
        headers={"Origin": ORIGIN},
    )
    assert resp.status_code == 200
    morsel = resp.cookies.get(CSRF_COOKIE)
    assert morsel is not None and morsel.value
    assert morsel["secure"]


def test_field_errors_mirrored_for_sdk(client, occurrence, proxy_key):
    body = payload(occurrence, replace={"profile": {"email": "not-an-email", "firstName": "Ada"}})
    resp = post_registration(client, body, proxy_key)
    assert resp.status_code == 400
    data = resp.json()
    assert "email" in data["fieldErrors"]
    assert data["fieldErrors"] == data["error"]["fields"]


def test_ga4_client_id_alias_accepted_with_analytics_consent(client, occurrence, proxy_key):
    from webinars.models import AttributionTouch

    body = payload(
        occurrence,
        consents={"analytics": {"granted": True}},
        attribution={"identifiers": {"ga4ClientId": "1234567890.1700000000"}},
    )
    resp = post_registration(client, body, proxy_key)
    assert resp.status_code == 201, resp.content
    touch = AttributionTouch.objects.get(touch_type="registration_touch")
    assert touch.identifiers == {"gaClientId": "1234567890.1700000000"}


def test_unverified_submission_cannot_grant_consent_for_existing_person(client, occurrence, proxy_key):
    from webinars.models import ConsentRecord, Registration

    victim = make_person(email="victim@example.com", first_name="Vic")
    body = payload(
        occurrence,
        replace={"profile": {"email": "victim@example.com", "firstName": "Mallory"}},
        consents={"advertising": {"granted": True}, "analytics": {"granted": True}},
    )
    resp = post_registration(client, body, proxy_key)
    assert resp.status_code == 201
    assert not ConsentRecord.objects.filter(person=victim, granted=True).exists()
    reg = Registration.objects.get(person=victim)
    assert reg.identity_verified is False
    assert reg.submitted_profile["consentClaims"]["advertising"]["granted"] is True


def test_capability_holder_can_grant_consent(client, occurrence, proxy_key):
    from webinars.models import ConsentRecord

    _resp, _token = register_with_cookie(client, occurrence, proxy_key, consents={"advertising": {"granted": True}})
    assert ConsentRecord.objects.filter(purpose="advertising", granted=True, server_verified=False).exists()


def test_profile_never_contains_pii_values(client, occurrence, proxy_key):
    _resp, token = register_with_cookie(client, occurrence, proxy_key)
    body = get_profile(client, occurrence, proxy_key, cookie=token).content.decode()
    assert "Ada" not in body and "example.com" not in body.lower()
    assert set(json.loads(body)) == {"recognized", "missingFields", "formVersion"}
    client.cookies.pop(COOKIE, None)
