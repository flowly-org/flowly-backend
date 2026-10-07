"""How full a day is: estimated minutes of my open tasks vs. the time left after calendar events."""

from datetime import datetime, time, timedelta

from django.db.models import Q, Sum
from django.utils import timezone

from .access import member_visible_q
from .models import Task

DEFAULT_CAPACITY = 8 * 60


def busy_minutes(user, day, tz) -> int:
    """Minutes of timed calendar events that day, with the travel to and from them (overlaps merged)."""
    from calendars import travel
    from calendars.models import CalendarEvent

    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    margin = timedelta(hours=8)  # events just outside the day whose travel reaches into it
    events = CalendarEvent.objects.filter(user=user, all_day=False, start__lt=end + margin, end__gt=start - margin).exclude(prep="background")
    spans = sorted((max(s, start), min(e, end)) for s, e in travel.spans(events, user) if s < end and e > start)
    total, cur_s, cur_e = 0, None, None
    for s, e in spans:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return int(total // 60)


def day_load(user, day=None) -> dict:
    from calendars.sync import user_tz

    tz = user_tz(user)
    today = timezone.now().astimezone(tz).date()
    day = day or today
    cond = Q(planned_date=day)
    if day == today:
        cond |= Q(planned_date__lt=day)  # carried over
    tasks = (Task.objects.filter(workspace__memberships__user=user, assignee=user, status__is_done=False)
             .filter(member_visible_q(user)).filter(cond).distinct())
    estimated = tasks.aggregate(m=Sum("estimate_minutes"))["m"] or 0
    capacity = (user.focus or {}).get("capacity_minutes") or DEFAULT_CAPACITY
    busy = busy_minutes(user, day, tz)
    return {
        "date": day.isoformat(),
        "tasks": tasks.count(),
        "estimated_minutes": estimated,
        "unestimated": tasks.filter(estimate_minutes__isnull=True).count(),
        "busy_minutes": busy,
        "capacity_minutes": capacity,
        "free_minutes": max(0, capacity - busy),
        "over_minutes": max(0, estimated - max(0, capacity - busy)),
    }
