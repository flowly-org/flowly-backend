"""ADHD mode for Flo: how it talks, how the brief is shaped, and the gentle nudges (check-ins, breaks, heads-ups)."""

import logging
import time
from datetime import timedelta

from django.utils import timezone

log = logging.getLogger("flowly.concierge")
_last_tick = 0.0


def active(user) -> bool:
    return bool(getattr(user, "adhd_mode", False))


def prompt(user) -> str:
    from accounts.focus import options

    o = options(user)
    checkin = o["check_in_minutes"] or 25
    return (
        "ADHD MODE (the user opted in; these rules win over any other length or style rule, the VOICE stays):\n"
        "• One thing at a time: lead with the single next action. At most 3 items in a list; offer the rest on request.\n"
        "• Short, concrete, verb-first lines. No preambles, no walls of text.\n"
        "• Big or vague tasks: offer to split them into 3-5 tiny steps of 5-15 minutes (add_steps); the first step "
        "must take under 5 minutes.\n"
        "• Time blindness: give concrete clock times and durations (\"it's 14:20, the call is in 40 min\").\n"
        "• No guilt about overdue or carried-over tasks: offer a fresh start (move, shrink or drop), never scold.\n"
        "• Celebrate finished tasks briefly and genuinely, then suggest the next small win.\n"
        "• If they're stuck choosing, pick one for them and say why in one line.\n"
        f"• Body doubling: when they're about to start something, offer to start the timer; you'll check in after {checkin} minutes."
    )


def brief_rules(user) -> str:
    from accounts.focus import options

    n = options(user)["today_limit"]
    return (
        f"ADHD mode: instead of the full plan, pick at most {n} tasks as \"Today's {n}\" (urgent, due and fixed-time "
        "things first), give the first tiny step of the first one, and mention everything else only as a count "
        "(\"+5 more can wait\"). Carried-over tasks are a fresh start, not a failure. Max 8 lines."
    )


def _nudge(user, key, title, body="", task=None):
    from . import notify
    from .models import FocusNudge, Notification

    _, created = FocusNudge.objects.get_or_create(user=user, key=key[:80])
    if not created:
        return False
    notify.create(user, Notification.Kind.FOCUS, title, body, task=task)
    return True


def minutes(n: int) -> str:
    h, m = divmod(int(n), 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def nudges(user, now=None) -> int:
    """Send the nudges due for one user; returns how many went out."""
    from accounts.focus import options
    from calendars.models import CalendarEvent
    from timetracking.models import TimeEntry

    from .brain import local_now

    now = now or timezone.now()
    o = options(user)
    sent = 0
    entry = TimeEntry.objects.filter(user=user, ended_at__isnull=True).select_related("task").first()
    what = (entry.task.title if entry and entry.task_id else (entry.description if entry else "")) or "your timer"
    if entry:
        elapsed = int((now - entry.started_at).total_seconds() // 60)
        if o["check_in_minutes"] and o["check_in_minutes"] <= elapsed < (o["hyperfocus_minutes"] or 10**6):
            sent += _nudge(user, f"checkin:{entry.pk}", f"⏱ {minutes(elapsed)} on “{what}”. How's it going?",
                           "Done? Mark it done and take the win. Stuck? Tell Flo and split it into a tiny next step. "
                           "Still flowing? Keep going.", task=entry.task)
        if o["hyperfocus_minutes"] and elapsed >= o["hyperfocus_minutes"]:
            n = elapsed // o["hyperfocus_minutes"]
            sent += _nudge(user, f"hyper:{entry.pk}:{n}", f"🌊 {minutes(elapsed)} on “{what}” without a pause.",
                           "Time to stand up, drink some water and look away from the screen for 5 minutes. "
                           "Forgot the timer running? Stop it and fix the time later.", task=entry.task)
    if o["heads_up_minutes"]:
        soon = CalendarEvent.objects.filter(user=user, all_day=False, start__gt=now,
                                            start__lte=now + timedelta(minutes=o["heads_up_minutes"])).exclude(prep="background")
        tz = local_now(user).tzinfo
        for event in soon[:3]:
            left = max(1, int((event.start - now).total_seconds() // 60))
            body = "Time to wrap up and get ready." + (f" Your timer on “{what}” is still running." if entry else "")
            sent += _nudge(user, f"event:{event.pk}:{event.start:%Y%m%d%H%M}",
                           f"🔔 In {left} min: {event.title or 'an event'} ({event.start.astimezone(tz):%H:%M})", body)
    return sent


def tick():
    """Worker: nudges for people with ADHD mode on (at most once a minute)."""
    global _last_tick
    if time.monotonic() - _last_tick < 60:
        return
    _last_tick = time.monotonic()
    from accounts.models import User

    for user in User.objects.filter(adhd_mode=True, is_active=True, is_bot=False):
        try:
            nudges(user)
        except Exception:
            log.exception("Focus nudges failed for %s", user.pk)


def prune(days=30):
    from .models import FocusNudge

    FocusNudge.objects.filter(created_at__lt=timezone.now() - timedelta(days=days)).delete()
