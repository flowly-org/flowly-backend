"""Calendar context for the concierge: background for the day, and events that may need preparation."""

from datetime import timedelta

from . import prep
from .models import CalendarEvent

HORIZON_DAYS = 21


def when(event: CalendarEvent, tz) -> str:
    if event.all_day:
        if event.end_date > event.start_date:
            return f"{event.start_date:%a %d %b}–{event.end_date:%a %d %b}"
        return f"{event.start_date:%a %d %b} (all day)"
    s, e = event.start.astimezone(tz), event.end.astimezone(tz)
    return f"{s:%a %d %b %H:%M}–{e:%H:%M}"


def line(event: CalendarEvent, tz, user, tasks_qs=None) -> str:
    from . import travel

    bits = [f"E{event.id} {when(event, tz)}: {event.title}"]
    if event.location:
        bits.append(f"@ {event.location}")
    leave = travel.leave_at(event, user)
    if leave:
        bits.append(f"(leave by {leave.astimezone(tz):%H:%M}: about {travel.minutes(event, user)} min to get there"
                    f"{', out of town' if travel.kind(event, user.home_location) == 'away' else ''})")
    text = " ".join(bits)
    if event.needs_prep:
        linked, found = prep.related_tasks(event, user)
        if tasks_qs is not None:  # hidden workspaces stay hidden
            allowed = set(tasks_qs.values_list("pk", flat=True))
            linked = [t for t in linked if t.pk in allowed]
            found = [t for t in found if t.pk in allowed]
        fmt = lambda ts: ", ".join(f"#{t.id} {t.title} ({'done' if t.status.is_done else t.status.name})" for t in ts) or "none"
        text += (
            f" — may need: {prep.REASONS.get(event.reason, event.reason)}; "
            f"confirmed still on: {'yes' if event.confirmed_at else 'no'}; linked tasks: {fmt(linked)}"
        )
        if found:
            text += f"; possibly related tasks: {fmt(found)}"
    return text


def covered(event: CalendarEvent, user) -> bool:
    """Confirmed (if away) and every linked task done: nothing left to ask about."""
    linked = list(prep.visible_tasks(user).filter(calendar_events=event))
    if not linked or any(not t.status.is_done for t in linked):
        return False
    return event.reason != "away" or event.confirmed_at is not None


def today_events(user, today):
    return list(
        CalendarEvent.objects.filter(user=user, calendar__selected=True, start_date__lte=today, end_date__gte=today)
        .select_related("calendar")
        .order_by("all_day", "start")
    )


def prep_events(user, today):
    return [e for e in prep.upcoming(user, today, HORIZON_DAYS) if not covered(e, user)]


def brief_context(user, today, tz, tasks_qs=None) -> str:
    """Text for the morning-brief prompt, or "" when the user has no calendar."""
    if not user.calendar_accounts.exists():
        return ""
    day = today_events(user, today)
    needs = prep_events(user, today)
    home = user.home_location or "(not set: ask the user once where home is, so trips can be spotted)"
    out = [f"Home: {home}", "Calendar today (background, not tasks):"]
    out += [f"• {line(e, tz, user, tasks_qs)}" for e in day] or ["• nothing"]
    out.append(f"Events in the next {HORIZON_DAYS} days that may need preparation:")
    out += [f"• {line(e, tz, user, tasks_qs)}" for e in needs] or ["• none"]
    return "\n".join(out)


BRIEF_RULES = (
    "Calendar events are background, not tasks: mention today's events only where they shape the plan (e.g. "
    "little free time, a trip). For each event that may need preparation: if it has linked or clearly related "
    "tasks, say in a few words that it's covered (or what is still open); if nothing covers it, ask whether "
    "something is needed (tickets, travel, hotel, registration, a gift) and offer to create the task. For events "
    "away from home that aren't confirmed, ask whether they're still on. For today's appointments with a leave-by "
    "time, say when to leave. Refer to events by their title and date, "
    "never by their E-number. Keep this part to a few lines, most urgent first."
)


def plain_lines(user, today, tz) -> list[str]:
    needs = prep_events(user, today)
    return [f"• {e.title} — {when(e, tz)}" for e in needs[:6]]


def window_events(user, start, days):
    return list(
        CalendarEvent.objects.filter(
            user=user, calendar__selected=True, start_date__lte=start + timedelta(days=days - 1), end_date__gte=start
        ).select_related("calendar")
    )
