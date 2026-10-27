"""beehiiv native adapter: mocked subscription/tag contracts, acknowledgements, consent and redaction.

No network: transports are fakes and socket connects raise. Request shapes follow the official
references fetched 2026-10-27 (docs/native-integrations.md); nothing here has been checked
against a real beehiiv publication.
"""
import json
import socket

import pytest

import webinars.delivery
import webinars.ssrf
from tests.test_native_integrations import Fake, _claimed, body
from webinars import native_integrations as ni
from webinars.models import Destination, OutboxMessage
from webinars.native_providers import beehiiv as bh

pytestmark = pytest.mark.django_db

HOST = "api.beehiiv.com"
KEY = "bh-test-key-not-real-0123456789abcdef"
PUB = "pub_0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SUB = "sub_11111111-2222-4333-8444-555555555555"
SUBS_URL = f"https://{HOST}/v2/publications/{PUB}/subscriptions"
TAGS_URL = f"{SUBS_URL}/{SUB}/tags"
BH = {
    "native_provider": "beehiiv",
    "native_region": "global",
    "native_api_key": KEY,
    "native_publication_id": PUB,
    "native_subscribe_ack": "confirmed",
    "native_email_merge_ack": "confirmed",
    "native_custom_field_map": "firstName=First Name,lastName=Last Name,company=Company,jobTitle=Job Title,webinar_event_title=Last Webinar",
    "native_tag_registered": "webinar registered",
    "native_tag_attended": "webinar attended",
    "native_tag_no_show": "webinar no-show",
    "native_processing_ack": "confirmed",
}
NO_TAGS = {k: v for k, v in BH.items() if not k.startswith("native_tag_")}
KW = dict(event_types=["registration.created"], allowed_groups=list(Destination.GROUPS), required_consents=[])
S = OutboxMessage.Status
CREATED = (200, {"data": {"id": SUB, "email": "person@example.com", "status": "active"}})
BEARER = f"Bearer {KEY}"
FORBIDDEN_KEYS = {
    "tier", "premium_tiers", "premium_tier_ids", "stripe_customer_id", "referring_site", "referral_code", "automation_ids",
    "newsletter_list_ids", "skip_newsletter_list_auto_subscribe", "complimentary_gift_id", "utm_term", "utm_content", "unsubscribe", "status", "tags",
}
BASE_SUBSCRIPTION = {"email": "person@example.com", "reactivate_existing": False, "send_welcome_email": False}
SAMPLE_FIELDS = [
    {"name": "First Name", "value": "Sample"},
    {"name": "Last Name", "value": "Person"},
    {"name": "Company", "value": "Example Co"},
    {"name": "Job Title", "value": "Role"},
    {"name": "Last Webinar", "value": "Sample webinar"},
]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net", HOST, "api.segment.io"]


def run(creds=BH, b=None, event_type="registration.created", fake=None):
    fake = fake or Fake(responses=[CREATED, (200, {"data": {"id": SUB}})])
    return ni.execute("beehiiv", creds, b if b is not None else body(), event_type, fake.ctx()), fake


def dest(**kw):
    return Destination(**{**KW, **kw})


# --- schema / credentials -----------------------------------------------------


def test_offered_and_schema_driven():
    assert dict(ni.PROVIDER_CHOICES)["beehiiv"] == "beehiiv"
    assert ni.FIELDS["beehiiv"][0].name == "region" and ni.FIELDS["beehiiv"][0].choices == (("global", HOST),)
    assert ni.PROCESSING_ACK in ni.FIELDS["beehiiv"]
    assert ni.HOSTS["beehiiv"] == {"global": HOST} and HOST in ni.VENDOR_HOSTS
    assert "beehiiv" in ni.NEEDS_REQUEST_TRANSPORT and "beehiiv" not in ni.NEEDS_DESTINATION_HOSTS
    assert "beehiiv" not in ni.ANALYTICS_PROVIDERS and "beehiiv" not in ni.CUSTOM_ID_FIELD and "beehiiv" not in ni.ORG_TARGETS
    assert ni.REGISTRY["beehiiv"] is bh
    names = [f.name for f in ni.FIELDS["beehiiv"]]
    assert {"subscribe_ack", "email_merge_ack", "double_opt_override", "optin_ack", "reactivate_existing", "send_welcome_email", "utm_source"} <= set(names)
    double = next(f for f in ni.FIELDS["beehiiv"] if f.name == "double_opt_override")
    assert [c for c, _ in double.choices] == ["not_set", "on", "off"]
    for name in ("send_welcome_email", "reactivate_existing"):
        field = next(f for f in ni.FIELDS["beehiiv"] if f.name == name)
        assert [c for c, _ in field.choices] == ["false", "true"] and not field.required
    assert "subscribed" in next(f for f in ni.FIELDS["beehiiv"] if f.name == "subscribe_ack").help


def test_credential_is_set_only():
    rendered = repr(ni.config_of(BH, "beehiiv")) + repr(ni.secrets_set(BH, "beehiiv"))
    assert KEY not in rendered and ni.secrets_set(BH, "beehiiv") == {"api_key": True}
    posted = {f.name: ni.config_of(BH, "beehiiv").get(f.name, "") for f in ni.FIELDS["beehiiv"]}
    posted["api_key"] = ""
    out, _ = ni.merge_credentials(BH, "beehiiv", posted)
    assert out["native_api_key"] == KEY  # blank keeps the stored value
    out, notes = ni.merge_credentials(BH, "segment", {"region": "us", "write_key": ""})
    assert KEY not in out.values() and notes


def test_valid_config():
    assert ni.validate("beehiiv", BH, **KW) == {}
    assert ni.validate("beehiiv", NO_TAGS, **KW) == {}
    assert ni.validate("beehiiv", {**BH, "native_double_opt_override": "on"}, **KW) == {}
    assert ni.validate("beehiiv", {**BH, "native_double_opt_override": "off", "native_optin_ack": "confirmed"}, **KW) == {}
    assert ni.validate("beehiiv", {**BH, "native_utm_source": "visioncast", "native_send_welcome_email": "true", "native_reactivate_existing": "true"}, **KW) == {}
    assert ni.check_send_policy("beehiiv", BH, dest()) is None


@pytest.mark.parametrize("key", ["native_processing_ack", "native_subscribe_ack", "native_email_merge_ack", "native_api_key", "native_publication_id"])
def test_required_fields(key):
    creds = {k: v for k, v in BH.items() if k != key}
    assert f"native-{key[len('native_'):]}" in ni.validate("beehiiv", creds, **KW)
    assert ni.check_send_policy("beehiiv", creds, dest()) == "native_config_invalid"


@pytest.mark.parametrize("key", ["native_subscribe_ack", "native_email_merge_ack"])
@pytest.mark.parametrize("value", ["not-confirmed", "yes", ""])
def test_acknowledgements_must_be_confirmed(key, value):
    creds = {**BH, key: value}
    assert f"native-{key[len('native_'):]}" in ni.validate("beehiiv", creds, **KW)
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_match_strategy_unconfirmed", [])


def test_double_opt_off_needs_optin_ack_and_unknown_values_refused():
    creds = {**BH, "native_double_opt_override": "off"}
    assert "native-optin_ack" in ni.validate("beehiiv", creds, **KW)
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_match_strategy_unconfirmed", [])
    # "on" sends beehiiv's own confirmation email; it needs no extra acknowledgement.
    assert "native-optin_ack" not in ni.validate("beehiiv", {**BH, "native_double_opt_override": "on"}, **KW)
    for value in ("true", "yes", "ON", "disabled"):
        creds = {**BH, "native_double_opt_override": value, "native_optin_ack": "confirmed"}
        assert "native-double_opt_override" in ni.validate("beehiiv", creds, **KW)
        outcome, fake = run(creds)
        assert (outcome.kind, fake.calls) == ("dead", [])


@pytest.mark.parametrize("pub", ["pub_123", "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "sub_0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "pub_0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d/x", "pub_", ""])
def test_publication_id_must_be_prefixed_uuid(pub):
    creds = {**BH, "native_publication_id": pub}
    assert "native-publication_id" in ni.validate("beehiiv", creds, **KW)
    outcome, fake = run(creds)
    assert outcome.kind == "dead" and fake.calls == []


@pytest.mark.parametrize(
    "mapping",
    [
        "email=Email",
        "phone=Phone",
        "customField=X",
        "webinar_event_type=Type",  # not a beehiiv source
        "firstName=A,firstName=B",
        "firstName=Name,lastName=Name",
        "firstName=Name,lastName=name",  # beehiiv field names compared case-insensitively
        "firstName=A;lastName=B",
        "firstName= Spaced",
        "firstName=Trailing ",
        "firstName=a=b",
    ],
)
def test_custom_field_map_allowlist(mapping):
    creds = {**BH, "native_custom_field_map": mapping}
    assert "native-custom_field_map" in ni.validate("beehiiv", creds, **KW)
    outcome, fake = run(creds)
    assert outcome.kind == "dead" and fake.calls == []


@pytest.mark.parametrize("value", ["bad source", "a/b", "x?y=1", "=x", "", "a" * 101])
def test_utm_source_pattern(value):
    creds = {**BH, "native_utm_source": value}
    if value:
        assert "native-utm_source" in ni.validate("beehiiv", creds, **KW)
        assert run(creds)[1].calls == []
    else:
        assert ni.validate("beehiiv", creds, **KW) == {}


@pytest.mark.parametrize("value", ["yes", "1", "True", "on"])
@pytest.mark.parametrize("key", ["native_send_welcome_email", "native_reactivate_existing"])
def test_boolean_choices_are_strict(key, value):
    creds = {**BH, key: value}
    assert f"native-{key[len('native_'):]}" in ni.validate("beehiiv", creds, **KW)
    outcome, fake = run(creds)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_invalid", [])


# --- host policy (fail closed) ---------------------------------------------------


def test_host_needs_operator_entry(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    errors = ni.validate("beehiiv", BH, **KW)
    assert any(HOST in m for m in errors.get("provider", []))
    assert ni.check_send_policy("beehiiv", BH, dest()) == "native_config_invalid"


def test_allowed_hosts_pins_exact_host():
    assert ni.allowed_hosts("beehiiv", BH) == frozenset({HOST})
    assert ni.base_url("beehiiv", BH) == f"https://{HOST}"
    for region in ("", "us", "eu", "dataplane"):
        assert ni.allowed_hosts("beehiiv", {**BH, "native_region": region}) == frozenset()
        outcome, fake = run({**BH, "native_region": region})
        assert (outcome.kind, outcome.error, fake.calls) == ("dead", "native_config_incomplete", [])


def test_without_request_transport_unsendable(monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    assert any("response-reading transport" in m for m in ni.validate("beehiiv", BH, **KW)["provider"])
    assert ni.check_send_policy("beehiiv", BH, dest()) == "native_transport_unavailable"


# --- request contract ---------------------------------------------------------


def test_exact_requests():
    outcome, fake = run()
    assert outcome.kind == "delivered"
    subscription, tags = fake.calls
    for call in fake.calls:
        assert call["headers"]["Authorization"] == BEARER and call["headers"]["Content-Type"].startswith("application/json")
        assert call["method"] == "POST"
    assert subscription["url"] == SUBS_URL
    assert subscription["json"] == {**BASE_SUBSCRIPTION, "custom_fields": SAMPLE_FIELDS}
    assert tags["url"] == TAGS_URL and tags["json"] == {"tags": ["webinar registered"]}


def test_subscription_id_from_response_is_path_of_tag_call():
    other = "sub_99999999-8888-4777-8666-555555555555"
    fake = Fake(responses=[(200, {"data": {"id": other}}), (200, {})])
    outcome, fake = run(fake=fake)
    assert outcome.kind == "delivered" and fake.calls[1]["url"] == f"{SUBS_URL}/{other}/tags"


@pytest.mark.parametrize("response", [(200, {}), (200, {"data": {"id": ""}}), (200, {"data": {}}), (200, []), (200, {"id": SUB}), (200, {"data": {"id": "weird/../id"}}), (200, {"data": {"id": "pub_x"}})])
def test_missing_or_unusable_subscription_id_is_retried_before_tags(response):
    outcome, fake = run(fake=Fake(responses=[response]))
    assert (outcome.kind, outcome.error) == ("retry", "subscription: response_missing_id")
    assert [c["url"] for c in fake.calls] == [SUBS_URL]


@pytest.mark.parametrize(
    "event_type,attendance,tag",
    [
        ("registration.created", "unknown", "webinar registered"),
        ("registration.updated", "unknown", None),
        ("attendance.checked_in", "unknown", "webinar attended"),
        ("attendance.recorded", "attended", "webinar attended"),
        ("attendance.recorded", "no_show", "webinar no-show"),
    ],
)
def test_lifecycle_tags(event_type, attendance, tag):
    b = body()
    b["registration"]["attendance"] = attendance
    outcome, fake = run(BH, b, event_type)
    assert outcome.kind == "delivered"
    urls = [c["url"] for c in fake.calls]
    assert urls == ([SUBS_URL] if tag is None else [SUBS_URL, TAGS_URL])
    if tag:
        assert fake.calls[1]["json"] == {"tags": [tag]}
    fields = {f["name"]: f["value"] for f in fake.calls[0]["json"]["custom_fields"]}
    assert fields["Last Webinar"] == "Sample webinar"
    outcome, fake = run(NO_TAGS, b, event_type)
    assert outcome.kind == "delivered" and [c["url"] for c in fake.calls] == [SUBS_URL]


def test_webinar_sources_map_lifecycle_facts():
    creds = {**BH, "native_custom_field_map": "webinar_starts_at=Webinar Start,webinar_registration_status=Webinar Status,webinar_attendance=Attendance"}
    b = body()
    b["registration"]["attendance"] = "attended"
    fields = {f["name"]: f["value"] for f in run(creds, b, "attendance.recorded")[1].calls[0]["json"]["custom_fields"]}
    assert fields == {"Webinar Start": "2026-01-02T12:00:00Z", "Webinar Status": "registered", "Attendance": "attended"}


def test_unknown_attendance_skipped():
    b = body()
    b["registration"]["attendance"] = "weird"
    outcome, fake = run(BH, b, "attendance.recorded")
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_attendance_unknown", [])


def test_never_tier_premium_stripe_referral_automations_or_other_endpoints():
    b = body()
    b["profile"].update({"phone": "+15550100", "tier": "premium", "stripe_customer_id": "cus_1", "automation_ids": ["aut_1"], "customField": "x"})
    outcome, fake = run(BH, b)
    assert outcome.kind == "delivered"
    subscription = fake.calls[0]["json"]
    assert not FORBIDDEN_KEYS & set(subscription)
    assert set(subscription) <= {"email", "reactivate_existing", "send_welcome_email", "custom_fields", "utm_source", "utm_medium", "utm_campaign", "double_opt_override"}
    assert "double_opt_override" not in subscription and "utm_source" not in subscription  # defaults: publication setting, no UTM
    for call in fake.calls:
        text = repr(call["json"])
        assert "+15550100" not in text and "customField" not in text and "premium" not in text and "cus_1" not in text and "aut_1" not in text
        assert not any(p in call["url"] for p in ("/by_email/", "/bulk_subscriptions", "/automations", "/custom_fields", "/posts", "/email_blasts", "/tiers", "/segments"))
    assert set(fake.calls[1]["json"]) == {"tags"}


def test_defaults_never_reactivate_or_welcome_and_explicit_choices_are_sent():
    assert run()[1].calls[0]["json"]["reactivate_existing"] is False
    assert run()[1].calls[0]["json"]["send_welcome_email"] is False
    creds = {**BH, "native_send_welcome_email": "true", "native_reactivate_existing": "true"}
    subscription = run(creds)[1].calls[0]["json"]
    assert subscription["reactivate_existing"] is True and subscription["send_welcome_email"] is True


@pytest.mark.parametrize("value,optin,sent", [("not_set", "", None), ("", "", None), ("on", "", "on"), ("off", "confirmed", "off")])
def test_double_opt_override_sent_only_when_chosen(value, optin, sent):
    creds = {**BH, "native_double_opt_override": value, "native_optin_ack": optin}
    subscription = run(creds)[1].calls[0]["json"]
    assert subscription.get("double_opt_override") == sent


def test_utm_fields_only_with_a_configured_source():
    b = body()
    b["event"]["slug"] = "spring-launch"
    subscription = run({**BH, "native_utm_source": "visioncast"}, b)[1].calls[0]["json"]
    assert (subscription["utm_source"], subscription["utm_medium"], subscription["utm_campaign"]) == ("visioncast", "webinar", "spring-launch")
    subscription = run(BH, b)[1].calls[0]["json"]
    assert not {"utm_source", "utm_medium", "utm_campaign"} & set(subscription)
    # No usable slug: utm_campaign is left out, never invented.
    for slug in (None, "", "bad slug", 12):
        b = body()
        b["event"]["slug"] = slug
        subscription = run({**BH, "native_utm_source": "visioncast"}, b)[1].calls[0]["json"]
        assert "utm_campaign" not in subscription and subscription["utm_medium"] == "webinar"


def test_empty_mapping_sends_email_and_flags_only():
    subscription = run({**BH, "native_custom_field_map": ""})[1].calls[0]["json"]
    assert subscription == BASE_SUBSCRIPTION


def test_partial_mapping_only_maps_listed_fields():
    subscription = run({**BH, "native_custom_field_map": "firstName=First Name"})[1].calls[0]["json"]
    assert subscription["custom_fields"] == [{"name": "First Name", "value": "Sample"}]


# --- groups / private links / identity / consent -------------------------------


def test_no_profile_group_uses_identity_email_without_profile_fields():
    b = body()
    b.pop("profile")
    outcome, fake = run(BH, b)
    assert outcome.kind == "delivered"
    assert fake.calls[0]["json"] == {**BASE_SUBSCRIPTION, "custom_fields": [{"name": "Last Webinar", "value": "Sample webinar"}]}


def test_no_email_skipped_before_any_call():
    b = body()
    b["profile"].pop("email")
    b["identity"].pop("email")
    outcome, fake = run(BH, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_no_email", [])


def test_unverified_identity_never_sent():
    for mutate in (lambda b: b["identity"].update(verified=False), lambda b: b["registration"].update(identityVerified=False)):
        b = body()
        mutate(b)
        outcome, fake = run(BH, b)
        assert (outcome.kind, outcome.error, fake.calls) == ("cancelled", "native_identity_unverified", [])


def test_not_analytics_consent_is_operator_choice():
    assert ni.validate("beehiiv", BH, **{**KW, "required_consents": []}) == {}
    b = body()
    b["consents"] = {"analytics": {"granted": False}}
    assert run(BH, b)[0].kind == "delivered"  # no analytics guard: Required consent gates it


def test_private_links_urls_and_unmapped_profile_never_sent():
    b = body()
    b["event"]["occurrence"].update({"joinUrl": "https://zoom.example/j/SECRET1", "waitingRoomUrl": "https://zoom.example/w/SECRET2", "managedRoomUrl": "https://w.example/w/SECRET3/"})
    b["event"]["heroImageUrl"] = "https://img.example/SECRET4"
    calls = run({**BH, "native_custom_field_map": "webinar_event_title=Last Webinar"}, b)[1].calls
    for call in calls:
        assert "SECRET" not in repr(call["json"]) and "https://" not in repr(call["json"])
    text = repr(calls[0]["json"]).replace("person@example.com", "")
    assert "Example Co" not in text and "Role" not in text and "Sample Person" not in text


def test_nested_field_value_refused_before_any_call():
    b = body()
    b["profile"]["firstName"] = {"given": "Sample"}
    outcome, fake = run(BH, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "subscription: payload_unsupported", [])


def test_oversize_body_refused_before_any_call():
    b = body()
    b["profile"]["company"] = "x" * (bh.MAX_BODY_BYTES + 1)
    outcome, fake = run(BH, b)
    assert (outcome.kind, outcome.error, fake.calls) == ("dead", "subscription: payload_too_large", [])


def test_consent_rechecked_before_tag_call():
    fake = Fake(responses=[CREATED])
    stop = ni.Outcome("cancelled", "consent_not_granted_mid_sequence")
    ctx = ni.NativeContext(post=fake.post, request=fake.request, renew=fake.renew, check=lambda: stop)
    outcome = ni.execute("beehiiv", BH, body(), "registration.created", ctx)
    assert outcome == stop and [c["url"] for c in fake.calls] == [SUBS_URL]


def test_lost_lease_stops_before_tag_call():
    fake = Fake(responses=[CREATED], renew=False)
    outcome, fake = run(fake=fake)
    assert outcome.kind == "lost_lease" and [c["url"] for c in fake.calls] == [SUBS_URL]


# --- responses / failures -------------------------------------------------------


class StatusFake:
    """``request`` seam (both calls read their response) sharing one ordered result list."""

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def ctx(self):
        def request(method, url, data, headers):
            self.calls.append(url)
            r = self.results.pop(0) if self.results else (200,)
            if isinstance(r, Exception):
                raise r
            return (r[0], json.dumps({"data": {"id": SUB}}).encode(), *r[1:])

        def post(url, data, headers):
            raise AssertionError("beehiiv reads every response")

        return ni.NativeContext(post=post, request=request, renew=lambda: True)


def test_all_steps_2xx_delivered():
    fake = StatusFake((200,), (200,))
    assert ni.execute("beehiiv", BH, body(), "registration.created", fake.ctx()).kind == "delivered"
    assert fake.calls == [SUBS_URL, TAGS_URL]


@pytest.mark.parametrize(
    "result,kind,error",
    [
        ((429, 30), "retry", "http_status: 429"),  # per-key 60 s window, documented
        ((429,), "retry", "http_status: 429"),  # no Retry-After documented: backoff only
        ((500,), "retry", "http_status: 500"),
        ((503, 7), "retry", "http_status: 503"),
        ((425,), "retry", "http_status: 425"),
        ((301,), "retry", "redirect_not_followed: http 301"),
        ((400,), "dead", "http_status: 400"),
        ((401,), "dead", "http_status: 401"),
        ((403,), "dead", "http_status: 403"),
        ((404,), "dead", "http_status: 404"),
        ((409,), "dead", "http_status: 409"),
        (TimeoutError(), "retry", "timeout"),
        (ConnectionResetError(KEY), "retry", "transport_error: ConnectionResetError"),
        (ValueError(f"echo {KEY} person@example.com"), "retry", "transport_error: ValueError"),
    ],
)
@pytest.mark.parametrize("step", ["subscription", "tags"])
def test_failures_stop_and_never_echo_secrets(step, result, kind, error):
    results = {"subscription": [result], "tags": [(200,), result]}[step]
    fake = StatusFake(*results)
    outcome = ni.execute("beehiiv", BH, body(), "registration.created", fake.ctx())
    assert (outcome.kind, outcome.error) == (kind, f"{step}: {error}")
    assert KEY not in outcome.error and "@" not in outcome.error and PUB not in outcome.error
    assert len(fake.calls) == len(results)
    if isinstance(result, tuple) and len(result) > 1:
        assert outcome.retry_after == result[1]


def test_retry_resends_identical_requests():
    first = [c["json"] for c in run()[1].calls]
    again = [c["json"] for c in run()[1].calls]
    assert first == again and first[0]["email"] == "person@example.com"


def test_max_calls_never_exceeded():
    assert len(run()[1].calls) <= ni.MAX_CALLS


# --- preview ------------------------------------------------------------------


def test_preview_redacts_api_key():
    calls = ni.preview("beehiiv", BH)
    assert [c["url"] for c in calls] == [SUBS_URL, f"{SUBS_URL}/sub_00000000-0000-0000-0000-000000000000/tags"]
    assert KEY not in repr(calls) and BEARER not in repr(calls)
    assert all(c["headers"]["Authorization"] == ni.REDACTED for c in calls)
    assert calls[0]["body"]["email"] == "person@example.com" and calls[0]["body"]["reactivate_existing"] is False
    assert calls[1]["body"] == {"tags": ["webinar registered"]}


def test_preview_empty_without_host(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["hooks.example.net"]
    assert ni.preview("beehiiv", {**BH, "native_region": "us"}) == []
    assert ni.preview_refusal("beehiiv", {**BH, "native_region": "us"}) is None  # validate reports it


def test_valid_preview_has_no_refusal():
    assert ni.preview("beehiiv", BH) and ni.preview_refusal("beehiiv", BH) is None
    assert len(ni.preview("beehiiv", NO_TAGS)) == 1 and ni.preview_refusal("beehiiv", NO_TAGS) is None
    assert ni.preview("beehiiv", {**BH, "native_double_opt_override": "on"})[0]["body"]["double_opt_override"] == "on"


@pytest.mark.parametrize(
    "creds, code",
    [
        ({**BH, "native_subscribe_ack": ""}, "native_match_strategy_unconfirmed"),
        ({**BH, "native_email_merge_ack": ""}, "native_match_strategy_unconfirmed"),
        ({**BH, "native_double_opt_override": "off"}, "native_match_strategy_unconfirmed"),
        ({**BH, "native_custom_field_map": "firstName=A,lastName=A"}, "native_config_invalid"),
        ({**BH, "native_publication_id": "pub_bad"}, "native_config_invalid"),
        ({**BH, "native_send_welcome_email": "yes"}, "native_config_invalid"),
    ],
)
def test_refused_preview_keeps_empty_list_and_reports_bounded_code(creds, code):
    assert ni.preview("beehiiv", creds) == []
    refusal = ni.preview_refusal("beehiiv", creds)
    assert refusal == (code, ni.PREVIEW_REFUSALS[code])
    assert KEY not in repr(refusal) and "person@example.com" not in repr(refusal)


# --- delivery (host pinning, consent gate, config change) ------------------------


class RecordingRequest:
    """Stands in for ``ssrf.request_json_bounded``: applies its host/credential policy, records calls."""

    def __init__(self, on_first=None, status=None):
        self.calls, self.on_first, self.status = [], on_first, status

    def __call__(self, method, url, data, headers, timeout, total, max_bytes, allowed_hosts=None):
        host = webinars.ssrf._validated_target(url)[0]
        webinars.ssrf._check_native_request(method, host, headers, allowed_hosts)
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "data": data, "allowed": allowed_hosts})
        if len(self.calls) == 1 and self.on_first:
            self.on_first()
        return self.status or 200, json.dumps({"data": {"id": SUB}}).encode(), None, False


@pytest.fixture
def transport(monkeypatch):
    fake = RecordingRequest()
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    return fake


def _deliver(msg):
    webinars.delivery.deliver(msg)
    msg.refresh_from_db()
    return msg


def test_e2e_delivered_to_pinned_host_without_private_links(occurrence, transport, caplog):
    occurrence.join_url = "https://zoom.example/j/PRIVATE123"
    occurrence.save()
    msg, reg = _claimed(occurrence, BH, include_private_links=True)
    msg = _deliver(msg)
    assert msg.status == S.DELIVERED and msg.attempts == 1
    assert [(c["method"], c["url"]) for c in transport.calls] == [("POST", SUBS_URL), ("POST", TAGS_URL)]
    assert all(c["allowed"] == frozenset({HOST}) and c["headers"]["Authorization"] == BEARER for c in transport.calls)
    assert all(b"PRIVATE123" not in c["data"] for c in transport.calls)
    sent = json.loads(transport.calls[0]["data"])
    assert sent["email"] == reg.person.email and sent["reactivate_existing"] is False and sent["send_welcome_email"] is False
    assert json.loads(transport.calls[1]["data"]) == {"tags": ["webinar registered"]}
    assert KEY not in caplog.text and KEY not in msg.last_error


@pytest.mark.parametrize("status,expected", [(503, S.PENDING), (429, S.PENDING), (400, S.DEAD)])
def test_e2e_failures(occurrence, transport, status, expected):
    transport.status = status
    msg, _ = _claimed(occurrence, BH)
    msg = _deliver(msg)
    assert msg.status == expected and msg.attempts == 1 and msg.last_status_code == status
    assert msg.last_error == f"subscription: http_status: {status}" and KEY not in msg.last_error


def test_e2e_operator_required_consent_blocks_before_first_step(occurrence, transport):
    msg, _ = _claimed(occurrence, BH)
    Destination.objects.filter(pk=msg.destination_id).update(required_consents=["marketing"])
    msg = _deliver(msg)
    assert transport.calls == [] and msg.status == S.CANCELLED


@pytest.mark.parametrize("target", ["https://api.segment.io", "https://hooks.example.net"])
def test_e2e_never_sent_to_another_host(occurrence, monkeypatch, transport, caplog, target):
    msg, _ = _claimed(occurrence, BH)
    monkeypatch.setattr(ni, "base_url", lambda provider, credentials: target)
    msg = _deliver(msg)
    assert transport.calls == []
    assert msg.status == S.DEAD and msg.last_error.endswith(": unsafe_destination")
    assert KEY not in msg.last_error and KEY not in caplog.text


def test_e2e_config_change_after_subscription_stops_before_tag(occurrence, monkeypatch):
    from webinars.crypto import encrypt_secret

    msg, _ = _claimed(occurrence, BH)
    changed = {**BH, "native_tag_registered": "other"}
    fake = RecordingRequest(lambda: Destination.objects.filter(pk=msg.destination_id).update(credentials_encrypted=encrypt_secret(json.dumps(changed))))
    monkeypatch.setattr(webinars.ssrf, "request_json_bounded", fake)
    msg = _deliver(msg)
    assert len(fake.calls) == 1 and fake.calls[0]["url"] == SUBS_URL  # the subscription is not recalled; no stale tag
    assert msg.status == S.DEAD and msg.last_error == "native_config_changed_mid_sequence"


def test_e2e_without_request_transport_dead(occurrence, monkeypatch):
    monkeypatch.delattr(webinars.ssrf, "request_json_bounded", raising=False)
    msg, _ = _claimed(occurrence, BH)
    msg = _deliver(msg)
    assert msg.status == S.DEAD and msg.last_error == "native_transport_unavailable"
