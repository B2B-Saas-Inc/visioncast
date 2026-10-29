"""Amplitude and Mixpanel identity: user id = verified e-mail (or platform person id) and the website
visitor's captured device id is sent so the vendor merges the anonymous visitor into the registrant."""
import pytest

from tests.test_native_expansion import AMPLITUDE
from tests.test_native_integrations import body, run
from tests.test_native_remaining_analytics import MIXPANEL
from webinars import attribution
from webinars import native_integrations as ni

pytestmark = pytest.mark.django_db

EMAIL = ni.SAMPLE_BODY["identity"]["email"]
PID = ni.SAMPLE_BODY["identity"]["personId"]
UUID4 = "3f2a1b6c-8d4e-4f0a-9b1c-2d3e4f5a6b7c"
GRANTED = {"analytics": {"granted": True, "source": "trusted_api", "serverVerified": True}}


@pytest.fixture(autouse=True)
def allowlist(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = []


def _captured(key, value, touch="registrationTouch"):
    return body(consents=GRANTED, attribution={touch: {"identifiers": {key: value}}})


def _amp(creds=AMPLITUDE, b=None):
    out, fake = run(creds, b)
    assert out.kind == "delivered", (out.kind, out.error)
    return fake.calls[0]["json"]["events"][0]


def _mp(creds=MIXPANEL, b=None):
    out, fake = run(creds, b)
    assert out.kind == "delivered", (out.kind, out.error)
    return fake.calls[0]["json"]


# --- Amplitude ----------------------------------------------------------------


def test_amplitude_user_id_is_email_by_default_and_person_id_on_request():
    assert _amp()["user_id"] == EMAIL
    assert _amp({**AMPLITUDE, "native_identity": "person_id"})["user_id"] == PID


def test_amplitude_sends_captured_device_id_only():
    event = _amp(b=_captured("amplitudeDeviceId", UUID4))
    assert event["device_id"] == UUID4 and event["user_id"] == EMAIL
    assert "device_id" not in _amp()  # never fabricated
    assert _amp(b=_captured("amplitudeDeviceId", UUID4, touch="firstTouch"))["device_id"] == UUID4


@pytest.mark.parametrize("bad", ["short", "has space", "x" * 65, "", 7])
def test_amplitude_ignores_malformed_device_ids(bad):
    assert "device_id" not in _amp(b=_captured("amplitudeDeviceId", bad))


def test_amplitude_identity_choice_validated():
    kw = dict(event_types=["registration.created"], allowed_groups=["identity", "registration", "event", "consents"], required_consents=["analytics"])
    assert "native-identity" in ni.validate("amplitude", {**AMPLITUDE, "native_identity": "cookie"}, **kw)
    assert "native-identity" not in ni.validate("amplitude", AMPLITUDE, **kw)


# --- Mixpanel -----------------------------------------------------------------


def test_mixpanel_simplified_merge_sends_user_and_device_ids():
    (event,) = _mp(b=_captured("mixpanelDeviceId", UUID4))
    props = event["properties"]
    assert props["$user_id"] == EMAIL == props["distinct_id"] and props["$device_id"] == UUID4
    assert event["event"] == "webinar registered"


def test_mixpanel_person_id_mode():
    (event,) = _mp({**MIXPANEL, "native_identity": "person_id"})
    assert event["properties"]["$user_id"] == PID == event["properties"]["distinct_id"]


def test_mixpanel_original_merge_prepends_identify_for_uuid4_anon_ids():
    identify, event = _mp({**MIXPANEL, "native_id_merge": "original"}, _captured("mixpanelDeviceId", UUID4))
    assert identify["event"] == "$identify"
    p = identify["properties"]
    assert p["$identified_id"] == EMAIL and p["$anon_id"] == UUID4 and p["distinct_id"] == EMAIL
    assert p["time"] == event["properties"]["time"] and p["$insert_id"] != event["properties"]["$insert_id"]
    assert "$device_id" not in event["properties"]  # original mode never sets $device_id


def test_mixpanel_original_merge_skips_non_uuid4_ids():
    (event,) = _mp({**MIXPANEL, "native_id_merge": "original"}, _captured("mixpanelDeviceId", "device-abc-12345"))
    assert event["event"] == "webinar registered"


def test_mixpanel_choices_validated():
    kw = dict(event_types=["registration.created"], allowed_groups=["identity", "registration", "event", "consents"], required_consents=["analytics"])
    assert "native-id_merge" in ni.validate("mixpanel", {**MIXPANEL, "native_id_merge": "legacy"}, **kw)
    assert "native-identity" in ni.validate("mixpanel", {**MIXPANEL, "native_identity": "cookie"}, **kw)
    assert ni.validate("mixpanel", MIXPANEL, **kw) == {}


def test_attribution_keeps_both_device_ids_only_with_analytics_consent():
    raw = {"identifiers": {"mixpanelDeviceId": UUID4, "amplitude_device_id": UUID4}}
    kept = attribution.clean_attribution(raw, {"analytics": {"granted": True}})["identifiers"]
    assert kept == {"mixpanelDeviceId": UUID4, "amplitudeDeviceId": UUID4}
    assert attribution.clean_attribution(raw, {"advertising": {"granted": True}})["identifiers"] == {}
