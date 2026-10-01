"""Calendar tool for the MCP server (personal endpoint only: calendars belong to a person, not a workspace)."""

from mcpserver.tools import register


def list_calendar_events(user, day="today", days=7, prep_only=False, scope=None):
    if scope:
        raise ValueError("Calendars are personal: use the personal MCP endpoint (/api/mcp/).")
    from concierge.brain import profile_for
    from concierge.notify import local_today
    from concierge.tools import Toolbox

    profile_for(user)
    return Toolbox(user, provider=None, today=local_today(user)).tool_list_events(day=day, days=days, prep_only=prep_only)


register(
    "list_calendar_events",
    "List calendar events",
    "The user's calendar events (Google Calendar), as background for planning: they are not tasks. Events that may "
    "need preparation (tickets, travel, trips away from home) show the reason and the tasks linked to them.",
    {"type": "object", "properties": {
        "day": {"type": "string", "description": "First day: 'today', 'tomorrow' or YYYY-MM-DD. Default today."},
        "days": {"type": "integer", "description": "Number of days (default 7, max 60)."},
        "prep_only": {"type": "boolean", "description": "Only events that may need preparation in the next 3 weeks."},
    }},
    list_calendar_events,
    read_only=True,
)
