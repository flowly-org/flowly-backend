import time

_last = 0.0


def tick():
    """Every minute: sprint rollover and standups."""
    global _last
    if time.monotonic() - _last < 60:
        return
    _last = time.monotonic()
    from .services import tick as run

    run()
