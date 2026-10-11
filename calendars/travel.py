"""Getting to appointments: travel time around in-person events, and the briefing Flo sends before you leave.

An event with an address in a home town takes `transfer_minutes` to reach (and to come back from); one in another
town at least twice that. People who opt in get the real driving time instead (calendars.routing), once it's known. Online events, events without an address and all-day events need no travel. The time
counts as busy in the day's load and in time blocking, and sets the "leave by" time.
"""

from datetime import timedelta
from urllib.parse import quote_plus

from . import prep

AWAY_FACTOR = 2


def kind(event, home: str) -> str:
    """'in_town', 'away' or '' (no travel: online, no address, or an all-day event)."""
    location = (event.location or "").strip()
    if event.all_day or not location or prep.VIRTUAL.search(location):
        return ""
    low = location.lower()
    if any(place in low for place in prep.home_places(home)):
        return "in_town"
    return "away" if prep.is_away(location, home) else ""


def driving(event, user, opts=None) -> int | None:
    """The real time by car (plus parking) when the user chose it and it's known for their current home."""
    from accounts.focus import options

    from . import routing

    if (opts or options(user))["travel_mode"] != "driving" or event.drive_minutes is None:
        return None
    home = routing.origin(user)
    if not home or event.drive_key != routing.route_key(home, event.location or ""):
        return None
    return routing.rounded(event.drive_minutes)


def minutes(event, user, opts=None) -> int:
    """Travel time to reach the event (0 when none is needed): the real drive when known, else the fixed estimate."""
    from accounts.focus import options

    opts = opts or options(user)
    k = kind(event, user.home_location)
    if not k:
        return 0
    real = driving(event, user, opts)
    if real is not None:
        return real
    base = opts["transfer_minutes"]
    return base if k == "in_town" else base * AWAY_FACTOR


def source(event, user, opts=None) -> str:
    """'driving' (a real route), 'fixed' (the estimate) or '' (no travel)."""
    if not kind(event, user.home_location):
        return ""
    return "driving" if driving(event, user, opts) is not None else "fixed"


def leave_at(event, user, opts=None):
    m = minutes(event, user, opts)
    return event.start - timedelta(minutes=m) if m else None


def spans(events, user):
    """(start, end) of each timed event, widened by the travel to and from it."""
    from accounts.focus import options

    opts = options(user)
    out = []
    for e in events:
        pad = timedelta(minutes=minutes(e, user, opts))
        out.append((e.start - pad, e.end + pad))
    return out


def in_person(event) -> bool:
    location = (event.location or "").strip()
    return bool(location) and not prep.VIRTUAL.search(location)


def map_link(location: str) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(location)}"


def briefing(event, user, tz) -> tuple[str, str]:
    """(title, body) of the message Flo sends before an appointment."""
    from accounts.focus import options

    opts = options(user)
    start, end = event.start.astimezone(tz), event.end.astimezone(tz)
    title = f"{event.title or 'Appointment'} at {start:%H:%M}"
    lines = [f"{start:%a %d %b}, {start:%H:%M}–{end:%H:%M}"]
    travel = minutes(event, user, opts)
    k = kind(event, user.home_location)
    if travel:
        leave = (event.start - timedelta(minutes=travel)).astimezone(tz)
        if source(event, user, opts) == "driving":
            lines.append(f"Leave by {leave:%H:%M} (about {travel} min by car, parking included).")
        elif k == "away":
            lines.append(f"Out of town: allow at least {travel} min to get there. Leave by {leave:%H:%M}.")
        else:
            lines.append(f"Leave by {leave:%H:%M} (about {travel} min to get there).")
    if event.location:
        lines.append(f"Where: {event.location}")
        if not prep.VIRTUAL.search(event.location):
            lines.append(f"Map: {map_link(event.location)}")
    if event.description:
        text = " ".join(event.description.split())
        lines.append(f"Notes: {text[:300]}{'…' if len(text) > 300 else ''}")
    linked = list(prep.visible_tasks(user).filter(calendar_events=event))
    if linked:
        lines.append("Tasks: " + "; ".join(f"{t.title} ({'done' if t.status.is_done else 'still open'})" for t in linked))
    if event.html_link:
        lines.append(event.html_link)
    return title, "\n".join(lines)


def due_briefings(user, now):
    """Events whose briefing time (leave time minus the lead) has come and that haven't started yet."""
    from accounts.focus import options

    from .models import CalendarEvent

    opts = options(user)
    mode = opts["appointment_briefs"]
    if mode == "off":
        return []
    lead = timedelta(minutes=opts["appointment_lead_minutes"])
    horizon = now + timedelta(minutes=opts["transfer_minutes"] * AWAY_FACTOR) + lead
    out = []
    events = (CalendarEvent.objects.filter(user=user, all_day=False, calendar__selected=True, start__gt=now, start__lte=horizon)
              .exclude(prep=CalendarEvent.Prep.BACKGROUND).select_related("calendar"))
    for e in events:
        if mode == "in_person" and not in_person(e):
            continue
        notify_at = e.start - timedelta(minutes=minutes(e, user, opts)) - lead
        if notify_at <= now:
            out.append(e)
    return out


def telegram_state(user):
    """The user asked for these briefings: send them on Telegram whenever Flo is linked (alerts setting or not)."""
    from concierge.models import ConciergeProfile, Notification

    profile = ConciergeProfile.objects.filter(user=user).first()
    ok = bool(profile and profile.enabled and profile.telegram_chat_id and user.has_pro)
    return Notification.Telegram.PENDING if ok else Notification.Telegram.NONE


def send_briefings(user, now=None) -> int:
    from django.utils import timezone

    from concierge import notify
    from concierge.models import FocusNudge, Notification

    from .sync import user_tz

    now = now or timezone.now()
    tz = user_tz(user)
    sent = 0
    for event in due_briefings(user, now):
        _, created = FocusNudge.objects.get_or_create(user=user, key=f"appt:{event.pk}:{event.start:%Y%m%d%H%M}"[:80])
        if not created:
            continue
        title, body = briefing(event, user, tz)
        notify.create(user, Notification.Kind.REMINDER, title, body, data={"event": event.pk}, telegram=telegram_state(user))
        sent += 1
    return sent


_last_tick = 0.0


def tick():
    """Worker: appointment briefings for people who turned them on (at most once a minute)."""
    import logging
    import time

    from accounts.models import User

    global _last_tick
    if time.monotonic() - _last_tick < 60:
        return
    _last_tick = time.monotonic()
    for user in User.objects.filter(is_active=True, is_bot=False, focus__appointment_briefs__in=["in_person", "all"]):
        try:
            send_briefings(user)
        except Exception:
            logging.getLogger("flowly.calendars").exception("Appointment briefings failed for %s", user.pk)
