"""Which calendar events need preparation (tickets, travel, a trip away from home) and which are background.

Plain heuristics: they run at every sync, cost nothing and work without the concierge. The concierge (an LLM)
gets the flagged events plus the user's related tasks and does the judging and the asking.
"""

import re
from datetime import timedelta

from django.db.models import Q

REASONS = {
    "away": "away from home",
    "travel": "travel or lodging",
    "tickets": "tickets or a booking",
    "event": "registration or a gift",
    "manual": "marked by you",
}

KEYWORDS = {
    "travel": (
        "flight volo voli vuelo vol flug voo train treno tren zug comboio frecciarossa italo trenitalia ryanair easyjet "
        "airport aeroporto aeropuerto aéroport flughafen hotel airbnb booking check-in checkin trip viaggio viaje "
        "voyage reise viagem ferry traghetto transfer"
    ),
    "tickets": (
        "ticket tickets biglietto biglietti entrada entradas billet billets karten bilhete bilhetes concert concerto "
        "konzert concierto show spettacolo theatre theater teatro théâtre cinema festival match partita opera gig "
        "museum museo musée mostra exhibition ausstellung"
    ),
    "event": (
        "conference conferenza congress congresso convegno summit wedding matrimonio boda mariage hochzeit casamento "
        "birthday compleanno cumpleaños anniversaire geburtstag aniversário"
    ),
}
_PATTERNS = {
    reason: re.compile(r"(?<!\w)(" + "|".join(re.escape(w) for w in words.split()) + r")(?!\w)", re.IGNORECASE)
    for reason, words in KEYWORDS.items()
}
VIRTUAL = re.compile(r"https?://|meet\.google|zoom\.us|teams\.microsoft|webex|\bonline\b|\bvirtual\b|\bremote\b", re.IGNORECASE)
STOPWORDS = set(
    "with from that this have the and for con per del della dei degli alle allo agli una uno nel nella sul sulla "
    "und mit der die das les des pour avec dans para por com como meeting call riunione".split()
)


def home_places(home: str) -> list[str]:
    """'Bologna, Italy; Imola' → ['bologna', 'imola']: the first part of each place, i.e. the town."""
    places = []
    for part in re.split(r"[;\n]", home or ""):
        town = part.split(",")[0].strip().lower()
        if len(town) >= 2:
            places.append(town)
    return places


def is_away(location: str, home: str) -> bool:
    location = (location or "").strip()
    places = home_places(home)
    if not location or not places or VIRTUAL.search(location):
        return False
    if "," not in location:
        return False  # "Room 3", "Office", "Sala verde": a place nearby, not an address
    low = location.lower()
    return not any(place in low for place in places)


def classify(title: str, location: str, description: str, home: str, days: int = 1) -> str:
    """'' for background, otherwise a reason code."""
    text = f"{title}\n{location}"
    for reason in ("travel", "tickets", "event"):
        if _PATTERNS[reason].search(text):
            return reason
    if is_away(location, home):
        return "away"
    if days > 1 and location and not VIRTUAL.search(location):
        return "travel"  # a multi-day event somewhere: probably a trip
    return ""


def keywords(title: str) -> list[str]:
    words = re.findall(r"\w{4,}", (title or "").lower())
    return [w for w in dict.fromkeys(words) if w not in STOPWORDS][:4]


def upcoming(user, today, days=21):
    from .models import CalendarEvent

    qs = CalendarEvent.objects.filter(
        user=user, calendar__selected=True, end_date__gte=today, start_date__lte=today + timedelta(days=days)
    ).exclude(prep=CalendarEvent.Prep.BACKGROUND)
    return [e for e in qs.filter(Q(prep=CalendarEvent.Prep.NEEDED) | ~Q(auto_reason="")).order_by("start")]


def visible_tasks(user):
    from tasks.access import member_visible_q
    from tasks.models import Task

    return (
        Task.objects.filter(workspace__memberships__user=user)
        .filter(member_visible_q(user))
        .select_related("workspace", "status", "project")
    )


def related_tasks(event, user, limit=5):
    """Tasks linked to the event, then open tasks of the user whose title shares a word with it."""
    linked = list(visible_tasks(user).filter(calendar_events=event))
    found = []
    words = keywords(event.title)
    if words:
        cond = Q()
        for w in words:
            cond |= Q(title__icontains=w)
        found = list(
            visible_tasks(user)
            .filter(cond)
            .filter(Q(assignee=user) | Q(created_by=user))
            .exclude(pk__in=[t.pk for t in linked])
            .order_by("-created_at")[:limit]
        )
    return linked, found
