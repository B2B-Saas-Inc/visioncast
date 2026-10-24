import pytest

from webinars.models import FormVersion
from webinars.normalization import (
    CANONICAL_FIELDS,
    FieldError,
    email_key,
    normalize_email,
    normalize_phone,
    normalize_submission,
)


def _form(**kw):
    defaults = dict(
        version="f1",
        required_fields=["email", "firstName"],
        optional_fields=["lastName", "company", "phone"],
        field_mapping={"work_email": "email", "org": "company"},
        custom_fields=["role"],
        default_phone_region="GB",
        sms_disclosure_version="sms-v1",
    )
    defaults.update(kw)
    return FormVersion(**defaults)  # unsaved: no DB needed


def test_canonical_fields_contract():
    assert CANONICAL_FIELDS == ["email", "phone", "firstName", "lastName", "name", "company", "jobTitle", "companySize"]


def test_canonical_keys_pass_through():
    n = normalize_submission({"email": "a@example.com", "firstName": "Ada", "jobTitle": "CTO"}, {}, _form())
    assert not n.errors
    assert n.fields == {"email": "a@example.com", "firstName": "Ada", "jobTitle": "CTO"}


def test_common_snake_case_aliases_map_to_canonical():
    n = normalize_submission({"first_name": "Ada", "last_name": "Lovelace", "company_size": "11-50"}, {}, _form())
    assert not n.errors
    assert n.fields == {"firstName": "Ada", "lastName": "Lovelace", "companySize": "11-50"}


def test_explicit_form_mapping():
    n = normalize_submission({"work_email": "a@example.com", "org": "Acme"}, {}, _form())
    assert not n.errors
    assert n.fields["email"] == "a@example.com"
    assert n.fields["company"] == "Acme"


def test_conflicting_aliases_rejected():
    n = normalize_submission({"firstName": "Ada", "first_name": "Grace"}, {}, _form())
    assert "firstName" in n.errors
    assert "firstName" not in n.fields


def test_identical_alias_values_are_not_a_conflict():
    n = normalize_submission({"firstName": "Ada", "first_name": "Ada"}, {}, _form())
    assert not n.errors
    assert n.fields["firstName"] == "Ada"


@pytest.mark.parametrize("key", ["fname", "FirstName", "e-mail", "emailAddress", "first name"])
def test_unknown_keys_rejected_without_fuzzy_matching(key):
    n = normalize_submission({key: "x"}, {}, _form())
    assert key in n.errors
    assert n.fields == {}


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_values_dropped(blank):
    n = normalize_submission({"email": "a@example.com", "company": blank, "lastName": blank}, {}, _form())
    assert not n.errors
    assert "company" not in n.fields
    assert "lastName" not in n.fields


def test_email_plus_and_dots_preserved_domain_lowercased():
    assert normalize_email("  Ada.Lovelace+tag@Example.COM ") == "Ada.Lovelace+tag@example.com"


def test_email_key_casefolds_without_stripping():
    key = email_key(normalize_email("Ada.Lovelace+tag@Example.COM"))
    assert key == "ada.lovelace+tag@example.com"
    assert email_key("ada.lovelace@example.com") != key
    assert email_key("adalovelace+tag@example.com") != key


@pytest.mark.parametrize(
    "bad",
    ["plainaddress", "a@b", "a@@example.com", "a b@example.com", ".a@example.com", "a..b@example.com", "a@example..com", "a@-example.com"],
)
def test_invalid_email_rejected(bad):
    with pytest.raises(FieldError):
        normalize_email(bad)


def test_invalid_email_reported_in_submission():
    n = normalize_submission({"email": "not-an-email"}, {}, _form())
    assert "email" in n.errors


def test_phone_international_to_e164_without_region():
    assert normalize_phone("+44 20 7946 0958", None) == "+442079460958"
    assert normalize_phone("+1 (212) 555-0199", None) == "+12125550199"


def test_phone_national_with_region_context():
    assert normalize_phone("020 7946 0958", "GB") == "+442079460958"
    assert normalize_phone("(212) 555-0199", "US") == "+12125550199"


def test_phone_national_without_region_rejected():
    with pytest.raises(FieldError):
        normalize_phone("020 7946 0958", None)
    with pytest.raises(FieldError):
        normalize_phone("020 7946 0958", "")


@pytest.mark.parametrize("bad", ["abc", "+12", "+44 20 7946 0958 ext 1", "12345"])
def test_phone_garbage_rejected(bad):
    with pytest.raises(FieldError):
        normalize_phone(bad, "GB")


def test_submission_phone_uses_form_region_only():
    n = normalize_submission({"phone": "020 7946 0958"}, {}, _form(default_phone_region="GB"))
    assert n.fields["phone"] == "+442079460958"
    n2 = normalize_submission({"phone": "020 7946 0958"}, {}, _form(default_phone_region=""))
    assert "phone" in n2.errors
    assert "phone" not in n2.fields


def test_names_never_split_or_joined():
    n = normalize_submission({"name": "Ada King Lovelace"}, {}, _form())
    assert n.fields == {"name": "Ada King Lovelace"}
    n2 = normalize_submission({"firstName": "Ada", "lastName": "Lovelace"}, {}, _form())
    assert "name" not in n2.fields


def test_custom_fields_namespaced():
    n = normalize_submission({}, {"role": "Engineer"}, _form())
    assert not n.errors
    assert n.custom == {"custom.role": "Engineer"}
    # Already-namespaced input is accepted too.
    n2 = normalize_submission({}, {"custom.role": "Engineer"}, _form())
    assert n2.custom == {"custom.role": "Engineer"}


def test_non_allowlisted_custom_field_rejected():
    n = normalize_submission({}, {"favourite_colour": "blue"}, _form())
    assert n.errors
    assert n.custom == {}


def test_custom_fields_cannot_spoof_canonical_fields():
    n = normalize_submission({}, {"email": "x@example.com"}, _form())
    assert n.errors
    assert "email" not in n.fields
