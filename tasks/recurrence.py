"""Recurring tasks: a repeat rule on the task; ticking it done creates the next occurrence, which takes the rule over.

Rule: {"freq": "daily" | "weekly" | "monthly" | "yearly", "interval": 1.., "weekdays": [0..6] (weekly, Monday = 0),
"after_done": false (true = count from the day it was done, not from the planned day), "until": "YYYY-MM-DD" | absent}.
"""

import calendar
import secrets
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

FREQS = ("daily", "weekly", "monthly", "yearly")
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def clean(rule):
    """Validate and normalise a rule; None/{} = no repetition. Raises ValueError."""
    if not rule:
        return None
    if not isinstance(rule, dict):
        raise ValueError("The repeat rule must be an object.")
    freq = rule.get("freq")
    if freq not in FREQS:
        raise ValueError(f"Repeat: freq must be one of {', '.join(FREQS)}.")
    try:
        interval = int(rule.get("interval") or 1)
    except (TypeError, ValueError):
        raise ValueError("Repeat: interval must be a number.") from None
    if not 1 <= interval <= 365:
        raise ValueError("Repeat: interval must be between 1 and 365.")
    out = {"freq": freq, "interval": interval}
    if freq == "weekly" and rule.get("weekdays"):
        days = sorted({int(d) for d in rule["weekdays"]})
        if any(d < 0 or d > 6 for d in days):
            raise ValueError("Repeat: weekdays go from 0 (Monday) to 6 (Sunday).")
        out["weekdays"] = days
    if rule.get("after_done"):
        out["after_done"] = True
    if rule.get("until"):
        try:
            out["until"] = date.fromisoformat(str(rule["until"])[:10]).isoformat()
        except ValueError:
            raise ValueError("Repeat: until must be a date (YYYY-MM-DD).") from None
    return out


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def next_date(rule: dict, base: date) -> date:
    """The first occurrence after `base`."""
    n = rule.get("interval", 1)
    freq = rule["freq"]
    if freq == "daily":
        return base + timedelta(days=n)
    if freq == "weekly":
        days = rule.get("weekdays")
        if not days:
            return base + timedelta(weeks=n)
        later = [d for d in days if d > base.weekday()]
        if later:  # another day of the same week
            return base + timedelta(days=later[0] - base.weekday())
        start_of_week = base - timedelta(days=base.weekday())
        return start_of_week + timedelta(weeks=n, days=days[0])
    if freq == "monthly":
        return add_months(base, n)
    return add_months(base, 12 * n)


def describe(rule) -> str:
    if not rule:
        return ""
    n, freq = rule.get("interval", 1), rule["freq"]
    unit = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}[freq]
    text = f"every {unit}" if n == 1 else f"every {n} {unit}s"
    if rule.get("weekdays"):
        text += " on " + ", ".join(WEEKDAY_NAMES[d] for d in rule["weekdays"])
    if rule.get("after_done"):
        text += " after done"
    if rule.get("until"):
        text += f" until {rule['until']}"
    return text


def _today_for(user):
    from calendars.sync import user_tz

    return timezone.now().astimezone(user_tz(user)).date() if user else timezone.localdate()


@transaction.atomic
def spawn_next(task, user=None):
    """The task was just ticked done: create its next occurrence (and move the rule to it). Returns it or None."""
    from .models import Reminder, Task

    rule = task.recurrence
    if not rule:
        return None
    anchor = task.planned_date or task.due_date
    base = _today_for(user or task.assignee) if rule.get("after_done") or not anchor else anchor
    nxt = next_date(rule, base)
    # A long-overdue occurrence shouldn't spawn another one in the past: catch up to today.
    today = _today_for(user or task.assignee)
    while not rule.get("after_done") and nxt < today:
        nxt = next_date(rule, nxt)
    if rule.get("until") and nxt > date.fromisoformat(rule["until"]):
        return None
    delta = nxt - (anchor or base)
    from .services import default_status

    clone = Task.objects.create(
        workspace=task.workspace, project=task.project, title=task.title, description=task.description,
        visibility=task.visibility, status=default_status(task.workspace), priority=task.priority,
        planned_date=(task.planned_date + delta) if task.planned_date else (nxt if not task.due_date else None),
        due_date=(task.due_date + delta) if task.due_date else None,
        assignee=task.assignee, created_by=task.created_by, position=task.position,
        checklist=[{**item, "done": False} for item in task.checklist or []],
        estimate_minutes=task.estimate_minutes, recurrence=rule, series=task.series or secrets.token_hex(6),
    )
    clone.tags.set(task.tags.all())
    now = timezone.now()
    for r in task.reminders.all():
        if r.remind_at + delta > now:
            Reminder.objects.get_or_create(task=clone, user=r.user, remind_at=r.remind_at + delta)
    # The rule now lives on the new occurrence; this one remembers it, to undo if the tick was a mistake.
    task.series = clone.series
    task.recurrence = {"spawned": clone.pk}
    Task.objects.filter(pk=task.pk).update(recurrence=task.recurrence, series=task.series)
    transaction.on_commit(lambda: _announce(clone, user))
    return clone


def _announce(clone, user):
    from concierge import notify
    from integrations.events import emit, has_outgoing

    if has_outgoing(clone.workspace_id):
        emit(None, clone)
    notify.task_changed(None, clone, actor=user)


def undo_spawn(task):
    """The task was unticked: if its next occurrence is still untouched, remove it and take the rule back."""
    from .models import Task

    spawned = (task.recurrence or {}).get("spawned")
    if not spawned:
        return False
    clone = Task.objects.filter(pk=spawned).select_related("status").first()
    if clone is None or clone.status.is_done or clone.comments.exists() or clone.time_entries.exists():
        return False
    task.recurrence = clone.recurrence
    clone.delete()
    Task.objects.filter(pk=task.pk).update(recurrence=task.recurrence)
    return True


def is_rule(value) -> bool:
    return bool(value) and "freq" in value
