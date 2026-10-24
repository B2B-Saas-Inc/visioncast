import json

import pytest
from django.core.management import call_command

from tests.conftest import make_destination, payload, post_registration, register_with_cookie
from webinars.models import (
    AttributionTouch,
    AuditLog,
    BrowserCapability,
    ConsentRecord,
    OrganizationExternalId,
    OutboxMessage,
    Person,
    Registration,
)

pytestmark = pytest.mark.django_db

PII = ("ada.lovelace", "Lovelace", "+447700900123", "Acme Secret Co")


def _seed(client, occurrence, proxy_key):
    make_destination(allowed_groups=list(("identity", "profile", "organization", "consents", "attribution", "registration", "event")))
    _resp, token = register_with_cookie(
        client,
        occurrence,
        proxy_key,
        profile={"lastName": "Lovelace", "company": "Acme Secret Co", "phone": "+447700900123"},
        organization={"externalIds": [{"namespace": "crm", "value": "ACME-77"}]},
        consents={"sms": {"granted": True, "disclosureVersion": "sms-v1"}, "advertising": {"granted": True}},
        attribution={"parameters": {"utm_source": "x"}, "identifiers": {"fbp": "fb.1.1.2"}, "pageUrl": "https://www.example.com/p"},
    )
    return Person.objects.get(), token


def test_purge_removes_personal_data(client, occurrence, proxy_key):
    person, token = _seed(client, occurrence, proxy_key)
    assert OutboxMessage.objects.exists()
    assert AttributionTouch.objects.exists()
    assert OrganizationExternalId.objects.exists()

    call_command("purge_personal_data", person=str(person.id))

    # Person row may be anonymised in place or deleted entirely.
    remaining = Person.objects.filter(pk=person.pk).first()
    if remaining is not None:
        assert remaining.email == ""
        assert remaining.email_key is None
        assert remaining.first_name == remaining.last_name == remaining.phone == remaining.company == ""
        assert remaining.custom_fields in ({}, None)
        assert remaining.erased_at is not None
    assert not AttributionTouch.objects.filter(person_id=person.pk).exists()
    assert not OrganizationExternalId.objects.filter(person_id=person.pk).exists()
    assert not BrowserCapability.objects.filter(person_id=person.pk, revoked_at__isnull=True).exists()
    for reg in Registration.objects.all():
        assert reg.submitted_profile in ({}, None)
    for cr in ConsentRecord.objects.all():
        assert cr.phone_snapshot == ""

    blob = json.dumps(list(OutboxMessage.objects.values_list("payload", flat=True)))
    blob += json.dumps(list(AuditLog.objects.values_list("metadata", flat=True)))
    for value in PII:
        assert value.lower() not in blob.lower(), value


def test_purged_cookie_no_longer_recognized(client, occurrence, proxy_key):
    from tests.conftest import get_profile

    person, token = _seed(client, occurrence, proxy_key)
    call_command("purge_personal_data", person=str(person.id))
    assert get_profile(client, occurrence, proxy_key, cookie=token).json()["recognized"] is False


def test_purge_leaves_other_people_intact(client, occurrence, proxy_key):
    person, _ = _seed(client, occurrence, proxy_key)
    other_resp = post_registration(
        client, payload(occurrence, profile={"email": "keep@example.org", "firstName": "Keep"}), proxy_key
    )
    assert other_resp.status_code == 201
    call_command("purge_personal_data", person=str(person.id))
    keep = Person.objects.get(email_key="keep@example.org")
    assert keep.first_name == "Keep"
    assert AttributionTouch.objects.filter(person=keep).exists()
