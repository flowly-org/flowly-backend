"""MCP tool catalogue: the concierge Toolbox tools plus tools other apps register (e.g. time tracking)."""

import json

from concierge.notify import local_today
from concierge.tools import Toolbox

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}

TOOLBOX_TITLES = {
    "list_tasks": "List tasks",
    "list_workspaces": "List workspaces",
    "create_task": "Create task",
    "update_task": "Update task",
    "complete_task": "Complete task",
    "add_comment": "Comment on task",
}

TOOLS = [
    {
        "name": spec["name"],
        "title": TOOLBOX_TITLES[spec["name"]],
        "description": spec["description"],
        "inputSchema": spec["parameters"],
        "annotations": READ_ONLY if spec["name"].startswith("list_") else WRITE,
    }
    for spec in Toolbox.SCHEMA
    if spec["name"] in TOOLBOX_TITLES
]

#: name → handler(user, **arguments) -> str, for tools registered by other apps.
EXTRA = {}


def register(name, title, description, parameters, handler, read_only=False):
    TOOLS.append({"name": name, "title": title, "description": description, "inputSchema": parameters,
                  "annotations": READ_ONLY if read_only else WRITE})
    EXTRA[name] = handler


def run_tool(user, name: str, arguments: dict, scope=None) -> tuple[str, bool]:
    """Returns (text, is_error). `scope` limits everything to one workspace (workspace endpoints)."""
    if name in EXTRA:
        try:
            return EXTRA[name](user, scope=scope, **arguments), False
        except (ValueError, LookupError, TypeError) as exc:
            return f"Error: {exc}", True
    from concierge.brain import profile_for

    profile_for(user)
    out = Toolbox(user, provider=None, today=local_today(user), workspace_id=scope).run(name, json.dumps(arguments))
    return out, out.startswith(("Error:", "Unknown tool"))
