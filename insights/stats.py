"""Numbers about work: for a person (across workspaces), a workspace or a project, over a period."""

import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.db.models import Q, Sum

from tasks.access import member_visible_q
from tasks.models import Task
from timetracking.models import TimeEntry


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def scope_tasks(viewer, *, user=None, workspace=None, project=None):
    """Tasks the viewer may see, for the scope."""
    qs = Task.objects.filter(workspace__memberships__user=viewer).filter(member_visible_q(viewer))
    if project is not None:
        qs = qs.filter(project=project)
    elif workspace is not None:
        qs = qs.filter(workspace=workspace)
    if user is not None:
        qs = qs.filter(Q(assignee=user) | Q(assignee__isnull=True, created_by=user))
    return qs.distinct()


def scope_entries(viewer, *, user=None, workspace=None, project=None):
    qs = TimeEntry.objects.filter(workspace__memberships__user=viewer, ended_at__isnull=False)
    if project is not None:
        qs = qs.filter(project=project)
    elif workspace is not None:
        qs = qs.filter(workspace=workspace)
    if user is not None:
        qs = qs.filter(user=user)
    return qs.distinct()


def compute(viewer, lo: date, hi: date, tz, today: date, **scope) -> dict:
    """lo..hi inclusive (whole weeks are best)."""
    start = datetime.combine(lo, time.min, tzinfo=tz)
    end = datetime.combine(hi + timedelta(days=1), time.min, tzinfo=tz)
    tasks = scope_tasks(viewer, **scope)
    entries = scope_entries(viewer, **scope).filter(started_at__gte=start, started_at__lt=end)
    done = list(tasks.filter(completed_at__gte=start, completed_at__lt=end, status__is_done=True)
                .select_related("project", "workspace", "assignee"))
    created = tasks.filter(created_at__gte=start, created_at__lt=end)

    weeks = []
    w = monday(lo)
    while w <= hi:
        weeks.append(w)
        w += timedelta(days=7)
    series = {wk.isoformat(): {"week": wk.isoformat(), "created": 0, "completed": 0, "minutes": 0} for wk in weeks}

    def week_of(dt):
        return monday(dt.astimezone(tz).date()).isoformat()

    for t in created.only("created_at"):
        if week_of(t.created_at) in series:
            series[week_of(t.created_at)]["created"] += 1
    for t in done:
        if week_of(t.completed_at) in series:
            series[week_of(t.completed_at)]["completed"] += 1
    for e in entries.only("started_at", "duration_seconds"):
        if week_of(e.started_at) in series:
            series[week_of(e.started_at)]["minutes"] += round(e.duration_seconds / 60)

    by_project = defaultdict(lambda: {"completed": 0, "minutes": 0})
    names = {}
    for t in done:
        key = t.project_id or -t.workspace_id
        names[key] = (t.project.name if t.project_id else t.workspace.name, t.project.color if t.project_id else t.workspace.color)
        by_project[key]["completed"] += 1
    for row in entries.values("project_id", "project__name", "project__color", "workspace_id", "workspace__name", "workspace__color").annotate(s=Sum("duration_seconds")):
        key = row["project_id"] or -row["workspace_id"]
        names.setdefault(key, (row["project__name"] or row["workspace__name"], row["project__color"] or row["workspace__color"]))
        by_project[key]["minutes"] += round((row["s"] or 0) / 60)

    by_person = defaultdict(lambda: {"completed": 0, "open": 0, "minutes": 0})
    people = {}
    if "user" not in scope or scope.get("user") is None:
        for t in done:
            if t.assignee_id:
                people[t.assignee_id] = t.assignee.full_name or t.assignee.email
                by_person[t.assignee_id]["completed"] += 1
        for t in tasks.filter(status__is_done=False, assignee__isnull=False).select_related("assignee"):
            people[t.assignee_id] = t.assignee.full_name or t.assignee.email
            by_person[t.assignee_id]["open"] += 1
        for row in entries.values("user_id", "user__full_name", "user__email").annotate(s=Sum("duration_seconds")):
            people.setdefault(row["user_id"], row["user__full_name"] or row["user__email"])
            by_person[row["user_id"]]["minutes"] += round((row["s"] or 0) / 60)

    open_tasks = tasks.filter(status__is_done=False)
    with_due = [t for t in done if t.due_date]
    on_time = sum(1 for t in with_due if t.completed_at.astimezone(tz).date() <= t.due_date)
    cycle = [(t.completed_at - t.created_at).total_seconds() / 86400 for t in done if t.created_at]
    # Estimates vs actual: done tasks with an estimate and tracked time.
    estimated = [t for t in done if t.estimate_minutes]
    tracked = dict(TimeEntry.objects.filter(task__in=estimated, ended_at__isnull=False).values_list("task_id").annotate(s=Sum("duration_seconds")))
    pairs = [(t.estimate_minutes, tracked[t.id] / 60) for t in estimated if tracked.get(t.id)]
    status_counts = defaultdict(int)
    for name in open_tasks.values_list("status__name", flat=True):
        status_counts[name] += 1

    return {
        "from": lo.isoformat(), "to": hi.isoformat(),
        "totals": {
            "completed": len(done), "created": created.count(),
            "minutes": round(sum(e.duration_seconds for e in entries.only("duration_seconds")) / 60),
            "open": open_tasks.count(),
            "overdue": open_tasks.filter(due_date__lt=today).count(),
            "carried_over": open_tasks.filter(planned_date__lt=today).count(),
            "on_time_rate": round(100 * on_time / len(with_due)) if with_due else None,
            "cycle_days": round(statistics.median(cycle), 1) if cycle else None,
            "estimate_ratio": round(sum(a for _, a in pairs) / sum(e for e, _ in pairs), 2) if pairs else None,
            "estimate_samples": len(pairs),
        },
        "weeks": list(series.values()),
        "projects": sorted(({"name": names[k][0], "color": names[k][1] or "", **v} for k, v in by_project.items()),
                           key=lambda r: (-r["minutes"], -r["completed"]))[:12],
        "people": sorted(({"id": k, "name": people.get(k, "?"), **v} for k, v in by_person.items()),
                         key=lambda r: (-r["completed"], -r["open"]))[:20],
        "statuses": [{"name": k, "count": v} for k, v in sorted(status_counts.items(), key=lambda kv: -kv[1])],
    }
