"""Form builder definitions (webinars.forms_schema) and the public form schema endpoint."""
import pytest
from django.core.exceptions import ValidationError

from tests.conftest import (
    make_event,
    make_form,
    make_occurrence,
    make_person,
    make_registration,
    payload,
    post_registration,
)
from webinars import forms_schema

pytestmark = pytest.mark.django_db

SIZES = ["1-10", "11-50", "51-200", "201+"]


def row(key, label=None, required=False, **kw):
    data = {"key": key, "label": label or forms_schema.DEFAULT_LABELS.get(key, key), "help": "", "required": required}
    data.update(kw)
    return data


def base_rows():
    return [
        row("email", required=True),
        row("firstName", required=True),
        row("companySize", options=SIZES),
        row("custom.role", label="Your role", type="select", options=["Marketing", "Product", "Engineering"]),
        row("custom.goals", label="What do you hope to learn?", type="textarea", help="Optional, helps the host."),
    ]


def errors_of(exc):
    return exc.value.message_dict if hasattr(exc.value, "error_dict") else {"_": exc.value.messages}


# ---------------------------------------------------------------------------
# build_definition
# ---------------------------------------------------------------------------


def test_build_definition_happy_path():
    definition = forms_schema.build_definition(base_rows())
    assert definition["required_fields"] == ["email", "firstName"]
    assert definition["optional_fields"] == ["companySize"]
    assert definition["custom_fields"] == ["role", "goals"]
    keys = [e["key"] for e in definition["field_layout"]]
    assert keys == ["email", "firstName", "companySize", "custom.role", "custom.goals"]
    company = definition["field_layout"][2]
    assert company["options"] == SIZES
    assert definition["field_layout"][3]["type"] == "select"


def test_email_must_be_present():
    rows = [row("firstName", required=True)]
    with pytest.raises(ValidationError) as exc:
        forms_schema.build_definition(rows)
    assert "_" in errors_of(exc)


def test_email_must_be_required():
    rows = [row("email", required=False), row("firstName", required=True)]
    with pytest.raises(ValidationError) as exc:
        forms_schema.build_definition(rows)
    assert "_" in errors_of(exc)


def test_empty_form_rejected():
    with pytest.raises(ValidationError):
        forms_schema.build_definition([])


def test_duplicate_field_rejected():
    rows = [row("email", required=True), row("firstName"), row("firstName", label="Given name")]
    with pytest.raises(ValidationError) as exc:
        forms_schema.build_definition(rows)
    assert "2" in {str(k) for k in errors_of(exc)}


def test_custom_field_cannot_be_required():
    rows = [row("email", required=True), row("custom.role", label="Role", type="text", required=True)]
    with pytest.raises(ValidationError) as exc:
        forms_schema.build_definition(rows)
    assert "1" in {str(k) for k in errors_of(exc)}


def test_options_rejected_on_first_name():
    rows = [row("email", required=True), row("firstName", options=["Ada", "Grace"])]
    with pytest.raises(ValidationError) as exc:
        forms_schema.build_definition(rows)
    assert "1" in {str(k) for k in errors_of(exc)}


def test_custom_select_needs_options():
    rows = [row("email", required=True), row("custom.team", label="Team", type="select", options=[])]
    with pytest.raises(ValidationError):
        forms_schema.build_definition(rows)


def test_custom_key_cannot_shadow_canonical_or_be_invalid():
    for bad in ("custom.email", "custom.9lives", "custom.has space"):
        rows = [row("email", required=True), row(bad, label="Bad", type="text")]
        with pytest.raises(ValidationError):
            forms_schema.build_definition(rows)


def test_unknown_custom_type_rejected():
    rows = [row("email", required=True), row("custom.file", label="Upload", type="file")]
    with pytest.raises(ValidationError):
        forms_schema.build_definition(rows)


def test_label_required():
    rows = [row("email", required=True), {"key": "firstName", "label": "  ", "required": False}]
    with pytest.raises(ValidationError):
        forms_schema.build_definition(rows)


# ---------------------------------------------------------------------------
# public_schema
# ---------------------------------------------------------------------------


def form_from_rows(version, rows, **kw):
    definition = forms_schema.build_definition(rows)
    return make_form(version=version, **definition, **kw)


def test_public_schema_order_and_labels():
    form = form_from_rows("schema-1", base_rows())
    schema = forms_schema.public_schema(form)
    assert schema["formVersion"] == "schema-1"
    assert schema["schemaVersion"] == forms_schema.SCHEMA_VERSION
    keys = [f["key"] for f in schema["fields"]]
    assert keys == ["email", "firstName", "companySize", "custom.role", "custom.goals"]
    by_key = {f["key"]: f for f in schema["fields"]}
    assert by_key["email"]["label"] == "Work email"
    assert by_key["email"]["type"] == "email"
    assert by_key["email"]["required"] is True
    assert by_key["email"]["autocomplete"] == "email"
    assert by_key["companySize"]["type"] == "select"
    assert by_key["companySize"]["options"] == SIZES
    assert by_key["custom.role"]["label"] == "Your role"
    assert by_key["custom.role"]["custom"] is True
    assert by_key["custom.role"]["required"] is False
    assert by_key["custom.goals"]["type"] == "textarea"
    assert by_key["custom.goals"]["help"] == "Optional, helps the host."
    assert schema["sms"]["disclosureVersion"] == "sms-v1"


def test_rows_for_legacy_form_without_layout():
    form = make_form(version="legacy-1")  # conftest defaults, empty field_layout
    rows = forms_schema.rows_for(form)
    keys = [r["key"] for r in rows]
    assert keys[:2] == ["email", "firstName"]
    assert "custom.role" in keys
    assert {r["key"]: r["label"] for r in rows}["firstName"] == "First name"


# ---------------------------------------------------------------------------
# Option enforcement during registration
# ---------------------------------------------------------------------------


def _options_occurrence():
    form = form_from_rows(
        "f1",  # payload() submits FORM_VERSION "f1"
        [row("email", required=True), row("firstName", required=True), row("companySize", options=SIZES)],
    )
    event = make_event(form)
    return make_occurrence(event)


def test_registration_rejects_company_size_outside_options(client, proxy_key):
    occ = _options_occurrence()
    resp = post_registration(client, payload(occ, profile={"companySize": "a zillion"}), proxy_key)
    assert 400 <= resp.status_code < 500
    assert "companySize" in resp.content.decode()


def test_registration_accepts_listed_company_size(client, proxy_key):
    occ = _options_occurrence()
    resp = post_registration(client, payload(occ, profile={"companySize": "11-50"}), proxy_key)
    assert resp.status_code == 201, resp.content


# ---------------------------------------------------------------------------
# GET /api/v1/forms/<version>
# ---------------------------------------------------------------------------


def test_form_schema_endpoint(client):
    form = form_from_rows("public-v2", base_rows())
    event = make_event(form, slug="schema-event")
    occ = make_occurrence(event)
    person = make_person(email="private.person@example.com", first_name="Secretive")
    make_registration(occ, person=person)
    resp = client.get("/api/v1/forms/public-v2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["formVersion"] == "public-v2"
    assert [f["key"] for f in data["fields"]][:2] == ["email", "firstName"]
    body = resp.content.decode()
    assert "private.person@example.com" not in body
    assert "Secretive" not in body
    assert "join.example.org" not in body


def test_form_schema_unknown_version_404(client):
    assert client.get("/api/v1/forms/does-not-exist").status_code == 404


def test_form_schema_inactive_unused_version_404(client):
    form_from_rows("retired-v1", base_rows(), is_active=False)
    assert client.get("/api/v1/forms/retired-v1").status_code == 404


def test_form_schema_post_not_allowed(client):
    form_from_rows("post-v1", base_rows())
    assert client.post("/api/v1/forms/post-v1", data="{}", content_type="application/json").status_code == 405
