from datetime import timedelta

import pytest
from django.utils import timezone

from tests.conftest import (
    JOIN_URL,
    WAITING_URL,
    cookie_value,
    make_api_key,
    make_event,
    make_occurrence,
    payload,
    post_registration,
    register_with_cookie,
)
from webinars.models import (
    AttributionTouch,
    ConsentRecord,
    IdempotencyRecord,
    Occurrence,
    OrganizationExternalId,
    Person,
    Registration,
)

pytestmark = pytest.mark.django_db


def _assert_success_shape(data, occurrence):
    assert data["status"] == "registered"
    assert data["occurrenceId"] == str(occurrence.id)
    assert data["thankYou"]["mode"] in ("message", "redirect")
    cal = data["calendar"]
    assert cal["uid"] == occurrence.ics_uid
    assert cal["sequence"] == occurrence.ics_sequence
    assert cal["timezone"] == "Europe/London"
    assert cal["startsAt"].endswith("Z") and cal["endsAt"].endswith("Z")
    assert cal["icsUrl"] == f"/api/v1/occurrences/{occurrence.id}/calendar.ics"


def _assert_no_private(resp):
    text = resp.content.decode().lower()
    for secret in (JOIN_URL, WAITING_URL, "ada.lovelace", "example.com\"", "joinurl", "waitingroom"):
        assert secret.lower() not in text


def test_register_created_with_contract_body(client, occurrence, proxy_key):
    resp = post_registration(client, payload(occurrence), proxy_key)
    assert resp.status_code == 201, resp.content
    _assert_success_shape(resp.json(), occurrence)
    _assert_no_private(resp)
    reg = Registration.objects.get()
    assert reg.occurrence_id == occurrence.id
    assert reg.identity_verified is True
    assert reg.attendance == Registration.Attendance.UNKNOWN
    person = reg.person
    assert person.email == "Ada.Lovelace+webinar@example.com"
    assert person.email_key == "ada.lovelace+webinar@example.com"
    assert person.first_name == "Ada"
    assert person.id != reg.id


def test_dedupe_returns_200_with_identical_body(client, occurrence, proxy_key):
    first, token = register_with_cookie(client, occurrence, proxy_key)
    second = post_registration(client, payload(occurrence), proxy_key, cookie=token)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert Registration.objects.count() == 1


def test_idempotent_replay_same_response_single_registration(client, occurrence, proxy_key):
    body = payload(occurrence)
    first = post_registration(client, body, proxy_key)
    second = post_registration(client, body, proxy_key)
    assert first.status_code == 201
    assert second.status_code in (200, 201)
    assert second.json() == first.json()
    assert Registration.objects.count() == 1
    assert IdempotencyRecord.objects.filter(key=body["idempotencyKey"]).count() == 1


def test_idempotency_key_reuse_with_different_body_conflicts(client, occurrence, proxy_key):
    body = payload(occurrence)
    assert post_registration(client, body, proxy_key).status_code == 201
    other = dict(body, profile={"email": "other@example.org", "firstName": "Other"})
    resp = post_registration(client, other, proxy_key)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "idempotency_conflict"
    assert Registration.objects.count() == 1


def test_missing_required_fields_400_with_field_keys(client, occurrence, proxy_key):
    resp = post_registration(client, payload(occurrence, replace={"profile": {"email": "x@example.org"}}), proxy_key)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "validation_error"
    assert "firstName" in err["fields"]
    assert Registration.objects.count() == 0


def test_invalid_email_400(client, occurrence, proxy_key):
    resp = post_registration(client, payload(occurrence, profile={"email": "nope"}), proxy_key)
    assert resp.status_code == 400
    assert "email" in resp.json()["error"]["fields"]


def test_unknown_profile_key_400(client, occurrence, proxy_key):
    resp = post_registration(client, payload(occurrence, profile={"fname": "Ada"}), proxy_key)
    assert resp.status_code == 400
    assert "fname" in resp.json()["error"]["fields"]


def test_form_version_mismatch_400(client, occurrence, proxy_key):
    resp = post_registration(client, payload(occurrence, formVersion="f0"), proxy_key)
    assert resp.status_code == 400
    assert "formVersion" in resp.json()["error"]["fields"]


def test_unknown_top_level_key_400(client, occurrence, proxy_key):
    resp = post_registration(client, payload(occurrence, personId="00000000-0000-0000-0000-000000000000"), proxy_key)
    assert resp.status_code == 400
    assert Registration.objects.count() == 0


def test_wrong_schema_version_400(client, occurrence, proxy_key):
    resp = post_registration(client, payload(occurrence, schemaVersion="2"), proxy_key)
    assert resp.status_code == 400


def test_invalid_json_400(client, occurrence, proxy_key):
    resp = post_registration(client, None, proxy_key, raw="{not json")
    assert resp.status_code == 400


def test_draft_event_not_found(client, form, proxy_key):
    draft = make_event(form, slug="draft-one", visibility="draft")
    occ = make_occurrence(draft)
    resp = post_registration(client, payload(occ), proxy_key)
    assert resp.status_code in (404, 410)
    assert Registration.objects.count() == 0


def test_unknown_occurrence_404(client, occurrence, proxy_key):
    resp = post_registration(client, payload(None), proxy_key)
    assert resp.status_code == 404


def test_cancelled_occurrence_410(client, event, proxy_key):
    occ = make_occurrence(event, status=Occurrence.Status.CANCELLED, cancelled_at=timezone.now())
    resp = post_registration(client, payload(occ), proxy_key)
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "registration_closed"


def test_past_occurrence_410(client, event, proxy_key):
    occ = make_occurrence(event, starts_at=timezone.now() - timedelta(days=2))
    resp = post_registration(client, payload(occ), proxy_key)
    assert resp.status_code == 410


def test_registration_closed_flag_410(client, event, proxy_key):
    occ = make_occurrence(event, registration_open=False)
    assert post_registration(client, payload(occ), proxy_key).status_code == 410


def test_no_api_key_401(client, occurrence):
    resp = post_registration(client, payload(occurrence), None)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_invalid_api_key_401(client, occurrence):
    resp = post_registration(client, payload(occurrence), "wbk_deadbeef_notarealsecret")
    assert resp.status_code == 401


def test_public_read_key_403(client, occurrence):
    key = make_api_key(("public_read",), name="reader")
    resp = post_registration(client, payload(occurrence), key)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert Registration.objects.count() == 0


def test_register_scope_key_allowed(client, occurrence):
    key = make_api_key(("register",), name="cms")
    assert post_registration(client, payload(occurrence), key).status_code == 201


def test_thank_you_redirect_mode(client, form, proxy_key):
    ev = make_event(form, slug="redir", thank_you_mode="redirect", thank_you_url="https://www.example.com/thanks")
    occ = make_occurrence(ev)
    resp = post_registration(client, payload(occ), proxy_key)
    assert resp.status_code == 201
    assert resp.json()["thankYou"] == {"mode": "redirect", "redirectUrl": "https://www.example.com/thanks"}


def test_consents_recorded_as_unverified_client_claims(client, occurrence, proxy_key):
    resp = post_registration(
        client,
        payload(
            occurrence,
            profile={"phone": "+447700900123"},
            consents={
                "sms": {"granted": True, "disclosureVersion": "sms-v1", "serverVerified": True},
                "analytics": {"granted": True, "serverVerified": True},
                "advertising": {"granted": False},
            },
        ),
        proxy_key,
    )
    assert resp.status_code in (201, 400)
    assert not ConsentRecord.objects.filter(server_verified=True).exists()
    if resp.status_code == 201:
        sms = ConsentRecord.objects.get(purpose="sms")
        assert sms.granted is True
        assert sms.disclosure_version == "sms-v1"
        assert sms.phone_snapshot == "+447700900123"
        assert sms.source == ConsentRecord.Source.CLIENT_CLAIM
        assert ConsentRecord.objects.get(purpose="analytics").granted is True


def test_sms_consent_without_phone_400(client, occurrence, proxy_key):
    resp = post_registration(
        client, payload(occurrence, consents={"sms": {"granted": True, "disclosureVersion": "sms-v1"}}), proxy_key
    )
    assert resp.status_code == 400
    assert Registration.objects.count() == 0
    assert ConsentRecord.objects.count() == 0


def test_sms_disclosure_version_mismatch_400(client, occurrence, proxy_key):
    resp = post_registration(
        client,
        payload(occurrence, profile={"phone": "+447700900123"}, consents={"sms": {"granted": True, "disclosureVersion": "sms-v0"}}),
        proxy_key,
    )
    assert resp.status_code == 400
    assert not ConsentRecord.objects.exists()


def test_sms_withdrawal_sets_withdrawn_at(client, event, occurrence, proxy_key):
    _r, token = register_with_cookie(
        client,
        occurrence,
        proxy_key,
        profile={"phone": "+447700900123"},
        consents={"sms": {"granted": True, "disclosureVersion": "sms-v1"}},
    )
    occ2 = make_occurrence(event, days=11)
    resp = post_registration(client, payload(occ2, consents={"sms": {"granted": False}}), proxy_key, cookie=token)
    assert resp.status_code in (200, 201), resp.content
    person = Person.objects.get()
    assert ConsentRecord.objects.filter(person=person, purpose="sms", withdrawn_at__isnull=False).exists()


def test_attribution_cleaned_and_stored(client, occurrence, proxy_key):
    resp = post_registration(
        client,
        payload(
            occurrence,
            consents={"analytics": {"granted": True}},
            attribution={
                "parameters": {
                    "utm_source": "newsletter",
                    "utm_campaign": "q4",
                    "keyword": "{keyword}",
                    "email": "leak@example.org",
                    "gclid": "abc123",
                },
                "identifiers": {"fbp": "fb.1.1700000000.1", "gaClientId": "GA1.1.1.2"},
                "pageUrl": "https://www.example.com/webinars/ai?utm_source=newsletter&email=leak@example.org#form",
                "referrer": "https://news.example.net/path/?token=secret#x",
            },
        ),
        proxy_key,
    )
    assert resp.status_code == 201
    touch = AttributionTouch.objects.get(touch_type="registration_touch")
    assert touch.page_url == "https://www.example.com/webinars/ai"
    assert touch.referrer == "https://news.example.net/path/"
    assert touch.parameters == {"utm_source": "newsletter", "utm_campaign": "q4"}  # gclid needs advertising
    assert touch.identifiers == {"gaClientId": "GA1.1.1.2"}  # fbp needs advertising consent
    assert "leak@example.org" not in str(touch.parameters) + touch.page_url


def test_ad_identifiers_kept_with_advertising_consent(client, occurrence, proxy_key):
    resp = post_registration(
        client,
        payload(
            occurrence,
            consents={"advertising": {"granted": True}},
            attribution={"parameters": {"gclid": "abc123"}, "identifiers": {"_fbp": "fb.1.1700000000.1", "evil": "x"}},
        ),
        proxy_key,
    )
    assert resp.status_code == 201
    touch = AttributionTouch.objects.get(touch_type="registration_touch")
    assert touch.parameters.get("gclid") == "abc123"
    assert touch.identifiers.get("fbp") == "fb.1.1700000000.1"
    assert "evil" not in touch.identifiers


def test_first_touch_once_per_person_registration_touch_per_registration(client, event, occurrence, proxy_key):
    analytics = {"analytics": {"granted": True}}
    _r, token = register_with_cookie(
        client, occurrence, proxy_key, consents=analytics, attribution={"parameters": {"utm_source": "first"}}
    )
    occ2 = make_occurrence(event, days=17)
    resp = post_registration(
        client, payload(occ2, consents=analytics, attribution={"parameters": {"utm_source": "second"}}), proxy_key, cookie=token
    )
    assert resp.status_code == 201
    person = Person.objects.get()
    first = AttributionTouch.objects.filter(person=person, touch_type="first_touch")
    assert first.count() == 1
    assert first.get().parameters == {"utm_source": "first"}
    assert AttributionTouch.objects.filter(person=person, touch_type="registration_touch").count() == 2
    assert set(AttributionTouch.objects.filter(touch_type="registration_touch").values_list("registration", flat=True)) == set(
        Registration.objects.values_list("id", flat=True)
    )


def test_browser_org_ids_stored_unverified(client, occurrence, proxy_key):
    resp = post_registration(
        client, payload(occurrence, organization={"externalIds": [{"namespace": "crm", "value": "ACME-1"}]}), proxy_key
    )
    assert resp.status_code == 201
    org = OrganizationExternalId.objects.get()
    assert (org.namespace, org.value) == ("crm", "ACME-1")
    assert org.verified is False
    assert org.source == OrganizationExternalId.Source.BROWSER


def test_custom_fields_namespaced_on_person(client, occurrence, proxy_key):
    resp = post_registration(client, payload(occurrence, customFields={"role": "Engineer"}), proxy_key)
    assert resp.status_code == 201
    person = Person.objects.get()
    assert person.custom_fields.get("custom.role") == "Engineer"


def test_non_allowlisted_custom_field_400(client, occurrence, proxy_key):
    resp = post_registration(client, payload(occurrence, customFields={"salary": "1"}), proxy_key)
    assert resp.status_code == 400


def test_cookie_not_issued_when_registration_fails(client, occurrence, proxy_key):
    resp = post_registration(client, payload(occurrence, profile={"email": "bad"}), proxy_key)
    assert resp.status_code == 400
    assert cookie_value(resp) is None
    assert Person.objects.count() == 0
