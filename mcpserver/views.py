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


def unauthorized(message="Sign in with OAuth, or use a Flowly API token: Authorization: Bearer flw_…", error=None, workspace_id=None):
    from mcpauth.views import api_base, with_cors

    res = JsonResponse({"error": error or "unauthorized", "message": message}, status=401)
    path = f"api/mcp/w/{workspace_id}/" if workspace_id else "api/mcp/"
    meta = f"{api_base()}/.well-known/oauth-protected-resource/{path}"
    extra = f', error="{error}"' if error else ""
    res["WWW-Authenticate"] = f'Bearer resource_metadata="{meta}"{extra}'
    return with_cors(res)


def authenticate(request):
    """(user, bound_workspace_id) from a personal token (flw_), OAuth access token (flo_) or workspace
    service token (flws_). bound_workspace_id limits the token to one workspace endpoint.
    None if there's no token; raises AuthenticationFailed for a bad one."""
    from mcpauth.views import authenticate_access_token, authenticate_workspace_token

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    raw = header[7:].strip()
    if raw.startswith("flws_"):
        found = authenticate_workspace_token(raw)
    elif raw.startswith("flo_"):
        found = authenticate_access_token(raw)
    else:
        auth = ApiTokenAuthentication().authenticate(request)
        found = (auth[0], None) if auth else None
    if found is None:
        raise AuthenticationFailed("The token is invalid, revoked or expired.")
    return found


@method_decorator(csrf_exempt, name="dispatch")
class McpView(View):
    def options(self, request, workspace_id=None):
        from mcpauth.views import with_cors

        return with_cors(HttpResponse(status=204))

    def get(self, request, workspace_id=None):
        # No server-initiated stream: every response comes back on the POST.
        return HttpResponse(status=405, headers={"Allow": "POST"})

    def delete(self, request, workspace_id=None):
        return HttpResponse(status=405, headers={"Allow": "POST"})

    def post(self, request, workspace_id=None):
        try:
            found = authenticate(request)
        except AuthenticationFailed as exc:
            return unauthorized(str(exc.detail), error="invalid_token", workspace_id=workspace_id)
        if found is None:
            return unauthorized(workspace_id=workspace_id)
        user, bound = found
        if bound is not None and bound != workspace_id:
            return unauthorized("This token only works on its workspace's MCP endpoint.", error="invalid_token", workspace_id=workspace_id)
        if workspace_id is not None and not user.memberships.filter(workspace_id=workspace_id).exists():
            return unauthorized("You aren't a member of this workspace.", error="invalid_token", workspace_id=workspace_id)
        self.scope = workspace_id
        try:
            payload = json.loads(request.body or b"null")
        except ValueError:
            return JsonResponse(rpc_error(None, -32700, "Parse error"), status=400)

        messages = payload if isinstance(payload, list) else [payload]
        from mcpauth.views import with_cors

        replies = [r for r in (self.handle(user, m) for m in messages) if r is not None]
        if not replies:
            return with_cors(HttpResponse(status=202))  # only notifications / responses
        return with_cors(JsonResponse(replies if isinstance(payload, list) else replies[0], safe=False))

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
                instructions = INSTRUCTIONS
                if self.scope:
                    from workspaces.models import Workspace

                    name = Workspace.objects.filter(pk=self.scope).values_list("name", flat=True).first()
                    instructions += f" This connection is limited to the workspace “{name}”."
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": instructions,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name = params.get("name")
                if name not in {t["name"] for t in TOOLS}:
                    return rpc_error(id_, -32602, f"Unknown tool: {name}")
                text, is_error = run_tool(user, name, params.get("arguments") or {}, scope=self.scope)
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            else:
                return rpc_error(id_, -32601, f"Method not found: {method}")
        except Exception:
            log.exception("MCP %s failed", method)
            return rpc_error(id_, -32603, "Internal error")
        return {"jsonrpc": "2.0", "id": id_, "result": result}
