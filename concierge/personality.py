"""How Flo talks (personalities) and what it knows about the person (memory and observed habits)."""

import json
import logging
import re
from collections import Counter
from datetime import timedelta

from django.utils import timezone

log = logging.getLogger("flowly.concierge")

PERSONALITIES = {
    "friendly": {
        "label": "Friendly", "blurb": "Warm and upbeat, short and clear.",
        "sample": "Morning! Three things today: let's start with the ACME sync. You've got this.",
        "prompt": "Personality: friendly. Warm, upbeat and encouraging, but short. Light friendliness, no gushing.",
    },
    "coach": {
        "label": "Coach", "blurb": "Keeps you focused and accountable.",
        "sample": "Two tasks carried over. Pick one and finish it before lunch: which one?",
        "prompt": "Personality: a focused productivity coach. Push gently on priorities, point out carried-over and overdue "
                  "work, propose one next action and ask for a commitment; celebrate finished tasks in a few words.",
    },
    "butler": {
        "label": "Butler", "blurb": "Formal, discreet and impeccably polite.",
        "sample": "Good morning. Your day is in order; the ACME meeting comes first.",
        "prompt": "Personality: a discreet, formal English butler. Address the person formally (Sir/Madam or their "
                  "surname when known), open with a courteous greeting, use polished, understated phrasing (\"Certainly\", "
                  "\"If I may suggest…\"); no emojis, no slang, no exclamation marks.",
    },
    "minimal": {
        "label": "Minimal", "blurb": "Just the facts. No small talk.",
        "sample": "Today: ACME sync (high), hero copy, plumber.",
        "prompt": "Personality: minimal. No greeting, no sign-off, no small talk, no emojis, no full sentences when a "
                  "fragment will do. Terse bullets only; confirmations in at most one short line (e.g. \"Done: moved to Fri.\").",
    },
    "buddy": {
        "label": "Buddy", "blurb": "Casual and playful, with a bit of humour.",
        "sample": "Big day! ACME first ☕ then the hero copy. The plumber won't call himself 😄",
        "prompt": "Personality: a casual, playful friend. Light humour and the odd emoji, never at the expense of clarity.",
    },
    "calm": {
        "label": "Calm", "blurb": "Steady and reassuring, one thing at a time.",
        "sample": "One step at a time: start with the ACME sync. The rest can wait.",
        "prompt": "Personality: calm and reassuring. Ease overwhelm: suggest one thing at a time, acknowledge effort, "
                  "avoid pressure words.",
    },
}
DEFAULT = "friendly"
MEMORY_LIMIT = 4000
MAX_NOTES = 15


def prompt_for(profile) -> str:
    return PERSONALITIES.get(profile.personality, PERSONALITIES[DEFAULT])["prompt"]


def choices() -> list[dict]:
    return [{"key": k, **{f: v[f] for f in ("label", "blurb", "sample")}} for k, v in PERSONALITIES.items()]


# --- observed habits (no AI) ----------------------------------------------------------


def habits(user) -> str:
    """A few facts read from the last 30 days of activity, to adapt suggestions. Empty when there's little data."""
    from tasks.models import Task
    from timetracking.models import TimeEntry

    from .notify import local_today
    from .sync_tz import tz_for

    since = timezone.now() - timedelta(days=30)
    tz = tz_for(user)
    lines = []
    created = Counter(Task.objects.filter(created_by=user, created_at__gte=since).values_list("workspace__name", flat=True))
    if sum(created.values()) >= 5:
        ws, n = created.most_common(1)[0]
        lines.append(f"adds most tasks to “{ws}” ({n} of {sum(created.values())} in the last 30 days)")
    done = Task.objects.filter(assignee=user, completed_at__gte=since).values_list("completed_at", flat=True)
    if len(done) >= 5:
        days = {d.astimezone(tz).date() for d in done}
        lines.append(f"finishes about {len(done) / max(1, len(days)):.0f} tasks on a working day")
        hours = Counter(d.astimezone(tz).hour for d in done)
        peak = hours.most_common(1)[0][0]
        lines.append(f"finishes most tasks around {peak}:00")
    starts = list(TimeEntry.objects.filter(user=user, started_at__gte=since).values_list("started_at", flat=True))
    if len(starts) >= 5:
        first = {}
        for s in starts:
            local = s.astimezone(tz)
            first[local.date()] = min(first.get(local.date(), local), local)
        mins = sorted(t.hour * 60 + t.minute for t in first.values())
        median = mins[len(mins) // 2]
        lines.append(f"usually starts tracking time around {median // 60}:{median % 60:02d}")
    carried = Task.objects.filter(assignee=user, status__is_done=False, planned_date__lt=local_today(user)).count()
    if carried >= 3:
        lines.append(f"has {carried} unfinished tasks carried over from earlier days")
    return "\n".join(f"- {l}" for l in lines)


# --- learning from conversations --------------------------------------------------------

REFLECT_PROMPT = """You maintain a short memory about a person, for their personal assistant in a task planner.
Update the memory from the recent conversation. Keep only lasting, useful facts and preferences for planning their days,
e.g. working hours, how they like briefs and replies, recurring commitments, names of people, projects or places they
mention, how they name their workspaces. Keep what they explicitly asked to remember. Drop what's outdated or
contradicted. Never store sensitive data (health, passwords, money details, political or religious views) or one-off
details. At most {n} notes, each one short sentence, in the person's language.
Reply with JSON only: {{"memory": ["…", "…"]}}"""


def notes(text: str) -> list[str]:
    return [re.sub(r"^[-•*]\s*", "", l).strip() for l in (text or "").splitlines() if l.strip()]


def reflect(profile) -> bool:
    """Distil recent conversations into the memory. Returns True when it changed."""
    from integrations.http import DeliveryError

    from .brain import provider_for
    from .llm import LLMUnavailable, chat
    from .models import ConciergeMessage

    user = profile.user
    since = profile.memory_updated_at or (timezone.now() - timedelta(days=7))
    recent = list(ConciergeMessage.objects.filter(user=user, created_at__gt=since).exclude(channel="brief").order_by("created_at")[:60])
    if sum(1 for m in recent if m.role == "user") < 3:
        return False
    provider = provider_for(user)
    if provider is None:
        return False
    convo = "\n".join(f"{'Person' if m.role == 'user' else 'Assistant'}: {m.content[:500]}" for m in recent)
    messages = [
        {"role": "system", "content": REFLECT_PROMPT.format(n=MAX_NOTES)},
        {"role": "user", "content": f"Current memory:\n{profile.memory or '(empty)'}\n\nRecent conversation:\n{convo}"},
    ]
    try:
        result = chat(provider, messages, max_tokens=2000, user=user)
        match = re.search(r"\{.*\}", result["message"].get("content") or "", re.S)
        data = json.loads(match.group(0)) if match else {}
    except (LLMUnavailable, DeliveryError, ValueError) as exc:
        log.warning("Memory reflection failed for %s: %s", user.pk, exc)
        return False
    items = [str(n).strip()[:200] for n in (data.get("memory") or []) if str(n).strip()][:MAX_NOTES]
    profile.memory_updated_at = timezone.now()
    if not items:
        profile.save(update_fields=["memory_updated_at"])
        return False
    new = "\n".join(f"- {n}" for n in items)[:MEMORY_LIMIT]
    changed = new != profile.memory
    profile.memory = new
    profile.save(update_fields=["memory", "memory_updated_at"])
    return changed


def due_reflections(limit=5):
    """Profiles to reflect on now: learning on, Pro, last reflection over a day ago (or never), new chats since."""
    from django.db.models import Max, Q

    from .models import ConciergeProfile

    cutoff = timezone.now() - timedelta(days=1)
    qs = (ConciergeProfile.objects.filter(enabled=True, learn=True)
          .filter(Q(memory_updated_at__isnull=True) | Q(memory_updated_at__lt=cutoff))
          .annotate(last_msg=Max("user__concierge_messages__created_at"))
          .filter(last_msg__isnull=False).select_related("user"))
    out = []
    for p in qs[:50]:
        if p.user.has_pro and (p.memory_updated_at is None or p.last_msg > p.memory_updated_at):
            out.append(p)
        if len(out) >= limit:
            break
    return out
