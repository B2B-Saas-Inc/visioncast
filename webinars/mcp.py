"""Model Context Protocol server: stateless Streamable HTTP, JSON responses only.

Transport: ``POST /mcp`` with one JSON-RPC 2.0 message per request. Responses are
``application/json`` (no SSE stream; ``GET /mcp`` → 405, which the spec allows for
servers that do not offer a server-initiated stream). No ``Mcp-Session-Id`` is issued:
every request is authenticated independently. Supported protocol versions:
``2025-06-18`` (preferred), ``2025-03-26``, ``2024-11-05``. Batches are rejected.
Notifications and JSON-RPC responses sent by the client are accepted with ``202``.

Auth: ``Authorization: Bearer wbk_...`` (existing hashed, scoped API keys). The tools a
key sees and may call follow its scopes:

* ``public_read`` / ``content_write``: list_events, get_event, get_form_schema
* ``content_write``: create_event, update_event (never visibility), schedule_session,
  update_session, cancel_session, configure_waiting_room, create_host, create_topic
* ``attendance_write``: record_attendance
* ``reporting``: get_report_summary (aggregates only)
* configuration tools (``config_read``, ``publication``, ``delivery_admin``,
  ``keys_admin``, ``team_admin`` and more ``content_write``/``attendance_write`` tools):
  ``webinars.mcp_config``; resources and prompts: ``webinars.mcp_resources``. Matrix:
  docs/mcp.md.

Reads use the same query functions as the public HTTP API (``webinars.api.queries``),
so filters, counts, pagination and visibility match ``GET /api/v1/events`` and
``GET /api/v1/forms/<version>``. Writes call the management API logic in-process
(``management.invoke``), so input validation, revisions, rescheduling and auditing
are identical to REST; tool input schemas are generated from the same field
allowlists. Arguments are validated against each tool's ``inputSchema`` (types,
enums, bounds, lengths, required and unknown keys); failures, like all tool errors,
come back as a ``tools/call`` result with ``isError: true``. Unexpected exceptions
are logged and reported generically. Outputs never contain join links, person data
or secrets.
"""
import json
import logging
import re
import uuid

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from webinars import mcp_config, mcp_resources, ratelimit
from webinars.api import management, queries, views
from webinars.api.auth import api_key_from_request, has_scope
from webinars.models import ApiKey, Event, Host, Topic

logger = logging.getLogger(__name__)

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = SUPPORTED_VERSIONS[0]
SERVER_INFO = {"name": "webinar-platform", "title": "Webinar Platform", "version": "1.0.0"}
MAX_BODY = 64 * 1024

READ = (ApiKey.Scope.PUBLIC_READ, ApiKey.Scope.CONTENT_WRITE)
WRITE = (ApiKey.Scope.CONTENT_WRITE,)
ATTEND = (ApiKey.Scope.ATTENDANCE_WRITE,)
REPORT = (ApiKey.Scope.REPORTING,)

_UUID = {"type": "string", "format": "uuid"}


def _schema(props, required=()):
    out = {"type": "object", "properties": props, "additionalProperties": False}
    out["required"] = list(required)
    return out


def _with_id(id_name, base, *, nest_as=None):
    """Prefix a resource id to a management input schema (inline or under ``changes``)."""
    if nest_as:
        return _schema({id_name: _UUID, nest_as: base}, [id_name, nest_as])
    return _schema({id_name: _UUID, **base["properties"]}, [id_name, *base.get("required", [])])


LIST_EVENTS_SCHEMA = _schema({
    "q": {"type": "string", "maxLength": queries.MAX_QUERY_LENGTH, "description": "Search public title and summary."},
    "topic": {"type": "string", "maxLength": 120, "description": "Topic slug."},
    "host": {"type": "string", "maxLength": 120, "description": "Host slug."},
    "when": {"type": "string", "enum": list(queries.WHEN_VALUES),
             "description": "upcoming: a scheduled session in the future; recorded: a published recording."},
    "limit": {"type": "integer", "minimum": 1, "maximum": queries.MAX_LIMIT, "default": queries.DEFAULT_LIMIT},
    "offset": {"type": "integer", "minimum": 0, "default": 0},
})

TOOLS = [
    {"name": "list_events", "scopes": READ, "readOnly": True,
     "description": "List webinars, newest first, with the same filters and pagination as GET /api/v1/events. "
                    "With public_read: public events (public projection). With content_write: all events including "
                    "drafts (management projection, no private links; q also matches the internal name).",
     "inputSchema": LIST_EVENTS_SCHEMA},
    {"name": "get_event", "scopes": READ, "readOnly": True,
     "description": "Get one event by slug (public/unlisted for public_read; any event for content_write).",
     "inputSchema": _schema({"slug": {"type": "string", "minLength": 1, "maxLength": 160}}, ["slug"])},
    {"name": "get_form_schema", "scopes": READ, "readOnly": True,
     "description": "Canonical registration form schema (field keys, labels, types, options) for an active form "
                    "version or one used by a public/unlisted event (same rule as GET /api/v1/forms/<version>).",
     "inputSchema": _schema({"formVersion": {"type": "string", "minLength": 1, "maxLength": 64}}, ["formVersion"])},
    {"name": "create_event", "scopes": WRITE,
     "description": "Create a draft event (AIDA copy fields attention/interest/decision/action).",
     "inputSchema": management.event_input_schema(create=True)},
    {"name": "update_event", "scopes": WRITE,
     "description": "Update an event's content by id. Visibility cannot change here: use set_event_visibility "
                    "(publication scope, gated).",
     "inputSchema": _with_id("eventId", management.event_input_schema(), nest_as="changes")},
    {"name": "schedule_session", "scopes": WRITE,
     "description": "Add a session (occurrence) to an event. Same fields and validation as "
                    "POST /api/v1/manage/events/{id}/occurrences.",
     "inputSchema": _with_id("eventId", management.occurrence_input_schema(create=True))},
    {"name": "update_session", "scopes": WRITE,
     "description": "Change a session; time changes reschedule and notify registrants. Same fields as "
                    "PATCH /api/v1/manage/occurrences/{id}.",
     "inputSchema": _with_id("sessionId", management.occurrence_input_schema(), nest_as="changes")},
    {"name": "cancel_session", "scopes": WRITE, "description": "Cancel a session (idempotent).",
     "inputSchema": _schema({"sessionId": _UUID}, ["sessionId"])},
    {"name": "configure_waiting_room", "scopes": WRITE,
     "description": "Enable the hosted waiting room, set lead minutes, or set admission mode (auto/hold/open/ended).",
     "inputSchema": _with_id("sessionId", management.waiting_room_input_schema())},
    {"name": "create_host", "scopes": WRITE, "description": "Create a host (speaker).",
     "inputSchema": management.input_schema(management.HOST_FIELDS, Host, required=("name", "slug"))},
    {"name": "create_topic", "scopes": WRITE, "description": "Create a topic.",
     "inputSchema": management.input_schema(management.TOPIC_FIELDS, Topic, required=("name", "slug"))},
    {"name": "record_attendance", "scopes": ATTEND,
     "description": "Record attended / no_show / follow_up for a registration id.",
     "inputSchema": _with_id("registrationId", management.attendance_input_schema())},
    {"name": "get_report_summary", "scopes": REPORT, "readOnly": True,
     "description": "Aggregate registration/attendance counts per session (no personal data).",
     "inputSchema": _schema({})},
]
# Configuration tools (config_read / publication / delivery_admin / keys_admin, gated actions).
TOOLS += mcp_config.TOOLS
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


class ToolInputError(ValueError):
    """Expected, caller-correctable problem with tool arguments."""


def _rpc_result(msg_id, result):
    return JsonResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _rpc_error(msg_id, code, message, status=200):
    return JsonResponse({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}, status=status)


def _tool_text(data, is_error=False):
    result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": is_error}
    if isinstance(data, dict) and not is_error:
        result["structuredContent"] = data
    return result


def _tool_error(code, message, fields=None):
    error = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    return _tool_text({"error": error}, is_error=True)


def _origin_ok(request):
    """Browser requests must come from an allowed origin (same list as the direct proxy)."""
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # non-browser clients
    allowed = views._allowed_origins()
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    if base:
        allowed.add(base)
    return origin.rstrip("/") in allowed


# ---------------------------------------------------------------------------
# Argument validation (the JSON Schema subset used by TOOLS)
# ---------------------------------------------------------------------------

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _check_value(schema, value, path):
    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS.get(t, lambda v: False)(value) for t in types):
            raise ToolInputError(f"invalid type for {path}: expected {' or '.join(types)}")
    if "enum" in schema and not any(
        value == e and isinstance(value, bool) == isinstance(e, bool) for e in schema["enum"]
    ):
        raise ToolInputError(f"invalid value for {path}: must be one of {', '.join(json.dumps(e) for e in schema['enum'])}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ToolInputError(f"{path} must be at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolInputError(f"{path} must be at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ToolInputError(f"{path} has an invalid format")
        if schema.get("format") == "uuid":
            try:
                uuid.UUID(value)
            except ValueError:
                raise ToolInputError(f"{path} must be a UUID")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolInputError(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolInputError(f"{path} must be <= {schema['maximum']}")
    elif isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolInputError(f"{path} must have at most {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                _check_value(schema["items"], item, f"{path}[{i}]")
    elif isinstance(value, dict):
        _check_object(schema, value, path)


def _check_object(schema, value, path=""):
    props = schema.get("properties", {})
    extra = schema.get("additionalProperties", True)
    prefix = f"{path}." if path else ""
    for key in value:
        if key in props:
            continue
        if extra is False:
            raise ToolInputError(f"unknown argument: {prefix}{key}")
        if isinstance(extra, dict):
            _check_value(extra, value[key], f"{prefix}{key}")
    for key in schema.get("required", []):
        if key not in value:
            raise ToolInputError(f"missing argument: {prefix}{key}")
    for key, sub in props.items():
        if key in value:
            _check_value(sub, value[key], f"{prefix}{key}")


def _validate_args(tool, args):
    """Validate ``args`` against the tool's ``inputSchema``; return an error string or None."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    try:
        _check_object(tool["inputSchema"], args)
    except ToolInputError as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _list_events(args, content_writer):
    filters = {name: args.get(name) for name in ("q", "topic", "host", "when")}
    if not content_writer:
        return 200, views.list_public_events(**filters, limit=args.get("limit"), offset=args.get("offset"))
    qs = queries.filter_events(Event.objects.select_related("form_version"), search_internal_name=True, **filters)
    limit, offset = queries.parse_page(args.get("limit"), args.get("offset"))
    count, rows = queries.page(qs, limit, offset)
    return 200, {"count": count, "limit": limit, "offset": offset,
                 "results": [management.serialize_event(e) for e in rows]}


def _call(tool_name, args, key):
    tool = TOOLS_BY_NAME.get(tool_name)
    if tool is not None and "handler" in tool:
        return mcp_config.call(tool, args, key)
    content_writer = has_scope(key, ApiKey.Scope.CONTENT_WRITE)
    if tool_name == "list_events":
        return _list_events(args, content_writer)
    if tool_name == "get_event":
        if content_writer:
            event = Event.objects.filter(slug=args["slug"]).first()
            if event is None:
                return 404, {"error": "not_found"}
            return management.invoke(management.event_detail, "GET", key, pk=event.pk)
        event = queries.public_event(args["slug"])
        return (200, views.serialize_event(event)) if event else (404, {"error": "not_found"})
    if tool_name == "get_form_schema":
        from webinars.forms_schema import public_schema

        fv = queries.public_form_version(args["formVersion"])
        return (200, public_schema(fv)) if fv else (404, {"error": "not_found"})
    if tool_name == "create_event":
        return management.invoke(management.events, "POST", key, dict(args))
    if tool_name == "update_event":
        changes = args["changes"]
        if "visibility" in changes:
            current = Event.objects.filter(pk=args["eventId"]).values_list("visibility", flat=True).first()
            if current is not None and changes["visibility"] != current:
                # Publication has one path: the gated set_event_visibility tool (publication scope).
                return 403, {"error": {"code": "publication_gated",
                                       "message": "Change visibility with set_event_visibility (publication scope, "
                                                  "dry run then confirmToken)."}}
        return management.invoke(management.event_detail, "PATCH", key, dict(args["changes"]), pk=args["eventId"])
    if tool_name == "schedule_session":
        body = {k: v for k, v in args.items() if k != "eventId"}
        return management.invoke(management.event_occurrences, "POST", key, body, pk=args["eventId"])
    if tool_name == "update_session":
        return management.invoke(management.occurrence_detail, "PATCH", key, dict(args["changes"]), pk=args["sessionId"])
    if tool_name == "cancel_session":
        return management.invoke(management.occurrence_cancel, "POST", key, {}, pk=args["sessionId"])
    if tool_name == "configure_waiting_room":
        body = {k: v for k, v in args.items() if k != "sessionId"}
        return management.invoke(management.occurrence_waiting_room, "PATCH", key, body, pk=args["sessionId"])
    if tool_name == "create_host":
        return management.invoke(management.hosts, "POST", key, dict(args))
    if tool_name == "create_topic":
        return management.invoke(management.topics, "POST", key, dict(args))
    if tool_name == "record_attendance":
        return management.invoke(management.registration_attendance, "POST", key, {"kind": args["kind"]},
                                 pk=args["registrationId"])
    if tool_name == "get_report_summary":
        from webinars.reporting import report_summary

        return 200, report_summary()
    return 404, {"error": "unknown_tool"}


def _run_tool(name, args, key):
    """Execute a validated tool call and always return a ``tools/call`` result."""
    try:
        status, payload = _call(name, args, key)
    except queries.QueryError as exc:
        return _tool_error("validation_error", exc.message, exc.fields)
    except ToolInputError as exc:
        return _tool_error("validation_error", str(exc))
    except Exception:
        logger.exception("MCP tool %s failed", name)
        return _tool_error("internal_error", "The tool failed unexpectedly. Try again later.")
    return _tool_text(payload if payload is not None else {"ok": True}, is_error=status >= 400)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def _is_client_response(message):
    """A JSON-RPC response (to a server request) sent by the client: no method, has result/error."""
    return (
        isinstance(message, dict)
        and message.get("jsonrpc") == "2.0"
        and "method" not in message
        and "id" in message
        and ("result" in message or "error" in message)
    )


@csrf_exempt
def endpoint(request):
    if request.method != "POST":
        response = HttpResponse(status=405)
        response["Allow"] = "POST"
        return response
    if not _origin_ok(request):
        return _rpc_error(None, -32600, "Origin not allowed", status=403)
    if not ratelimit.hit(request, "mcp"):
        return _rpc_error(None, -32000, "Rate limit exceeded", status=429)
    key = api_key_from_request(request)
    if key is None:
        response = _rpc_error(None, -32001, "A valid API key is required (Authorization: Bearer ...)", status=401)
        response["WWW-Authenticate"] = 'Bearer realm="mcp"'
        return response
    if not ratelimit.hit_key("mcp_key", str(key.pk)):
        return _rpc_error(None, -32000, "Rate limit exceeded", status=429)
    header_version = request.headers.get("MCP-Protocol-Version")
    if header_version and header_version not in SUPPORTED_VERSIONS:
        return _rpc_error(None, -32600, f"Unsupported MCP-Protocol-Version: {header_version}", status=400)
    if len(request.body) > MAX_BODY:
        return _rpc_error(None, -32600, "Request too large", status=413)
    try:
        message = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _rpc_error(None, -32700, "Parse error", status=400)
    if isinstance(message, list):
        return _rpc_error(None, -32600, "Batch requests are not supported", status=400)
    if _is_client_response(message):
        # This server never sends requests to clients; accept and ignore (spec: 202, no body).
        return HttpResponse(status=202)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request", status=400)

    method = message["method"]
    msg_id = message.get("id")
    params = message.get("params") or {}
    if "id" not in message:
        # Notification (e.g. notifications/initialized): accepted, no body.
        return HttpResponse(status=202)
    if not isinstance(params, dict):
        return _rpc_error(msg_id, -32602, "Invalid params")

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else LATEST
        return _rpc_result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False, "subscribe": False},
                             "prompts": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "Webinar CMS tools. Read tools return public or management projections; private join links "
                            "and personal data are never returned. Start with resource webinar://playbook/implementation. "
                            "Gated tools need a dry run then its confirmToken; a confirmToken is not independent human approval.",
        })
    if method == "ping":
        return _rpc_result(msg_id, {})
    if method == "tools/list":
        tools = []
        for tool in TOOLS:
            if has_scope(key, *tool["scopes"]):
                entry = {"name": tool["name"], "description": tool["description"], "inputSchema": tool["inputSchema"]}
                entry["annotations"] = {"readOnlyHint": bool(tool.get("readOnly"))}
                tools.append(entry)
        return _rpc_result(msg_id, {"tools": tools})
    if method == "tools/call":
        name = params.get("name")
        tool = TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
        if tool is None:
            return _rpc_error(msg_id, -32602, f"Unknown tool: {name}")
        if not has_scope(key, *tool["scopes"]):
            return _rpc_error(msg_id, -32602, f"This API key's scopes do not allow {name}")
        args = params.get("arguments")
        if args is None:
            args = {}
        problem = _validate_args(tool, args)
        if problem:
            return _rpc_result(msg_id, _tool_error("validation_error", problem))
        return _rpc_result(msg_id, _run_tool(name, args, key))
    if method == "resources/list":
        return _rpc_result(msg_id, {"resources": mcp_resources.list_resources(key)})
    if method == "resources/templates/list":
        return _rpc_result(msg_id, {"resourceTemplates": [{
            "uriTemplate": mcp_resources.FORM_PREFIX + "{formVersion}", "name": "form-schema",
            "title": "Form schema and context", "mimeType": "application/json"}]})
    if method == "resources/read":
        try:
            contents = mcp_resources.read_resource(params.get("uri"), key)
        except Exception:
            logger.exception("MCP resource read failed")
            return _rpc_error(msg_id, -32603, "Internal error")
        if contents is None:
            # Unknown and not-permitted URIs are indistinguishable.
            return _rpc_error(msg_id, -32002, "Resource not found")
        return _rpc_result(msg_id, {"contents": contents})
    if method == "prompts/list":
        return _rpc_result(msg_id, {"prompts": mcp_resources.list_prompts()})
    if method == "prompts/get":
        try:
            prompt = mcp_resources.get_prompt(params.get("name"), params.get("arguments"))
        except ValueError as exc:
            return _rpc_error(msg_id, -32602, str(exc))
        if prompt is None:
            return _rpc_error(msg_id, -32602, "Unknown prompt")
        return _rpc_result(msg_id, prompt)
    return _rpc_error(msg_id, -32601, f"Method not found: {method}")
