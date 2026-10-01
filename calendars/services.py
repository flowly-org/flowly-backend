"""Event ↔ task operations shared by the API, the concierge and the MCP server."""

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from tasks.models import Task
from workspaces.models import Workspace

from . import prep
from .models import CalendarEvent


def decide(event: CalendarEvent, prep=None, confirmed=None):
    fields = []
    if prep is not None:
        if prep not in CalendarEvent.Prep.values:
            raise ValidationError({"prep": f"Choose one of {', '.join(CalendarEvent.Prep.values)}."})
        event.prep = prep
        fields.append("prep")
    if confirmed is not None:
        event.confirmed_at = timezone.now() if confirmed in (True, "true", "1", 1) else None
        fields.append("confirmed_at")
    if fields:
        event.save(update_fields=fields)
    return event


def link(event: CalendarEvent, user, task_id):
    task = prep.visible_tasks(user).filter(pk=int(task_id)).first()
    if task is None:
        raise ValidationError({"task": f"No task #{task_id} you can access."})
    event.tasks.add(task)
    return task


def _workspace(user, value):
    qs = Workspace.objects.filter(memberships__user=user)
    if value in (None, ""):
        ws = qs.filter(name__iexact="Personal").first() or qs.order_by("id").first()
    elif str(value).isdigit():
        ws = qs.filter(pk=int(value)).first()
    else:
        ws = qs.filter(name__iexact=str(value)).first() or qs.filter(name__icontains=str(value)).first()
    if ws is None:
        raise PermissionDenied("You aren't a member of that workspace.")
    return ws


def create_task(event: CalendarEvent, user, data: dict) -> Task:
    """Create a task (assigned to the user, planned before the event by default) and link it to the event."""
    from concierge import notify
    from integrations.events import emit, has_outgoing
    from tasks.serializers import TaskSerializer

    ws = _workspace(user, data.get("workspace"))
    payload = {
        "title": (data.get("title") or f"Prepare: {event.title}")[:255],
        "description": data.get("description") or _context(event),
        "assignee": user.id,
        "planned_date": data.get("planned_date", timezone.localdate().isoformat()),
        "due_date": data.get("due_date", event.start_date.isoformat()),
    }
    for key in ("project", "priority", "status", "visibility"):
        if data.get(key) not in (None, ""):
            payload[key] = data[key]
    serializer = TaskSerializer(data=payload, context={"workspace": ws})
    serializer.is_valid(raise_exception=True)
    task = serializer.save(workspace=ws, created_by=user)
    if has_outgoing(ws.id):
        emit(None, task)
    notify.task_changed(None, task, actor=user)
    event.tasks.add(task)
    return task


def _context(event: CalendarEvent) -> str:
    when = event.start_date.isoformat() if event.all_day else event.start.strftime("%Y-%m-%d %H:%M")
    bits = [f"For “{event.title}” on {when}"]
    if event.location:
        bits.append(f"at {event.location}")
    text = " ".join(bits) + "."
    return text + (f"\n{event.html_link}" if event.html_link else "")
