"""One interface over the calendar sources: list the calendars of an account, list events in a window."""

import requests

from integrations.http import USER_AGENT, DeliveryError, ensure_public_url

from . import caldav, google, ics, microsoft


def calendars(account) -> list[dict]:
    """[{"id", "name", "color", "primary", "mine"}] — "mine" calendars are selected by default."""
    if account.provider == "google":
        return [{"id": c["id"], "name": c.get("summaryOverride") or c.get("summary") or c["id"], "color": c.get("backgroundColor", ""),
                 "primary": bool(c.get("primary")), "mine": bool(c.get("primary")) or c.get("accessRole") == "owner"}
                for c in google.calendars(account)]
    if account.provider == "microsoft":
        return microsoft.calendars(account)
    if account.provider == "caldav":
        return [{**c, "primary": i == 0, "mine": True} for i, c in enumerate(caldav.Client.for_account(account).discover())]
    if account.provider == "ics":
        return [{"id": account.server_url, "name": account.username or "iCal", "color": "", "primary": True, "mine": True}]
    return []


def events(account, calendar_id: str, lo, hi, tz) -> list[tuple[str, dict]]:
    """[(external id, CalendarEvent values)] for the events in [lo, hi)."""
    if account.provider == "google":
        from .sync import parse_event

        return [(item["id"][:255], values) for item in google.events(account, calendar_id, lo, hi)
                if (values := parse_event(item, tz)) is not None]
    if account.provider == "microsoft":
        return microsoft.events(account, calendar_id, lo, hi, tz)
    if account.provider == "caldav":
        return ics.occurrences(caldav.Client.for_account(account).events(calendar_id, lo, hi), lo, hi, tz)
    if account.provider == "ics":
        return ics.occurrences([fetch_feed(calendar_id)], lo, hi, tz)
    return []


def fetch_feed(url: str, max_bytes=5 * 1024 * 1024) -> str:
    url = url.strip().replace("webcal://", "https://", 1)
    ensure_public_url(url)
    try:
        res = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT}, stream=True)
    except requests.RequestException as exc:
        raise DeliveryError(f"Couldn't fetch the calendar: {exc}") from exc
    if res.status_code >= 400:
        raise DeliveryError(f"The calendar link answered HTTP {res.status_code}.", status=res.status_code, retry=res.status_code >= 500)
    data = res.raw.read(max_bytes + 1, decode_content=True)
    if len(data) > max_bytes:
        raise DeliveryError("That calendar is too large (max 5 MB).", retry=False)
    text = data.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text[:2000]:
        raise DeliveryError("That address isn't an iCal (.ics) calendar.", retry=False)
    return text


def enabled(provider: str) -> bool:
    return {"google": google.enabled, "microsoft": microsoft.enabled}.get(provider, lambda: True)()
