"""Studio configuration of native providers: provider choice, secrets, validation, redaction."""
import json

import pytest
from django.contrib.auth.models import Group
from django.test import Client

from webinars.crypto import decrypt_secret
from webinars.models import Destination

pytestmark = pytest.mark.django_db

PASSWORD = "studio-pass-Quiet-Harbor-7"
SECRET = "phc_TopSecretProjectKey123"


@pytest.fixture(autouse=True)
def vendor_allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", "us.i.posthog.com", "eu.i.posthog.com"]


def _client(django_user_model, role="Administrator"):
    user = django_user_model.objects.create_user(role.lower(), f"{role.lower()}@example.com", PASSWORD)
    user.groups.add(Group.objects.get(name=role))
    user.is_staff = role == "Administrator"
    user.save()
    c = Client()
    c.force_login(user)
    return c


def _form(**over):
    data = {
        "name": "PostHog",
        "provider": "posthog",
        "kind": "generic",
        "url": "",
        "event_types": ["registration.created"],
        "allowed_groups": ["identity", "profile", "organization", "registration", "event"],
        "required_consents": ["analytics"],
        "max_attempts": "8",
        "native-region": "us",
        "native-group_type": "company",
        "native-org_namespace": "crm_account_id",
        "native-processing_ack": "confirmed",
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def _creds(dest):
    dest.refresh_from_db()
    return json.loads(decrypt_secret(dest.credentials_encrypted)) if dest.credentials_encrypted else {}


def _create(c, **over):
    resp = c.post("/studio/integrations/new/", _form(**over))
    assert resp.status_code == 302, (resp.context.get("errors") if resp.context else resp.content[:2000])
    return Destination.objects.get(name=over.get("name", "PostHog"))


@pytest.fixture
def admin(django_user_model):
    return _client(django_user_model)


def test_create_native_stores_provider_without_secrets_and_derives_url(admin):
    dest = _create(admin)
    creds = _creds(dest)
    assert creds["native_provider"] == "posthog" and creds["native_region"] == "us"
    assert "native_api_key" not in creds
    assert dest.url == "https://us.i.posthog.com" and dest.enabled is False


def test_create_enabled_without_secret_is_refused(admin):
    resp = admin.post("/studio/integrations/new/", _form(enabled="on"))
    assert resp.status_code == 400
    assert not Destination.objects.exists()
    assert b"Project API key is required" in resp.content
    # The chosen provider survives the failed submit.
    assert b'<option value="posthog" selected' in resp.content


def test_secret_set_explicitly_never_rendered_and_preview_redacted(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    resp = admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    assert resp.status_code == 302
    assert _creds(dest)["native_api_key"] == SECRET
    page = admin.get(url)
    assert page.status_code == 200
    assert SECRET.encode() not in page.content
    assert b"[redacted]" in page.content and b"/batch/" in page.content


def test_main_form_save_keeps_secret_and_enables_after_validation(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    resp = admin.post(url, _form(enabled="on", url=dest.url))
    assert resp.status_code == 302, resp.content[:2000]
    dest.refresh_from_db()
    assert dest.enabled is True
    assert _creds(dest)["native_api_key"] == SECRET  # not silently erased


def test_blank_secret_submit_keeps_value(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    admin.post(url + "credentials", {"action": "native", "secret-api_key": ""})
    assert _creds(dest)["native_api_key"] == SECRET


def test_enable_without_analytics_consent_refused(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    resp = admin.post(url, _form(enabled="on", required_consents=[]))
    assert resp.status_code == 400
    dest.refresh_from_db()
    assert dest.enabled is False


def test_vendor_host_must_be_operator_allowlisted(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    resp = admin.post("/studio/integrations/new/", _form())
    assert resp.status_code == 400
    assert not Destination.objects.exists()


def test_generic_credentials_form_keeps_provider_settings(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    admin.post(url + "credentials", {"cred-0-name": "extra", "cred-0-value": "v1"})
    creds = _creds(dest)
    assert creds["native_api_key"] == SECRET and creds["native_provider"] == "posthog" and creds["extra"] == "v1"
    resp = admin.post(url + "credentials", {"cred-0-name": "native_api_key", "cred-0-value": "x" * 10})
    assert resp.status_code == 302
    assert _creds(dest)["native_api_key"] == SECRET


def test_switch_to_generic_removes_native_settings_with_notice(admin):
    dest = _create(admin)
    url = f"/studio/integrations/{dest.pk}/"
    admin.post(url + "credentials", {"action": "native", "secret-api_key": SECRET})
    resp = admin.post(url, _form(provider="", url="https://hooks.example.net/x"), follow=True)
    assert resp.status_code == 200
    assert "native_provider" not in _creds(dest)
    assert b"settings and secrets were removed" in resp.content


def test_generic_destination_unchanged(admin):
    resp = admin.post(
        "/studio/integrations/new/",
        _form(name="Hook", provider="", url="https://hooks.example.net/x"),
    )
    assert resp.status_code == 302
    dest = Destination.objects.get(name="Hook")
    assert dest.url == "https://hooks.example.net/x" and dest.credentials_encrypted == ""


def test_analyst_cannot_set_secrets(admin, django_user_model):
    dest = _create(admin)
    analyst = _client(django_user_model, "Analyst")
    resp = analyst.post(
        f"/studio/integrations/{dest.pk}/credentials", {"action": "native", "secret-api_key": SECRET}
    )
    assert resp.status_code in (403, 404)
    assert "native_api_key" not in _creds(dest)


BREVO_KEY = "xkeysib-studioSecretValue456"


def _brevo_dest(**over):
    from webinars.crypto import encrypt_secret

    creds = {
        "native_provider": "brevo",
        "native_region": "global",
        "native_api_key": BREVO_KEY,
        "native_contact_sync": "upsert",
        "native_email_merge_ack": "confirmed",
        "native_contact_attributes": "firstName=FIRSTNAME",
        "native_processing_ack": "confirmed",
        **over,
    }
    return Destination.objects.create(
        name="Brevo",
        url="https://api.brevo.com",
        event_types=["registration.created"],
        allowed_groups=list(Destination.GROUPS),
        required_consents=[],
        credentials_encrypted=encrypt_secret(json.dumps(creds)),
    )


def test_brevo_preview_refusal_shows_bounded_reason(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.brevo.com"]
    dest = _brevo_dest(native_contact_attributes="firstName=FIRSTNAME,lastName=FIRSTNAME")
    page = admin.get(f"/studio/integrations/{dest.pk}/")
    assert page.status_code == 200
    assert b"Preview unavailable (native_config_invalid)" in page.content
    assert BREVO_KEY.encode() not in page.content and b"person@example.com" not in page.content


def test_brevo_valid_preview_has_no_refusal_and_redacts_key(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.brevo.com"]
    dest = _brevo_dest()
    page = admin.get(f"/studio/integrations/{dest.pk}/")
    assert page.status_code == 200
    assert b"Preview unavailable" not in page.content
    assert b"/v3/events" in page.content
    # The template omits request headers entirely; the context must still redact them.
    assert page.context["native_preview"][0]["headers"]["api-key"] == "[redacted]"
    assert BREVO_KEY.encode() not in page.content


# --- module-registered providers (webinars/native_providers/): the form is schema-driven ---------

AC_KEY = "ac-studioTokenValue-789"
MC_KEY = "mailchimp-test-key-not-real-0123456789-us19"
BH_KEY = "beehiiv-test-key-not-real-0123456789abcdef"
BH_PUB = "pub_0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_create_activecampaign_derives_url_from_typed_host(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["acme.api-us1.com"]
    dest = _create(
        admin,
        name="ActiveCampaign",
        provider="activecampaign",
        required_consents=[],
        **{
            "native-region": "account",
            "native-api_host": "acme.api-us1.com",
            "native-email_merge_ack": "confirmed",
            "native-field_map": "company=12",
            "native-tag_registered": "20",
            "native-group_type": None,
            "native-org_namespace": None,
        },
    )
    creds = _creds(dest)
    assert creds["native_provider"] == "activecampaign" and creds["native_api_host"] == "acme.api-us1.com"
    assert "native_api_key" not in creds and dest.url == "https://acme.api-us1.com" and dest.enabled is False


def test_create_mailchimp_derives_url_from_datacenter(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["us19.api.mailchimp.com"]
    dest = _create(
        admin,
        name="Mailchimp",
        provider="mailchimp",
        required_consents=[],
        **{
            "native-region": "datacenter",
            "native-datacenter": "us19",
            "native-audience_id": "a1b2c3d4e5",
            "native-email_merge_ack": "confirmed",
            "native-group_type": None,
            "native-org_namespace": None,
        },
    )
    creds = _creds(dest)
    assert creds["native_provider"] == "mailchimp" and creds["native_datacenter"] == "us19"
    assert "native_api_key" not in creds and dest.url == "https://us19.api.mailchimp.com"


def test_create_beehiiv_requires_subscription_acknowledgement(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.beehiiv.com"]
    fields = {
        "native-region": "global",
        "native-publication_id": BH_PUB,
        "native-subscribe_ack": "confirmed",
        "native-email_merge_ack": "confirmed",
        "native-tag_registered": "webinar registered",
        "native-group_type": None,
        "native-org_namespace": None,
    }
    dest = _create(admin, name="beehiiv", provider="beehiiv", required_consents=[], **fields)
    creds = _creds(dest)
    assert creds["native_provider"] == "beehiiv" and creds["native_publication_id"] == BH_PUB
    assert "native_api_key" not in creds and dest.url == "https://api.beehiiv.com" and dest.enabled is False
    # Without the subscription acknowledgement the destination can't be enabled, even with the key set.
    unacked = _module_dest("beehiiv unacked", "api.beehiiv.com", {**creds, "native_api_key": BH_KEY})
    form = _form(name="beehiiv unacked", provider="beehiiv", enabled="on", required_consents=[], **{**fields, "native-subscribe_ack": ""})
    resp = admin.post(f"/studio/integrations/{unacked.pk}/", form)
    assert resp.status_code == 400 and b"subscribes registrants" in resp.content
    unacked.refresh_from_db()
    assert unacked.enabled is False and BH_KEY.encode() not in resp.content


def _module_dest(name, host, creds):
    from webinars.crypto import encrypt_secret

    return Destination.objects.create(
        name=name,
        url=f"https://{host}",
        event_types=["registration.created"],
        allowed_groups=list(Destination.GROUPS),
        required_consents=[],
        credentials_encrypted=encrypt_secret(json.dumps(creds)),
    )


def test_activecampaign_edit_page_previews_without_secret(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["acme.api-us1.com"]
    creds = {
        "native_provider": "activecampaign", "native_region": "account", "native_api_host": "acme.api-us1.com",
        "native_api_key": AC_KEY, "native_email_merge_ack": "confirmed", "native_tag_registered": "20", "native_processing_ack": "confirmed",
    }
    dest = _module_dest("ActiveCampaign", "acme.api-us1.com", creds)
    page = admin.get(f"/studio/integrations/{dest.pk}/")
    assert page.status_code == 200
    assert b"Preview unavailable" not in page.content
    assert b"/api/3/contact/sync" in page.content and b"/api/3/contactTags" in page.content
    assert page.context["native_preview"][0]["headers"]["Api-Token"] == "[redacted]"
    assert AC_KEY.encode() not in page.content


def test_mailchimp_edit_page_previews_without_secret_and_refuses_pending_without_ack(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["us19.api.mailchimp.com"]
    creds = {
        "native_provider": "mailchimp", "native_region": "datacenter", "native_datacenter": "us19", "native_api_key": MC_KEY,
        "native_audience_id": "a1b2c3d4e5", "native_email_merge_ack": "confirmed", "native_processing_ack": "confirmed",
    }
    dest = _module_dest("Mailchimp", "us19.api.mailchimp.com", creds)
    page = admin.get(f"/studio/integrations/{dest.pk}/")
    assert page.status_code == 200
    assert b"Preview unavailable" not in page.content and b"/members/" in page.content and b"/events" in page.content
    assert page.context["native_preview"][0]["headers"]["Authorization"] == "[redacted]"
    assert MC_KEY.encode() not in page.content and b"us19" in page.content
    pending = _module_dest("Mailchimp pending", "us19.api.mailchimp.com", {**creds, "native_status_if_new": "pending"})
    page = admin.get(f"/studio/integrations/{pending.pk}/")
    assert page.status_code == 200
    assert b"Preview unavailable (native_match_strategy_unconfirmed)" in page.content
    assert MC_KEY.encode() not in page.content


def test_beehiiv_edit_page_previews_without_secret_and_refuses_optout_without_ack(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.beehiiv.com"]
    creds = {
        "native_provider": "beehiiv", "native_region": "global", "native_api_key": BH_KEY, "native_publication_id": BH_PUB,
        "native_subscribe_ack": "confirmed", "native_email_merge_ack": "confirmed", "native_tag_registered": "webinar registered",
        "native_processing_ack": "confirmed",
    }
    dest = _module_dest("beehiiv", "api.beehiiv.com", creds)
    page = admin.get(f"/studio/integrations/{dest.pk}/")
    assert page.status_code == 200
    assert b"Preview unavailable" not in page.content
    assert f"/v2/publications/{BH_PUB}/subscriptions".encode() in page.content and b"/tags" in page.content
    assert page.context["native_preview"][0]["headers"]["Authorization"] == "[redacted]"
    assert BH_KEY.encode() not in page.content
    assert b"registrants are subscribed to this publication" in page.content.lower()
    off = _module_dest("beehiiv opt-out", "api.beehiiv.com", {**creds, "native_double_opt_override": "off"})
    page = admin.get(f"/studio/integrations/{off.pk}/")
    assert page.status_code == 200
    assert b"Preview unavailable (native_match_strategy_unconfirmed)" in page.content
    assert BH_KEY.encode() not in page.content


# --- transactional email providers (Resend, Amazon SES): per-provider event types, templates ---------

RESEND_KEY = "re_StudioTestKeyValue_0000000000"
SES_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
EMAIL_FORM = {
    "required_consents": [],
    "native-region": "global",
    "native-from_address": "events@example.com",
    "native-from_name": "Example Events",
    "native-send_ack": "confirmed",
    "native-group_type": None,
    "native-org_namespace": None,
}


def test_create_resend_derives_url_and_offers_its_own_event_types(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.resend.com"]
    dest = _create(admin, name="Resend", provider="resend", event_types=["registration.created", "reminder.due"], **EMAIL_FORM)
    creds = _creds(dest)
    assert creds["native_provider"] == "resend" and creds["native_from_address"] == "events@example.com"
    assert "native_api_key" not in creds and dest.url == "https://api.resend.com" and dest.enabled is False
    assert dest.event_types == ["registration.created", "reminder.due"]
    page = admin.get(f"/studio/integrations/{dest.pk}/")
    assert page.status_code == 200
    offered = [o["value"] for o in page.context["event_type_options"]]
    assert set(offered) == set(page.context["native_event_types"]) and len(offered) == 7
    assert "reminder.due" in offered and "occurrence.cancelled" in offered and "attendance.checked_in" not in offered
    assert b'<textarea class="input" id="native-body_registration_created"' in page.content
    assert b"Events this provider supports" in page.content


def test_create_ses_derives_url_from_region(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["email.eu-west-2.amazonaws.com"]
    dest = _create(admin, name="SES", provider="ses", **{**EMAIL_FORM, "native-region": "eu-west-2", "native-configuration_set": "webinars"})
    creds = _creds(dest)
    assert creds["native_provider"] == "ses" and creds["native_region"] == "eu-west-2" and creds["native_configuration_set"] == "webinars"
    assert "native_access_key_id" not in creds and "native_secret_access_key" not in creds
    assert dest.url == "https://email.eu-west-2.amazonaws.com"


def test_other_providers_keep_the_default_event_types(admin):
    dest = _create(admin)
    page = admin.get(f"/studio/integrations/{dest.pk}/")
    offered = [o["value"] for o in page.context["event_type_options"]]
    assert offered == ["registration.created", "registration.updated", "attendance.recorded", "attendance.checked_in"]
    assert tuple(page.context["native_event_types"]) == ("registration.created", "registration.updated", "attendance.checked_in", "attendance.recorded")
    generic = admin.get("/studio/integrations/new/")
    assert len(generic.context["event_type_options"]) == 9  # generic webhooks offer every outbox event


def test_enable_email_destination_refuses_unsupported_event_and_bad_template(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.resend.com"]
    dest = _module_dest("Resend", "api.resend.com", {
        "native_provider": "resend", "native_region": "global", "native_api_key": RESEND_KEY, "native_from_address": "events@example.com",
        "native_send_ack": "confirmed", "native_processing_ack": "confirmed",
    })
    page = admin.get(f"/studio/integrations/{dest.pk}/")
    assert page.status_code == 200 and b"Preview unavailable" not in page.content
    assert b"You&#x27;re registered: Sample webinar" in page.content and RESEND_KEY.encode() not in page.content
    assert page.context["native_preview"][0]["headers"]["Authorization"] == "[redacted]"
    resp = admin.post(
        f"/studio/integrations/{dest.pk}/",
        _form(name="Resend", provider="resend", enabled="on", event_types=["attendance.checked_in"], **{**EMAIL_FORM, "native-body_reminder_due": "Hello {nope}"}),
    )
    assert resp.status_code == 400
    assert "event_types" in resp.context["errors"] and "native-body_reminder_due" in resp.context["errors"]
    assert "Unknown placeholder" in resp.context["errors"]["native-body_reminder_due"][0]
    dest.refresh_from_db()
    assert dest.enabled is False


def test_email_template_overrides_round_trip_through_the_form(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["api.resend.com"]
    body_text = "Hi {first_name},\r\n\r\nSee you at {event_title}.\r\n\r\n{join_link}"
    dest = _create(admin, name="Resend", provider="resend", **{**EMAIL_FORM, "native-body_registration_created": body_text, "native-subject_registration_created": "See you, {first_name}"})
    creds = _creds(dest)
    assert creds["native_subject_registration_created"] == "See you, {first_name}"
    assert creds["native_body_registration_created"] == body_text
    page = admin.get(f"/studio/integrations/{dest.pk}/")
    assert b"See you at {event_title}." in page.content


def test_ses_edit_page_previews_without_secrets(admin, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["email.eu-west-2.amazonaws.com"]
    dest = _module_dest("SES", "email.eu-west-2.amazonaws.com", {
        "native_provider": "ses", "native_region": "eu-west-2", "native_access_key_id": "AKIAIOSFODNN7EXAMPLE", "native_secret_access_key": SES_SECRET,
        "native_from_address": "events@example.com", "native_send_ack": "confirmed", "native_processing_ack": "confirmed",
    })
    page = admin.get(f"/studio/integrations/{dest.pk}/")
    assert page.status_code == 200 and b"Preview unavailable" not in page.content
    assert b"/v2/email/outbound-emails" in page.content
    assert page.context["native_preview"][0]["headers"]["Authorization"] == "[redacted]"
    assert SES_SECRET.encode() not in page.content and b"AKIAIOSFODNN7EXAMPLE" not in page.content and b"Signature=" not in page.content
