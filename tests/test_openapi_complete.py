"""The served OpenAPI document is complete: local $refs resolve, management bodies
and responses are typed, and real management responses match the documented schemas."""
import json

import pytest

from tests.conftest import JOIN_URL, WAITING_URL, make_api_key, make_event, make_occurrence

pytestmark = pytest.mark.django_db

MANAGE = "Management (content_write)"


@pytest.fixture
def spec(client):
    resp = client.get("/api/v1/openapi.json")
    assert resp.status_code == 200
    return resp.json()


def _resolve(spec, node):
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        assert ref.startswith("#/"), ref
        target = spec
        for part in ref[2:].split("/"):
            target = target[part]
        node = target
    return node


def _walk_refs(node):
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            yield node["$ref"]
        for value in node.values():
            yield from _walk_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_refs(value)


def _schema(spec, path, method, status):
    return _resolve(spec, spec["paths"][path][method]["responses"][status]["content"]["application/json"]["schema"])


def _body(spec, path, method):
    return _resolve(spec, spec["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"])


def test_all_local_refs_resolve(spec):
    refs = set(_walk_refs(spec))
    assert refs
    for ref in refs:
        assert ref.startswith("#/components/schemas/"), ref
        _resolve(spec, {"$ref": ref})


def test_management_operations_are_typed_and_secured(spec):
    ops = [(p, m, op) for p, item in spec["paths"].items() if p.startswith("/api/v1/manage/")
           for m, op in item.items() if m in ("get", "post", "patch", "delete")]
    assert len(ops) >= 19
    for path, method, op in ops:
        assert op["security"] == [{"apiKey": []}], (path, method)
        assert op["tags"][0].startswith("Management ("), (path, method)
        assert {"401", "403"} <= set(op["responses"]), (path, method)
        if method in ("post", "patch"):
            body = _body(spec, path, method)
            assert body["type"] == "object", (path, method)
            if path.endswith("/cancel"):
                assert body["additionalProperties"] is False
            else:
                assert body.get("properties"), (path, method)
        for status, resp in op["responses"].items():
            if status.startswith("2") and status != "204":
                schema = _resolve(spec, resp["content"]["application/json"]["schema"])
                assert schema.get("properties"), (path, method, status)
        if "{" in path:
            params = op.get("parameters") or spec["paths"][path].get("parameters")
            assert params and all(p["in"] == "path" and p["required"] for p in params)


def test_create_vs_patch_required(spec):
    pairs = [("/api/v1/manage/events", "post", "/api/v1/manage/events/{id}", "patch", {"internalName", "slug", "publicTitle", "formVersion"}),
             ("/api/v1/manage/events/{id}/occurrences", "post", "/api/v1/manage/occurrences/{id}", "patch", {"timezone", "localStart"}),
             ("/api/v1/manage/hosts", "post", "/api/v1/manage/hosts/{slug}", "patch", {"name", "slug"}),
             ("/api/v1/manage/topics", "post", "/api/v1/manage/topics/{slug}", "patch", {"name", "slug"}),
             ("/api/v1/manage/forms", "post", "/api/v1/manage/forms/{version}", "patch", {"version", "requiredFields"})]
    for create_path, cm, patch_path, pm, required in pairs:
        create, patch = _body(spec, create_path, cm), _body(spec, patch_path, pm)
        assert set(create["required"]) == required, create_path
        assert not patch.get("required"), patch_path
        assert create["additionalProperties"] is False and patch["additionalProperties"] is False
    assert set(_body(spec, "/api/v1/manage/forms/{version}", "patch")["properties"]) == {"description", "isActive"}


def test_enums_reflect_models(spec):
    from webinars.models import Event, Occurrence

    schemas = spec["components"]["schemas"]
    assert schemas["ManagedEvent"]["properties"]["visibility"]["enum"] == list(Event.Visibility.values)
    assert schemas["EventInput"]["properties"]["thankYouMode"]["enum"] == list(Event.ThankYouMode.values)
    assert schemas["ManagedOccurrence"]["properties"]["status"]["enum"] == list(Occurrence.Status.values)
    assert schemas["WaitingRoomUpdate"]["properties"]["admissionMode"]["enum"] == list(Occurrence.Admission.values)


def test_registration_request_is_structured(spec):
    req = _resolve(spec, spec["components"]["schemas"]["RegistrationRequest"])
    props = req["properties"]
    for name in ("profile", "consents", "attribution"):
        assert _resolve(spec, props[name]).get("properties"), name
    assert set(_resolve(spec, props["consents"])["properties"]) == {"sms", "analytics", "advertising"}
    assert set(_resolve(spec, props["attribution"])["properties"]) == {"parameters", "identifiers", "pageUrl", "referrer"}
    ext = props["organization"]["properties"]["externalIds"]["items"]
    assert set(ext["required"]) == {"namespace", "value"}
    assert req["example"]["schemaVersion"] == "1"


def test_profile_query_parameters_required(spec):
    for path in ("/api/v1/profile", "/webinar-api/profile"):
        params = {p["name"]: p for p in spec["paths"][path]["get"]["parameters"]}
        assert params["occurrenceId"]["required"] and params["formVersion"]["required"]
        assert set(_schema(spec, path, "get", "200")["properties"]) == {"recognized", "missingFields", "formVersion"}


def _assert_covers(spec, schema, payload, where):
    schema = _resolve(spec, schema)
    props = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        assert set(payload) <= set(props), (where, set(payload) - set(props))
    for key in schema.get("required", []):
        assert key in payload, (where, key)
    assert set(props) >= set(payload) or schema.get("additionalProperties") is not False


def _assert_no_private_links(payload):
    text = json.dumps(payload)
    assert JOIN_URL not in text and WAITING_URL not in text


def test_real_management_responses_match_schema(client, spec, form):
    key = make_api_key(("content_write",), name="content")
    auth = {"Authorization": f"Bearer {key}"}
    schemas = spec["components"]["schemas"]

    def call(method, path, body=None):
        kwargs = {"headers": auth}
        if body is not None:
            kwargs.update(data=json.dumps(body), content_type="application/json")
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code in (200, 201), (path, resp.status_code, resp.content)
        _assert_no_private_links(resp.json())
        return resp.json()

    host = call("post", "/api/v1/manage/hosts", {"name": "Ada", "slug": "ada"})
    assert set(host) == set(schemas["Host"]["properties"])
    _assert_covers(spec, schemas["Host"], call("patch", "/api/v1/manage/hosts/ada", {"title": "Analyst"}), "host")

    topic = call("post", "/api/v1/manage/topics", {"name": "Analytics", "slug": "analytics"})
    assert set(topic) == set(schemas["Topic"]["properties"])
    _assert_covers(spec, schemas["Topic"], call("patch", "/api/v1/manage/topics/analytics", {"name": "Data"}), "topic")

    fv = call("post", "/api/v1/manage/forms", {"version": "f2", "requiredFields": ["email"]})
    assert set(fv) == set(schemas["ManagedFormVersion"]["properties"])
    _assert_covers(spec, schemas["ManagedFormVersion"], call("patch", "/api/v1/manage/forms/f2", {"isActive": False}), "form")

    created = call("post", "/api/v1/manage/events", {"internalName": "I", "slug": "spec-event", "publicTitle": "T",
                                                     "formVersion": form.version, "hosts": ["ada"], "topics": ["analytics"]})
    assert set(created) == set(schemas["ManagedEvent"]["properties"])
    _assert_covers(spec, schemas["ManagedEvent"], created, "event")

    event = make_event(form, slug="spec-public")
    make_occurrence(event)
    listed = call("get", "/api/v1/manage/events")
    _assert_covers(spec, schemas["ManagedEventList"], listed, "event list")
    full = next(e for e in listed["results"] if e["slug"] == "spec-public")
    assert set(full) == set(schemas["ManagedEvent"]["properties"])
    occ = full["occurrences"][0]
    assert set(occ) == set(schemas["ManagedOccurrence"]["properties"])
    assert occ["hasJoinUrl"] is True
    assert occ["status"] in schemas["ManagedOccurrence"]["properties"]["status"]["enum"]
    assert set(occ["waitingRoom"]) <= set(schemas["WaitingRoom"]["properties"])

    room = call("get", f"/api/v1/manage/occurrences/{occ['id']}/waiting-room")
    assert set(room) <= set(schemas["WaitingRoom"]["properties"])


def test_private_projection_preserved(spec):
    schemas = spec["components"]["schemas"]
    occ_in = schemas["OccurrenceInput"]["properties"]
    assert occ_in["joinUrl"]["writeOnly"] and occ_in["waitingRoomUrl"]["writeOnly"]
    for name in ("ManagedOccurrence", "ManagedEvent", "WaitingRoom", "RegistrationResponse", "ProfileStatus"):
        props = set(schemas[name]["properties"])
        assert not props & {"joinUrl", "waitingRoomUrl", "email", "phone"}, name
