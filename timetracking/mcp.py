"""Time tracking tools for the MCP server."""

from datetime import date, timedelta

from django.utils import timezone

from mcpserver.tools import register
from workspaces.models import Workspace

from . import services
from .models import TimeEntry


def _fmt(seconds):
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h{m:02d}m"


def _workspace_for(user, task_id=None, workspace=""):
    from tasks.access import member_visible_q
    from tasks.models import Task

    if task_id:
        task = Task.objects.filter(workspace__memberships__user=user).filter(member_visible_q(user)).filter(pk=int(task_id)).first()
        if task is None:
            raise LookupError(f"No task #{task_id} you can access.")
        return task.workspace, task
    qs = Workspace.objects.filter(memberships__user=user)
    ws = (qs.filter(name__iexact=workspace).first() if workspace else None) or qs.filter(name__iexact="Personal").first() or qs.first()
    if ws is None:
        raise LookupError("No workspace.")
    return ws, None


def start_timer(user, task_id=None, description="", workspace=""):
    ws, task = _workspace_for(user, task_id, workspace)
    services.start_timer(user, ws, task, description)
    return f"Timer started on {task.title if task else description or 'untitled work'} ({ws.name})."


def stop_timer(user):
    entry = services.stop_timer(user)
    if entry is None:
        return "No timer was running."
    what = entry.task.title if entry.task_id else entry.description or "untitled work"
    return f"Stopped: {_fmt(entry.duration_seconds)} on {what}."


def log_time(user, minutes, task_id=None, description="", day="today", workspace=""):
    minutes = int(minutes)
    if not 1 <= minutes <= 24 * 60:
        raise ValueError("minutes must be between 1 and 1440.")
    ws, task = _workspace_for(user, task_id, workspace)
    on = timezone.localdate() if day == "today" else (timezone.localdate() - timedelta(days=1) if day == "yesterday" else date.fromisoformat(day))
    start = timezone.make_aware(timezone.datetime.combine(on, timezone.datetime.min.time()).replace(hour=9))
    project = task.project if task else None
    TimeEntry.objects.create(workspace=ws, user=user, task=task, project=project, description=description[:500],
                             started_at=start, ended_at=start + timedelta(minutes=minutes), duration_seconds=minutes * 60,
                             billable=services.default_billable(ws, project))
    return f"Logged {_fmt(minutes * 60)} on {task.title if task else description or 'work'} for {on} ({ws.name})."


def time_report(user, start="", end=""):
    s = date.fromisoformat(start) if start else timezone.localdate() - timedelta(days=timezone.localdate().weekday())
    e = date.fromisoformat(end) if end else timezone.localdate()
    entries = TimeEntry.objects.filter(user=user, started_at__date__gte=s, started_at__date__lte=e).select_related("task", "project", "workspace")
    totals = {}
    for entry in entries:
        key = f"{entry.workspace.name} / {entry.project.name if entry.project_id else 'No project'}"
        totals[key] = totals.get(key, 0) + entry.elapsed_seconds()
    if not totals:
        return f"No time tracked between {s} and {e}."
    lines = [f"Time from {s} to {e}:"] + [f"• {k}: {_fmt(v)}" for k, v in sorted(totals.items(), key=lambda kv: -kv[1])]
    lines.append(f"Total: {_fmt(sum(totals.values()))}")
    running = services.running_entry(user)
    if running:
        lines.append(f"(Timer running for {_fmt(running.elapsed_seconds())})")
    return "\n".join(lines)


register("start_timer", "Start timer", "Start tracking time on a task (stops any running timer).",
         {"type": "object", "properties": {"task_id": {"type": "integer"}, "description": {"type": "string"},
                                           "workspace": {"type": "string", "description": "Used when there's no task."}}},
         start_timer)
register("stop_timer", "Stop timer", "Stop the running timer.", {"type": "object", "properties": {}}, stop_timer)
register("log_time", "Log time", "Record time already spent on a task.",
         {"type": "object", "required": ["minutes"], "properties": {
             "minutes": {"type": "integer"}, "task_id": {"type": "integer"}, "description": {"type": "string"},
             "day": {"type": "string", "description": "today, yesterday or YYYY-MM-DD"}, "workspace": {"type": "string"}}},
         log_time)
register("time_report", "Time report", "How much time the user tracked, per workspace and project (default: this week).",
         {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}}, time_report, read_only=True)
