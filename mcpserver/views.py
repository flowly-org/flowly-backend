"""Flowly as a remote MCP server (Streamable HTTP transport, stateless JSON responses).

Clients POST JSON-RPC messages to /api/mcp/ with `Authorization: Bearer flw_…` (a personal API token).
Tools run as that user with the same permissions as the app. Workspaces whose AI provider is marked
*exclusive* stay hidden, since their data must not reach other models.
"""

import json
import logging

from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework.exceptions import AuthenticationFailed

from accounts.tokens import ApiTokenAuthentication

from .tools import TOOLS, run_tool

log = logging.getLogger("flowly.mcp")

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]
SERVER_INFO = {"name": "flowly", "title": "Flowly", "version": "1.0.0"}
INSTRUCTIONS = (
    "Flowly is the user's task planner. Each day has a board of tasks across workspaces and projects. "
    "Use list_tasks before answering questions about their day; refer to tasks by their #id. "
    "Dates are YYYY-MM-DD or today/tomorrow/backlog."
)


def rpc_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def unauthorized(message="Use a Flowly API token: Authorization: Bearer flw_…"):
    res = JsonResponse({"error": "unauthorized", "message": message}, status=401)
    res["WWW-Authenticate"] = 'Bearer realm="flowly"'
    return res


@method_decorator(csrf_exempt, name="dispatch")
class McpView(View):
    def get(self, request):
        # No server-initiated stream: every response comes back on the POST.
        return HttpResponse(status=405, headers={"Allow": "POST"})

    def delete(self, request):
        return HttpResponse(status=405, headers={"Allow": "POST"})

    def post(self, request):
        try:
            auth = ApiTokenAuthentication().authenticate(request)
        except AuthenticationFailed as exc:
            return unauthorized(str(exc.detail))
        if auth is None:
            return unauthorized()
        user = auth[0]
        try:
            payload = json.loads(request.body or b"null")
        except ValueError:
            return JsonResponse(rpc_error(None, -32700, "Parse error"), status=400)

        messages = payload if isinstance(payload, list) else [payload]
        replies = [r for r in (self.handle(user, m) for m in messages) if r is not None]
        if not replies:
            return HttpResponse(status=202)  # only notifications / responses
        return JsonResponse(replies if isinstance(payload, list) else replies[0], safe=False)

    def handle(self, user, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return rpc_error(None, -32600, "Invalid request")
        method, id_ = msg.get("method"), msg.get("id")
        if method is None:  # a response from the client; nothing to do
            return None
        if id_ is None:  # notification (e.g. notifications/initialized)
            return None
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name = params.get("name")
                if name not in {t["name"] for t in TOOLS}:
                    return rpc_error(id_, -32602, f"Unknown tool: {name}")
                text, is_error = run_tool(user, name, params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            else:
                return rpc_error(id_, -32601, f"Method not found: {method}")
        except Exception:
            log.exception("MCP %s failed", method)
            return rpc_error(id_, -32603, "Internal error")
        return {"jsonrpc": "2.0", "id": id_, "result": result}
