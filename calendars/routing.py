"""Real driving times to appointments (opt-in): Nominatim finds the places, OSRM the route by car.

Both are OpenStreetMap services: the public servers by default, or your own on-premises install (Admin → Settings →
Travel; private addresses are fine, and OSRM's profile name is configurable). On public servers the work happens in
the worker, a little at a time: at most one Nominatim request per second, every address cached, a handful of routes per minute.
Results are stored on the event (`drive_minutes`) together with the from/to they were computed for, so pages never
wait on the network and a changed home or location is recomputed. Admins can point both at their own servers.
"""

import hashlib
import logging
import math
import time
from datetime import timedelta
from urllib.parse import quote_plus

from django.utils import timezone

from instance import conf
from integrations.http import DeliveryError, send

from . import prep

log = logging.getLogger("flowly.calendars")
#: Days ahead whose appointments get a driving time.
HORIZON_DAYS = 14
#: Routes computed per worker tick (all users together), to stay a polite guest on public servers.
ROUTES_PER_TICK = 12
#: Minutes added to the drive: parking and walking to the door.
MARGIN = 5
#: Nominatim's usage policy: one request per second at most.
NOMINATIM_GAP = 1.1
#: A place that wasn't found is asked again after this long (maybe the address was fixed).
RETRY_MISSING = timedelta(days=7)

_last_nominatim = 0.0


class Unavailable(Exception):
    """The map service didn't answer: try again later (unlike "not found", which is an answer)."""


def origin(user) -> str:
    """Where trips start: the first of the user's homes."""
    return (user.home_location or "").split(";")[0].strip()


def route_key(home: str, location: str) -> str:
    return hashlib.sha1(f"{home.strip().lower()}|{location.strip().lower()}".encode()).hexdigest()[:20]


def headers():
    contact = conf.get("PUBLIC_URL") or "https://flowly.net"
    return {"User-Agent": f"Flowly/1.0 (+{contact}; travel times)", "Accept": "application/json"}


#: Degrees around home where a street name is looked for first (Nominatim's viewbox is a preference, not a limit).
NEAR = 0.6


def geocode(query: str, near=None):
    """(lat, lon) of a place, or None. Cached in GeoPlace, found or not. `near` (lat, lon) prefers matches around it,
    so "Via Rizzoli 1, Bologna" is the one in town and not a namesake street in the countryside."""
    from .models import GeoPlace

    global _last_nominatim
    key = " ".join(query.lower().split())[:500]
    if not key:
        return None
    place = GeoPlace.objects.filter(query=key).first()
    if place and (place.lat is not None or place.checked_at > timezone.now() - RETRY_MISSING):
        return (place.lat, place.lon) if place.lat is not None else None
    wait = NOMINATIM_GAP - (time.monotonic() - _last_nominatim)
    if wait > 0:
        time.sleep(wait)
    _last_nominatim = time.monotonic()
    base = conf.get("NOMINATIM_URL").rstrip("/")
    try:
        # Admin-configured server: a self-hosted Nominatim may well live on a private network.
        bias = f"&viewbox={near[1] - NEAR:.4f},{near[0] + NEAR:.4f},{near[1] + NEAR:.4f},{near[0] - NEAR:.4f}" if near else ""
        found = send("GET", f"{base}/search?format=jsonv2&limit=5&addressdetails=1&q={quote_plus(query)}{bias}",
                     headers=headers(), timeout=10, check_url=False).json()
    except (DeliveryError, ValueError) as exc:
        raise Unavailable(f"Nominatim: {exc}") from exc  # not cached: a network hiccup isn't an answer
    match = best_match(query, found)
    lat, lon = (float(match["lat"]), float(match["lon"])) if match else (None, None)
    GeoPlace.objects.update_or_create(query=key, defaults={"lat": lat, "lon": lon, "checked_at": timezone.now()})
    return (lat, lon) if lat is not None else None


#: Address parts that name a place someone lives in; county, state and country are left out on purpose (in Italy the
#: province is named after its capital, and "Bologna" must not match a street in another town of the province).
TOWN_KEYS = ("city", "town", "village", "hamlet", "suburb", "municipality", "city_district", "quarter", "neighbourhood")


def towns_in(query: str) -> set[str]:
    """Place names written after the street: "Via X 1, 40125 Bologna BO, Italy" → {"bologna", "italy"}."""
    out = set()
    for part in query.split(",")[1:]:
        words = [w for w in part.split() if not any(c.isdigit() for c in w) and not (len(w) == 2 and w.isupper())]
        if words:
            out.add(" ".join(words).lower())
    return out


def best_match(query: str, results: list):
    """The first result in the town the address names. None when every result is somewhere else: a namesake street
    in another town is worse than no answer (the fixed estimate then applies)."""
    if not results:
        return None
    towns = towns_in(query)
    if not towns:
        return results[0]
    for r in results:
        names = {str(v).lower() for k, v in (r.get("address") or {}).items() if k in TOWN_KEYS}
        if names & towns:
            return r
    return None


def drive_minutes(a, b) -> int | None:
    """Minutes by car from a to b ((lat, lon) pairs), or None when there's no route."""
    base = conf.get("OSRM_URL").rstrip("/")
    profile = (conf.get("OSRM_PROFILE") or "driving").strip("/")
    url = f"{base}/route/v1/{profile}/{a[1]:.6f},{a[0]:.6f};{b[1]:.6f},{b[0]:.6f}?overview=false"
    try:
        data = send("GET", url, headers=headers(), timeout=10, check_url=False).json()
    except DeliveryError as exc:
        if exc.status and 400 <= exc.status < 500 and exc.status != 429:
            return None  # OSRM answers 400 NoRoute for places it can't connect
        raise Unavailable(f"OSRM: {exc}") from exc
    except ValueError as exc:
        raise Unavailable("OSRM sent something that isn't JSON.") from exc
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    return math.ceil(data["routes"][0]["duration"] / 60)


def rounded(drive: int) -> int:
    """The drive plus parking, rounded up to 5 minutes (people plan in fives)."""
    return int(math.ceil((drive + MARGIN) / 5) * 5)


def needs_route(event, home: str) -> bool:
    location = (event.location or "").strip()
    if event.all_day or not location or prep.VIRTUAL.search(location) or not home:
        return False
    return event.drive_key != route_key(home, location)


def refresh(user, now=None, budget=ROUTES_PER_TICK) -> int:
    """Compute driving times for the user's upcoming in-person events that don't have a current one. Returns how
    many routes were asked (successful or not), so the caller can share the budget across users."""
    from .models import CalendarEvent

    now = now or timezone.now()
    home = origin(user)
    if not home:
        return 0
    events = (CalendarEvent.objects.filter(user=user, all_day=False, calendar__selected=True, start__gt=now,
                                           start__lte=now + timedelta(days=HORIZON_DAYS))
              .exclude(location="").exclude(prep="background").order_by("start"))
    from . import travel

    # Only events that get travel at all (an address in or out of town), not "Room 3" or "Office".
    todo = [e for e in events if travel.kind(e, user.home_location) and needs_route(e, home)][:budget]
    if not todo:
        return 0
    try:
        start = geocode(home)
        for event in todo:
            dest = geocode(event.location, near=start) if start else None
            minutes = drive_minutes(start, dest) if start and dest else None
            # Stored with what it was computed for; None means "no route" (fixed time applies).
            CalendarEvent.objects.filter(pk=event.pk).update(drive_minutes=minutes, drive_key=route_key(home, event.location))
    except Unavailable as exc:
        log.warning("Driving times paused for %s: %s", user.pk, exc)
    return len(todo)


_last_tick = 0.0


def tick():
    """Worker: driving times for people who chose them (at most once a minute, a few routes each time)."""
    from accounts.models import User

    global _last_tick
    if time.monotonic() - _last_tick < 60:
        return
    _last_tick = time.monotonic()
    budget = ROUTES_PER_TICK
    for user in User.objects.filter(is_active=True, is_bot=False, focus__travel_mode="driving").exclude(home_location=""):
        if budget <= 0:
            break
        try:
            budget -= refresh(user, budget=budget)
        except Exception:
            log.exception("Driving times failed for %s", user.pk)



def check() -> str:
    """Admin "Check": geocode a well-known address and route a short trip, so a self-hosted setup can be verified."""
    try:
        a = geocode("Piazza Maggiore, Bologna, Italy")
        b = geocode("Piazza del Nettuno, Bologna, Italy", near=a)
    except Unavailable as exc:
        raise RuntimeError(str(exc)) from exc
    if not a or not b:
        raise RuntimeError(f"Nominatim at {conf.get('NOMINATIM_URL')} answered but didn't find the test address (is Italy in its data?).")
    try:
        minutes = drive_minutes(a, (b[0] + 0.01, b[1] + 0.01))
    except Unavailable as exc:
        raise RuntimeError(str(exc)) from exc
    if minutes is None:
        raise RuntimeError(f"OSRM at {conf.get('OSRM_URL')} found no route (check the profile, now {conf.get('OSRM_PROFILE')!r}, and its map data).")
    return f"Nominatim and OSRM answer: a test trip takes {minutes} min by car."
