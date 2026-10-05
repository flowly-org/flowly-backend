"""iCalendar (RFC 5545) events → CalendarEvent values, with recurring events expanded in the sync window."""

from datetime import date, datetime, timedelta, timezone as dt_timezone

import icalendar
import recurring_ical_events


def to_values(component, tz) -> dict | None:
    """One VEVENT occurrence → the fields of CalendarEvent (None for cancelled ones)."""
    if str(component.get("STATUS", "")).upper() == "CANCELLED":
        return None
    start = component.get("DTSTART")
    if start is None:
        return None
    start = start.dt
    end = component.get("DTEND")
    end = end.dt if end is not None else None
    if end is None and component.get("DURATION") is not None:
        end = start + component.get("DURATION").dt
    if isinstance(start, datetime):
        start = start if start.tzinfo else start.replace(tzinfo=tz)
        end = end if isinstance(end, datetime) else start
        end = end if end.tzinfo else end.replace(tzinfo=tz)
        values = {"all_day": False, "start": start, "end": max(end, start),
                  "start_date": start.astimezone(tz).date(), "end_date": max(start, end - timedelta(microseconds=1)).astimezone(tz).date()}
    else:
        first = start
        after = end if isinstance(end, date) and not isinstance(end, datetime) else first + timedelta(days=1)
        last = max(first, after - timedelta(days=1))
        values = {"all_day": True, "start": datetime.combine(first, datetime.min.time(), tzinfo=dt_timezone.utc),
                  "end": datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=dt_timezone.utc),
                  "start_date": first, "end_date": last}
    return {
        **values,
        "title": str(component.get("SUMMARY") or "(busy)")[:500],
        "description": str(component.get("DESCRIPTION") or "")[:2000],
        "location": str(component.get("LOCATION") or "")[:500],
        "html_link": str(component.get("URL") or "")[:1000],
    }


def occurrences(ics_texts, lo, hi, tz) -> list[tuple[str, dict]]:
    """[(external id, values)] for every occurrence of every event in the window."""
    out = []
    for text in ics_texts:
        try:
            cal = icalendar.Calendar.from_ical(text)
            events = recurring_ical_events.of(cal).between(lo, hi)
        except Exception:  # one broken event must not break the whole calendar
            continue
        for ev in events:
            values = to_values(ev, tz)
            if values is None:
                continue
            uid = str(ev.get("UID") or values["title"])
            # Occurrences of a recurring event share the UID: tell them apart by their start.
            key = f"{uid}@{values['start'].astimezone(dt_timezone.utc):%Y%m%dT%H%M}"
            out.append((key[:255], values))
    return out
