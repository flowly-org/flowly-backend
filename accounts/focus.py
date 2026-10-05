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
}
LIMITS = {"today_limit": (1, 10), "check_in_minutes": (0, 240), "hyperfocus_minutes": (0, 600), "heads_up_minutes": (0, 120), "capacity_minutes": (30, 1440)}


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
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number.") from None
        low, high = LIMITS[key]
        if not low <= v <= high:
            raise ValueError(f"{key} must be between {low} and {high}.")
        out[key] = v
    return out


def options(user) -> dict:
    return {**DEFAULTS, **(user.focus or {})}
