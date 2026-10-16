"""Stairway's MCP endpoint (JSON-RPC over HTTP at <url>/mcp/<token>)."""

import json

from integrations.http import DeliveryError, send


class StairwayError(Exception):
    pass


def _endpoint(integration) -> str:
    from integrations.connectors import REGISTRY

    return f"{integration.config['url']}/mcp/{REGISTRY['stairway'].token(integration)}"


def call(integration, method: str, params: dict | None = None, timeout=30) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    try:
        res = send("POST", _endpoint(integration), json=body, timeout=timeout,
                   headers={"Accept": "application/json, text/event-stream"}).json()
    except DeliveryError as exc:
        if exc.status in (401, 403, 404):
            raise StairwayError("Stairway refused the token: check it in the integration.") from exc
        raise StairwayError(f"Can't reach Stairway ({exc}).") from exc
    except ValueError as exc:
        raise StairwayError("Stairway sent something that isn't JSON.") from exc
    if "error" in res:
        raise StairwayError(res["error"].get("message") or "Stairway error.")
    return res.get("result") or {}


def tool(integration, tool_name: str, /, timeout=30, **arguments) -> str:
    """Call a Stairway tool; its text result, or StairwayError when the tool failed. (Positional-only parameters:
    Stairway's own arguments include `name`.)"""
    result = call(integration, "tools/call", {"name": tool_name, "arguments": arguments}, timeout=timeout)
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    if result.get("isError"):
        raise StairwayError(text.removeprefix("Error: ") or f"{tool_name} failed.")
    return text


def tool_json(integration, tool_name: str, /, timeout=30, **arguments) -> dict:
    try:
        return json.loads(tool(integration, tool_name, timeout=timeout, **arguments))
    except ValueError as exc:
        raise StairwayError(f"{tool_name} didn't return JSON: update Stairway.") from exc
