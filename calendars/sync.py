"""Mirror the user's selected calendars (Google, Microsoft, CalDAV, iCal links) into CalendarEvent for a rolling window."""

import logging
import time
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from integrations.http import DeliveryError

from . import google, prep, providers
from .models import Calendar, CalendarAccount, CalendarEvent

log = logging.getLogger("flowly.calendars")
PAST = timedelta(days=1)
AHEAD = timedelta(days=60)
EVERY = timedelta(minutes=15)
_last_check = 0.0


def user_tz(user) -> ZoneInfo:
    from concierge.models import ConciergeProfile

    profile = ConciergeProfile.objects.filter(user=user).first()
    try:
        return ZoneInfo(profile.timezone if profile else "Europe/Rome")
    except Exception:
        return ZoneInfo("UTC")


def parse_event(item: dict, tz) -> dict | None:
    if item.get("status") == "cancelled":
        return None
    for person in item.get("attendees") or []:
        if person.get("self") and person.get("responseStatus") == "declined":
            return None
    start, end = item.get("start") or {}, item.get("end") or {}
    if "date" in start:
        first = date.fromisoformat(start["date"])
        after = date.fromisoformat(end.get("date") or start["date"])
        last = max(first, after - timedelta(days=1))
        values = {
            "all_day": True,
            "start": datetime.combine(first, datetime.min.time(), tzinfo=dt_timezone.utc),
            "end": datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=dt_timezone.utc),
            "start_date": first, "end_date": last,
        }
    elif "dateTime" in start:
        s = parse_datetime(start["dateTime"])
        e = parse_datetime(end.get("dateTime") or start["dateTime"])
        if s is None or e is None:
            return None
        values = {
            "all_day": False, "start": s, "end": e,
            "start_date": s.astimezone(tz).date(),
            "end_date": max(s, e - timedelta(microseconds=1)).astimezone(tz).date(),
        }
    else:
        return None
    return {
        **values,
        "title": (item.get("summary") or "(busy)")[:500],
        "description": (item.get("description") or "")[:2000],
        "location": (item.get("location") or "")[:500],
        "html_link": (item.get("htmlLink") or "")[:1000],
    }


def classify(event: CalendarEvent, home: str):
    days = (event.end_date - event.start_date).days + 1
    event.auto_reason = prep.classify(event.title, event.location, event.description, home, days)


def reclassify(user):
    """Run after the user's home changes."""
    for event in CalendarEvent.objects.filter(user=user, end_date__gte=timezone.localdate() - PAST):
        before = event.auto_reason
        classify(event, user.home_location)
        if event.auto_reason != before:
            event.save(update_fields=["auto_reason"])


def sync_calendars(account: CalendarAccount):
    seen = []
    for item in providers.calendars(account):
        cal, created = Calendar.objects.get_or_create(
            account=account, external_id=item["id"][:255],
            # Your own calendars by default; holidays, birthdays and others' calendars are opt-in.
            defaults={"name": item["name"][:255], "primary": item["primary"], "selected": item["primary"] or item["mine"]},
        )
        cal.name = item["name"][:255]
        cal.color = (item.get("color") or "")[:20]
        cal.primary = item["primary"]
        cal.save()
        seen.append(cal.pk)
    account.calendars.exclude(pk__in=seen).delete()


def sync_account(account: CalendarAccount) -> int:
    """Returns the number of events in the window; records failures on the account."""
    user = account.user
    tz = user_tz(user)
    now = timezone.now()
    lo, hi = now - PAST, now + AHEAD
    count = 0
    try:
        sync_calendars(account)
        for cal in account.calendars.filter(selected=True):
            items = providers.events(account, cal.external_id, lo, hi, tz)
            with transaction.atomic():
                kept = []
                for external_id, values in items:
                    event, _ = CalendarEvent.objects.update_or_create(
                        calendar=cal, external_id=external_id[:255], defaults={**values, "user": user}
                    )
                    classify(event, user.home_location)
                    event.save(update_fields=["auto_reason"])
                    kept.append(event.pk)
                # Gone from Google (deleted, declined, moved out of the window).
                cal.events.filter(end__gte=lo, start__lte=hi).exclude(pk__in=kept).delete()
            count += len(kept)
        CalendarEvent.objects.filter(calendar__account=account, calendar__selected=False).delete()
    except DeliveryError as exc:
        account.last_error = str(exc)[:500]
        if not exc.retry:
            account.status = CalendarAccount.Status.ERROR
        account.last_synced_at = now
        account.save(update_fields=["last_error", "status", "last_synced_at"])
        log.warning("Calendar sync failed for account %s: %s", account.pk, exc)
        return count
    account.status = CalendarAccount.Status.OK
    account.last_error = ""
    account.last_synced_at = now
    account.save(update_fields=["status", "last_error", "last_synced_at"])
    return count


def tick():
    """Called from the worker loop: sync accounts that are due, a few at a time."""
    global _last_check
    if time.monotonic() - _last_check < 30:
        return
    _last_check = time.monotonic()
    live = [p for p in CalendarAccount.Provider.values if providers.enabled(p)]
    due = CalendarAccount.objects.filter(status=CalendarAccount.Status.OK, provider__in=live).filter(
        Q(last_synced_at__lt=timezone.now() - EVERY) | Q(last_synced_at__isnull=True))
    for account in due.select_related("user").order_by("last_synced_at")[:3]:
        try:
            sync_account(account)
        except Exception:
            log.exception("Calendar sync crashed for account %s", account.pk)
