"""ADHD mode options. Everything is opt-in and adjustable; 0 turns a nudge off."""

DEFAULTS = {
    #: How many tasks "Today's N" shows before the rest is folded away.
    "today_limit": 3,
    #: Minutes after starting a timer when Flo checks in (body doubling).
    "check_in_minutes": 25,
    #: Minutes of continuous tracking before a break nudge (hyperfocus / forgotten timer), repeated.
    "hyperfocus_minutes": 90,
    #: Minutes before a calendar event for a heads-up to wrap up.
    "heads_up_minutes": 10,
    #: A small celebration when a task is done.
    "celebrate": True,
    #: Less motion and fewer details on cards.
    "calm": False,
    #: Working minutes in a day, for the day's load (estimates vs. free time). Not ADHD-specific.
    "capacity_minutes": 480,
    #: Working hours, for time blocking (free slots, auto-plan). Not ADHD-specific.
    "day_start": "09:00",
    "day_end": "18:00",
    #: A notification when a time block starts.
    "block_reminders": True,
    #: Minutes to reach an appointment in your home town (twice that out of town); busy time and "leave by".
    "transfer_minutes": 45,
    #: Flo's briefing before appointments, on Telegram: off, in_person (events with a place) or all (every timed event).
    "appointment_briefs": "off",
    #: How many minutes before you need to leave the briefing arrives.
    "appointment_lead_minutes": 15,
}
#: Settings that take one of a few words.
CHOICES = {"appointment_briefs": ("off", "in_person", "all")}
LIMITS = {"today_limit": (1, 10), "check_in_minutes": (0, 240), "hyperfocus_minutes": (0, 600), "heads_up_minutes": (0, 120), "capacity_minutes": (30, 1440), "transfer_minutes": (0, 240), "appointment_lead_minutes": (0, 180)}


def clean(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Must be an object.")
    out = {}
    for key, default in DEFAULTS.items():
        if key not in value:
            continue
        v = value[key]
        if isinstance(default, bool):
            out[key] = bool(v)
            continue
        if key in CHOICES:
            if v not in CHOICES[key]:
                raise ValueError(f"{key} must be one of: {', '.join(CHOICES[key])}.")
            out[key] = v
            continue
        if isinstance(default, str):
            import re

            if not isinstance(v, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                raise ValueError(f"{key} must be a time like 09:00.")
            out[key] = v
            continue
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number.") from None
        low, high = LIMITS[key]
        if not low <= v <= high:
            raise ValueError(f"{key} must be between {low} and {high}.")
        out[key] = v
    if "day_start" in out and "day_end" in out and out["day_start"] >= out["day_end"]:
        raise ValueError("The working day must end after it starts.")
    return out


def options(user) -> dict:
    return {**DEFAULTS, **(user.focus or {})}
