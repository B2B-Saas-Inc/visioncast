"""Developer documentation served by the app (public, no secrets): /developers/..."""
from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.views.decorators.http import require_GET

from webinars.models import ApiKey
from webinars.openapi import build_spec


@require_GET
def openapi_json(request):
    return JsonResponse(build_spec(), json_dumps_params={"indent": 2})


@require_GET
def docs_index(request):
    return TemplateResponse(request, "developers/index.html", {"scopes": ApiKey.Scope.choices})


@require_GET
def api_reference(request):
    spec = build_spec()
    groups = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method == "parameters":
                continue
            tag = op.get("tags", ["Other"])[0]
            groups.setdefault(tag, []).append({
                "method": method.upper(), "path": path, "summary": op.get("summary", ""),
                "description": op.get("description", ""),
                "auth": "API key" if op.get("security", spec["security"]) else "None",
                "responses": sorted(op.get("responses", {}).items()),
            })
    return TemplateResponse(request, "developers/api.html", {"groups": sorted(groups.items()), "info": spec["info"]})


@require_GET
def mcp_guide(request):
    from webinars.mcp import SUPPORTED_VERSIONS, TOOLS

    tools = [{"name": t["name"], "description": t["description"], "scopes": ", ".join(str(s) for s in t["scopes"]),
              "args": ", ".join(t["inputSchema"]["properties"].keys())} for t in TOOLS]
    base = request.build_absolute_uri("/mcp")
    return TemplateResponse(request, "developers/mcp.html", {"tools": tools, "versions": SUPPORTED_VERSIONS, "endpoint": base})
