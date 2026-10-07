"""Time blocking: free slots in the working day (around meetings and other blocks) and an automatic day plan."""

from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from .models import CalendarEvent, TimeBlock

DEFAULT_MINUTES = 30
STEP = 15  # minutes: blocks start on the quarter hour


def working_hours(user, day, tz):
    from accounts.focus import options

    o = options(user)
    start = datetime.combine(day, time.fromisoformat(o["day_start"]), tzinfo=tz)
    end = datetime.combine(day, time.fromisoformat(o["day_end"]), tzinfo=tz)
    return start, end


def busy(user, lo, hi):
    """Merged busy spans (timed events that aren't marked background, with travel to and from them, and time
    blocks) within [lo, hi)."""
    from . import travel

    margin = timedelta(hours=8)
    events = CalendarEvent.objects.filter(user=user, all_day=False, calendar__selected=True,
                                          start__lt=hi + margin, end__gt=lo - margin).exclude(prep="background")
    spans = [(s, e) for s, e in travel.spans(events, user) if s < hi and e > lo]
    spans += [(b.start, b.end) for b in TimeBlock.objects.filter(user=user, start__lt=hi, end__gt=lo)]
    spans = sorted((max(s, lo), min(e, hi)) for s, e in spans)
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def ceil_step(dt):
    minutes = (dt.minute + STEP - 1) // STEP * STEP
    return dt.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)


def free_slots(user, day, tz, *, from_now=True, min_minutes=STEP):
    lo, hi = working_hours(user, day, tz)
    now = timezone.now()
    if from_now and lo < now < hi:
        lo = ceil_step(now.astimezone(tz))
    elif from_now and now >= hi:
        return []
    slots, cursor = [], lo
    for s, e in busy(user, lo, hi):
        if (s - cursor).total_seconds() >= min_minutes * 60:
            slots.append((cursor, s))
        cursor = max(cursor, ceil_step(e))
    if (hi - cursor).total_seconds() >= min_minutes * 60:
        slots.append((cursor, hi))
    return slots


def queue(user, day, today):
    """My open tasks for the day in the order to do them (as the "Now" card): due first, then priority, then plan."""
    from tasks.access import member_visible_q
    from tasks.models import Tag, Task

    cond = Q(planned_date=day)
    if day == today:
        cond |= Q(planned_date__lt=day)
    tasks = list(Task.objects.filter(workspace__memberships__user=user, assignee=user, status__is_done=False)
                 .filter(member_visible_q(user)).filter(cond).exclude(blocked_by_links__depends_on__status__is_done=False)
                 .select_related("priority", "workspace", "project").distinct())
    ranks = {}
    for t in Tag.objects.filter(group__kind="priority", workspace__in={t.workspace_id for t in tasks}).order_by("position", "id"):
        ranks.setdefault(t.workspace_id, []).append(t.id)

    def key(t):
        prio = ranks.get(t.workspace_id, [])
        return (0 if t.due_date and t.due_date <= day else 1,
                prio.index(t.priority_id) if t.priority_id in prio else len(prio) + 1,
                t.planned_date or day, t.position)

    return sorted(tasks, key=key)


def plan(user, day, *, apply=False):
    """Fit the day's unblocked tasks into the free slots (estimate or 30 min each). Returns the proposal."""
    from calendars.sync import user_tz

    tz = user_tz(user)
    today = timezone.now().astimezone(tz).date()
    lo, hi = working_hours(user, day, tz)
    already = set(TimeBlock.objects.filter(user=user, start__lt=hi + timedelta(hours=6), end__gt=lo - timedelta(hours=6),
                                           task__isnull=False).values_list("task_id", flat=True))
    slots = [list(s) for s in free_slots(user, day, tz, from_now=day == today)]
    placed, left = [], []
    for task in queue(user, day, today):
        if task.id in already:
            continue
        minutes = task.estimate_minutes or DEFAULT_MINUTES
        need = timedelta(minutes=minutes)
        for slot in slots:
            if slot[1] - slot[0] >= need:
                start, end = slot[0], slot[0] + need
                placed.append({"task": task.id, "title": task.title, "start": start, "end": end, "minutes": minutes})
                slot[0] = ceil_step(end)
                break
        else:
            left.append({"task": task.id, "title": task.title, "minutes": minutes})
    if apply:
        TimeBlock.objects.bulk_create([TimeBlock(user=user, task_id=p["task"], title=p["title"][:255], start=p["start"], end=p["end"]) for p in placed])
    return {"placed": placed, "unscheduled": left}


def block_data(b):
    task = b.task
    return {"id": b.id, "task": b.task_id, "title": task.title if task else b.title, "start": b.start, "end": b.end,
            "workspace": task.workspace_id if task else None, "project": task.project_id if task else None,
            "done": task.status.is_done if task else False}
