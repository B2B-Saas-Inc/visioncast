import json

import pytest

from tests.conftest import COOKIE, ORIGIN, cookie_value, payload
from webinars.models import BrowserCapability, Registration

pytestmark = pytest.mark.django_db

PATH = "/webinar-api/registrations"


def _post(client, body, content_type="application/json", **headers):
    base = {"Origin": ORIGIN, "X-Webinar-Request": "1", "Sec-Fetch-Site": "same-origin"}
    base.update(headers)
    base = {k: v for k, v in base.items() if v is not None}
    data = json.dumps(body) if not isinstance(body, str) else body
    return client.post(PATH, data=data, content_type=content_type, headers=base)


def test_valid_same_origin_registration(client, occurrence):
    resp = _post(client, payload(occurrence))
    assert resp.status_code == 201, resp.content
    assert Registration.objects.count() == 1
    assert cookie_value(resp)


def test_missing_origin_rejected(client, occurrence):
    resp = _post(client, payload(occurrence), Origin=None)
    assert resp.status_code == 403
    assert Registration.objects.count() == 0


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example.org", "http://www.example.com", "https://www.example.com.evil.org", "https://example.com", "null"],
)
def test_other_origin_rejected(client, occurrence, origin):
    resp = _post(client, payload(occurrence), Origin=origin)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "origin_rejected"
    assert Registration.objects.count() == 0


def test_missing_custom_header_rejected(client, occurrence):
    resp = _post(client, payload(occurrence), **{"X-Webinar-Request": None})
    assert resp.status_code == 403
    assert Registration.objects.count() == 0


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded"])
def test_non_json_content_type_rejected(client, occurrence, ctype):
    resp = _post(client, json.dumps(payload(occurrence)), content_type=ctype)
    assert resp.status_code in (400, 403, 415)
    assert Registration.objects.count() == 0


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_fetch_rejected(client, occurrence, site):
    resp = _post(client, payload(occurrence), **{"Sec-Fetch-Site": site})
    assert resp.status_code == 403
    assert Registration.objects.count() == 0


def test_direct_profile_get(client, occurrence):
    resp = client.get(
        "/webinar-api/profile",
        {"occurrenceId": str(occurrence.id), "formVersion": "f1"},
        headers={"X-Webinar-Request": "1", "Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["recognized"] is False
    assert "email" in data["missingFields"]


def test_direct_profile_recognizes_cookie_from_direct_registration(client, occurrence):
    resp = _post(client, payload(occurrence))
    token = cookie_value(resp)
    client.cookies[COOKIE] = token
    prof = client.get(
        "/webinar-api/profile",
        {"occurrenceId": str(occurrence.id), "formVersion": "f1"},
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
    )
    assert prof.status_code == 200
    assert prof.json()["recognized"] is True


def test_direct_profile_cross_origin_rejected(client, occurrence):
    resp = client.get(
        "/webinar-api/profile",
        {"occurrenceId": str(occurrence.id), "formVersion": "f1"},
        headers={"Origin": "https://evil.example.org", "Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403


def test_direct_reset_requires_origin_and_revokes(client, occurrence):
    token = cookie_value(_post(client, payload(occurrence)))
    client.cookies[COOKIE] = token
    bad = client.post("/webinar-api/reset", data="{}", content_type="application/json", headers={"Origin": "https://evil.example.org", "X-Webinar-Request": "1"})
    assert bad.status_code == 403
    assert BrowserCapability.objects.get().revoked_at is None
    client.cookies[COOKIE] = token
    ok = client.post("/webinar-api/reset", data="{}", content_type="application/json", headers={"Origin": ORIGIN, "X-Webinar-Request": "1"})
    assert ok.status_code == 200
    assert BrowserCapability.objects.get().revoked_at is not None


def test_direct_registration_org_ids_never_trusted(client, occurrence):
    from webinars.models import OrganizationExternalId

    resp = _post(client, payload(occurrence, organization={"externalIds": [{"namespace": "crm", "value": "X-1"}]}))
    assert resp.status_code == 201
    assert not OrganizationExternalId.objects.filter(verified=True).exists()
