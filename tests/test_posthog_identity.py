"""PostHog identity: distinct_id is the verified e-mail (or the platform person id on request) and the
website visitor's captured PostHog distinct_id is merged through ``$anon_distinct_id``."""
import pytest

from tests.test_native_integrations import POSTHOG, body, run
from webinars import attribution
from webinars import native_integrations as ni

pytestmark = pytest.mark.django_db

ANON = "0192f4c1-5b7e-7a3c-9d2e-1f0a2b3c4d5e"
GRANTED = {"analytics": {"granted": True, "source": "trusted_api", "serverVerified": True}}


def _with_anon(anon=ANON, touch="registrationTouch"):
    return body(consents=GRANTED, attribution={touch: {"identifiers": {"posthogDistinctId": anon}}})


def _batch(creds=POSTHOG, b=None):
    out, fake = run(creds, b)
    assert out.kind == "delivered", (out.kind, out.error)
    return fake.calls[0]["json"]["batch"]


def test_default_identity_is_the_verified_email():
    identify, group, capture = _batch()
    email = ni.SAMPLE_BODY["identity"]["email"]
    assert identify["distinct_id"] == email and capture["distinct_id"] == email
    assert "$anon_distinct_id" not in identify["properties"]  # nothing captured: no merge, no invention
    assert identify["properties"]["$set"]["email"] == email


def test_person_id_mode_keeps_the_platform_uuid():
    identify, _group, capture = _batch({**POSTHOG, "native_identity": "person_id"})
    pid = ni.SAMPLE_BODY["identity"]["personId"]
    assert identify["distinct_id"] == pid and capture["distinct_id"] == pid


def test_captured_website_distinct_id_is_merged_via_identify():
    identify, _group, capture = _batch(b=_with_anon())
    assert identify["event"] == "$identify" and identify["distinct_id"] == ni.SAMPLE_BODY["identity"]["email"]
    assert identify["properties"]["$anon_distinct_id"] == ANON
    assert capture["distinct_id"] != ANON  # the event belongs to the identified person


def test_first_touch_identifier_is_used_when_registration_touch_has_none():
    identify, *_ = _batch(b=_with_anon(touch="firstTouch"))
    assert identify["properties"]["$anon_distinct_id"] == ANON


def test_identify_sent_for_merge_even_without_profile_group():
    b = _with_anon()
    b.pop("profile")
    batch = _batch(b=b)
    identify = batch[0]
    assert identify["event"] == "$identify" and identify["properties"] == {"$anon_distinct_id": ANON}


def test_no_identify_without_profile_or_captured_id():
    b = body()
    b.pop("profile")
    batch = _batch(b=b)
    assert [e["event"] for e in batch] == ["$groupidentify", "webinar registered"]


@pytest.mark.parametrize("bad", ["has space", "x" * 201, "", 123, "<script>"])
def test_malformed_captured_ids_are_ignored(bad):
    identify, *_ = _batch(b=_with_anon(anon=bad))
    assert "$anon_distinct_id" not in identify["properties"]


def test_same_id_as_distinct_id_is_not_sent_as_anon():
    email = ni.SAMPLE_BODY["identity"]["email"]
    identify, *_ = _batch(b=_with_anon(anon=email))
    assert "$anon_distinct_id" not in identify["properties"]


def test_identity_choice_validated():
    ok = dict(event_types=["registration.created"], allowed_groups=["identity", "profile", "registration", "event"], required_consents=["analytics"])
    assert "native-identity" in ni.validate("posthog", {**POSTHOG, "native_identity": "cookie"}, **ok)
    assert "native-identity" not in ni.validate("posthog", {**POSTHOG, "native_identity": "email"}, **ok)
    assert "native-identity" not in ni.validate("posthog", POSTHOG, **ok)  # unset = email


def test_attribution_keeps_the_posthog_id_only_with_analytics_consent():
    raw = {"identifiers": {"posthogDistinctId": ANON, "_ga": "GA1.1.1.2"}}
    kept = attribution.clean_attribution(raw, {"analytics": {"granted": True}})
    assert kept["identifiers"]["posthogDistinctId"] == ANON
    dropped = attribution.clean_attribution(raw, {"advertising": {"granted": True}})
    assert "posthogDistinctId" not in dropped["identifiers"]
    assert attribution.clean_attribution({"identifiers": {"posthog_distinct_id": ANON}}, {"analytics": {"granted": True}})["identifiers"]["posthogDistinctId"] == ANON


def test_preview_sample_has_no_anon_merge_and_uses_email():
    calls = ni.preview("posthog", POSTHOG)
    identify = calls[0]["body"]["batch"][0]
    assert identify["distinct_id"] == ni.SAMPLE_BODY["identity"]["email"] and "$anon_distinct_id" not in identify["properties"]
