"""MCP endpoint protocol contract: POST /mcp, JSON-RPC 2.0 over Streamable HTTP (JSON responses)."""
import json

import pytest

from tests.conftest import JOIN_URL, WAITING_URL, make_api_key, make_event, make_form, make_occurrence

pytestmark = pytest.mark.django_db

PROTOCOL = "2025-06-18"


def rpc(client, key, method, params=None, msg_id=1, raw=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    data = raw if raw is not None else json.dumps(body)
    return client.post("/mcp", data=data, content_type="application/json", headers=headers)


def tool_names(client, key):
    resp = rpc(client, key, "tools/list")
    assert resp.status_code == 200
    data = resp.json()
    assert data["jsonrpc"] == "2.0"
    return {tool["name"] for tool in data["result"]["tools"]}


@pytest.fixture
def read_key(db):
    return make_api_key(("public_read",), name="agent-read")


@pytest.fixture
def write_key(db):
    return make_api_key(("public_read", "content_write"), name="agent-write")


def test_missing_key_401(client):
    resp = rpc(client, None, "initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    assert resp.status_code == 401


def test_invalid_key_401(client):
    assert rpc(client, "wbk_deadbeef_nope", "tools/list").status_code == 401


def test_get_not_allowed(client, read_key):
    resp = client.get("/mcp", headers={"Authorization": f"Bearer {read_key}", "Accept": "text/event-stream"})
    assert resp.status_code == 405


def test_initialize(client, read_key):
    resp = rpc(
        client,
        read_key,
        "initialize",
        {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1.0"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 1
    result = data["result"]
    assert result["protocolVersion"] == PROTOCOL
    assert result["serverInfo"]["name"]
    assert "tools" in result["capabilities"]


def test_initialize_unsupported_version_negotiates(client, read_key):
    resp = rpc(
        client,
        read_key,
        "initialize",
        {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "old", "version": "0"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["protocolVersion"] != "1999-01-01"
    assert result["protocolVersion"]  # the server's own supported version


def test_initialized_notification_accepted(client, read_key):
    resp = rpc(client, read_key, "notifications/initialized", msg_id=None)
    assert resp.status_code == 202
    assert resp.content == b""


def test_tools_list_scoped_to_key(client, read_key, write_key):
    read_tools = tool_names(client, read_key)
    assert {"list_events", "get_event", "get_form_schema"} <= read_tools
    assert "create_event" not in read_tools
    write_tools = tool_names(client, write_key)
    assert "create_event" in write_tools


def test_tools_have_input_schemas(client, write_key):
    data = rpc(client, write_key, "tools/list").json()
    for tool in data["result"]["tools"]:
        assert tool.get("description")
        assert tool["inputSchema"]["type"] == "object"


def test_get_event_returns_content_without_join_url(client, read_key):
    form = make_form()
    event = make_event(form, slug="agentic-launch")
    make_occurrence(event)
    resp = rpc(client, read_key, "tools/call", {"name": "get_event", "arguments": {"slug": "agentic-launch"}})
    assert resp.status_code == 200
    data = resp.json()
    assert "error" not in data
    result = data["result"]
    assert result.get("isError") in (None, False)
    assert result["content"]
    assert result["content"][0]["type"] == "text"
    assert "agentic-launch" in json.dumps(result)
    assert JOIN_URL not in resp.content.decode()
    assert WAITING_URL not in resp.content.decode()


def test_get_event_draft_not_visible(client, read_key):
    form = make_form()
    make_event(form, slug="secret-draft", visibility="draft")
    resp = rpc(client, read_key, "tools/call", {"name": "get_event", "arguments": {"slug": "secret-draft"}})
    data = resp.json()
    assert "error" in data or data["result"].get("isError") is True


def test_list_events_never_leaks_join_url(client, read_key):
    form = make_form()
    make_occurrence(make_event(form, slug="listed-one"))
    resp = rpc(client, read_key, "tools/call", {"name": "list_events", "arguments": {}})
    assert resp.status_code == 200
    assert "listed-one" in resp.content.decode()
    assert JOIN_URL not in resp.content.decode()


def test_create_event_requires_content_write(client, read_key):
    from webinars.models import Event

    resp = rpc(
        client,
        read_key,
        "tools/call",
        {"name": "create_event", "arguments": {"internalName": "Nope", "slug": "nope", "publicTitle": "Nope"}},
    )
    data = resp.json()
    assert "error" in data or data["result"].get("isError") is True
    assert not Event.objects.filter(slug="nope").exists()


def test_unknown_method(client, read_key):
    data = rpc(client, read_key, "does/not/exist").json()
    assert data["error"]["code"] == -32601
    assert data["id"] == 1


def test_parse_error(client, read_key):
    resp = rpc(client, read_key, "x", raw="{not json")
    data = resp.json()
    assert data["error"]["code"] == -32700
    assert data["id"] is None
