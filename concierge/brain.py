"""The concierge: an LLM with task tools, short-term chat history and long-term memory."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from integrations.http import DeliveryError

from .i18n import LLM_NAMES, t
from .llm import LLMUnavailable, Provider, chat
from .models import ConciergeMessage, ConciergeProfile
from .tools import Toolbox, describe

log = logging.getLogger("flowly.concierge")
HISTORY = 12
MAX_STEPS = 6


def profile_for(user) -> ConciergeProfile:
    profile, _ = ConciergeProfile.objects.get_or_create(user=user)
    return profile


def provider_for(user) -> Provider | None:
    profile = profile_for(user)
    integration = profile.provider
    if (
        integration is not None
        and integration.enabled
        and integration.workspace.is_enterprise
        and integration.workspace.memberships.filter(user=user).exists()
    ):
        return Provider.from_integration(integration)
    return Provider.platform_default()


def availability(user) -> str | None:
    """None if the concierge can work for this user, otherwise why not."""
    if not user.has_pro:
        return t("pro_only", lang(user))
    if provider_for(user) is None:
        return "No AI provider is configured. Ask the admin to set OPENROUTER_API_KEY, or pick a workspace AI provider."
    return None


def local_now(user) -> datetime:
    try:
        return timezone.now().astimezone(ZoneInfo(profile_for(user).timezone))
    except Exception:
        return timezone.localtime()


def lang(user) -> str:
    """Language code for fixed texts: the chosen one, or English when on auto."""
    code = profile_for(user).language
    return code if code in LLM_NAMES else "en"


def language_rule(user) -> str:
    code = profile_for(user).language
    if code in LLM_NAMES:
        return f"Always write in {LLM_NAMES[code]}, whatever language the user writes in."
    return "Reply in the language the user writes in; if unclear, use English."


def system_prompt(user, now: datetime) -> str:
    from .personality import habits, prompt_for

    profile = profile_for(user)
    name = user.full_name or user.email.split("@")[0]
    memory = profile.memory.strip() or "(nothing yet)"
    try:
        observed = habits(user)
    except Exception:
        log.exception("Habits failed for %s", user.pk)
        observed = ""
    return (
        f"You are {profile.name or 'Flo'}, Flowly's concierge and the personal assistant of {name}. Flowly is a task "
        "planner where each day's tasks are grouped by project or tags across several workspaces.\n"
        f"{language_rule(user)}\n"
        f"Now: {now:%A %d %B %Y, %H:%M} ({profile.timezone}). Today's date is {now.date().isoformat()}.\n"
        "Use the tools to look up and change tasks; never invent tasks or ids. When you change something, say what you "
        "changed. Calendar events (list_events) are background, not tasks; when one needs preparation, offer to create "
        "a task with create_event_task, and record answers about events with mark_event. Write plain text for a chat app: "
        "short lines, '•' bullets, no markdown headings or tables. If something is ambiguous, ask one brief question.\n"
        f"VOICE: {prompt_for(profile)} Every reply, including briefs, must clearly sound like this personality: wording, "
        "greeting (or none), tone and length. Earlier messages may have used another voice; ignore their style.\n"
        f"What you remember about {name} (adapt to it; save lasting preferences with remember, drop wrong ones with "
        f"forget):\n{memory}"
        + (f"\nWhat their activity shows:\n{observed}" if observed else "")
    )


def run(user, messages, *, tools=True, max_tokens=2500) -> str:
    provider = provider_for(user)
    toolbox = Toolbox(user, provider, local_now(user).date())
    for _ in range(MAX_STEPS):
        result = chat(provider, messages, tools=Toolbox.openai_tools() if tools else None, max_tokens=max_tokens, user=user)
        message = result["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            return (message.get("content") or "").strip()
        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        for call in calls:
            output = toolbox.run(call["function"]["name"], call["function"].get("arguments") or "{}")
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output[:6000]})
    return "I got a bit lost doing that. Could you rephrase?"


def reply(user, text: str, channel="telegram") -> str:
    reason = availability(user)
    if reason:
        return reason
    now = local_now(user)
    history = list(ConciergeMessage.objects.filter(user=user).order_by("-created_at", "-id")[:HISTORY])[::-1]
    messages = [{"role": "system", "content": system_prompt(user, now)}]
    messages += [{"role": m.role, "content": m.content} for m in history]
    messages.append({"role": "user", "content": text})
    try:
        answer = run(user, messages)
    except LLMUnavailable as exc:
        return str(exc)
    except DeliveryError as exc:
        log.warning("Concierge LLM error for %s: %s %s", user.pk, exc, exc.body[:300])
        return t("error", lang(user))
    ConciergeMessage.objects.create(user=user, role="user", content=text[:4000], channel=channel)
    ConciergeMessage.objects.create(user=user, role="assistant", content=answer[:4000], channel=channel)
    return answer or "Done."


def plain_brief(user, now) -> str:
    toolbox = Toolbox(user, provider_for(user), now.date())
    tasks = toolbox._tasks().filter(assignee=user)
    today = now.date()
    open_today = [t for t in tasks.filter(planned_date__lte=today, status__is_done=False).order_by("planned_date", "position")]
    due = [t for t in tasks.filter(due_date=today, status__is_done=False)]
    code = lang(user)
    lines = [t("brief_greeting", code, date=now.date().isoformat())]
    if not open_today:
        lines.append(t("brief_empty", code))
    for task in open_today[:15]:
        extra = f" ({t('brief_carried', code)})" if task.planned_date and task.planned_date < today else ""
        lines.append(f"• {task.title} — {task.workspace.name}{extra}")
    if due:
        lines.append(f"{t('brief_due', code)}: " + ", ".join(task.title for task in due))
    from calendars import concierge as cal
    from calendars.sync import user_tz

    upcoming = cal.plain_lines(user, today, user_tz(user))
    if upcoming:
        lines.append(t("brief_prep", code) + ":")
        lines += upcoming
    return "\n".join(lines)


def brief(user) -> str:
    """Morning brief: today's plan across workspaces, written by the LLM (plain fallback)."""
    now = local_now(user)
    if availability(user):
        return plain_brief(user, now)
    toolbox = Toolbox(user, provider_for(user), now.date())
    data = toolbox.tool_list_tasks("today", include_overdue=True)
    due = toolbox._tasks().filter(assignee=user, due_date=now.date(), status__is_done=False)
    due_text = "\n".join(describe(t, now.date()) for t in due) or "none"
    from calendars import concierge as cal

    calendar = cal.brief_context(user, now.date(), now.tzinfo, toolbox._tasks())
    calendar_text = f"\n\n{cal.BRIEF_RULES}\n{calendar}" if calendar else ""
    try:
        from delegation.engine import suggestions

        delegable = suggestions(user, now.date())
    except Exception:
        log.exception("Delegation suggestions failed")
        delegable = []
    if delegable:
        calendar_text += ("\n\nTasks the AI agent could take on (offer them in one short line each; the user can reply "
                          "'yes' and you call delegate_task):\n" + "\n".join(
                              f"- #{t.id} {t.title}: {'can do it' if d.verdict == 'full' else 'can do part'}: {d.plan}" for t, d in delegable))
    messages = [
        {"role": "system", "content": system_prompt(user, now)},
        {"role": "user", "content": (
            "Write my morning brief for today. Start with a one-line greeting, then the plan grouped by workspace or "
            "project, urgent and carried-over items first, then anything due today. Max ~12 lines. Suggest a sensible "
            f"first task.\n\nTasks for today:\n{data}\n\nDue today:\n{due_text}{calendar_text}"
        )},
    ]
    try:
        text = run(user, messages, tools=False, max_tokens=2500)  # reasoning models think before answering
    except (DeliveryError, LLMUnavailable) as exc:
        log.warning("Brief fell back to plain text for %s: %s", user.pk, exc)
        return plain_brief(user, now)
    ConciergeMessage.objects.create(user=user, role="assistant", content=text[:4000], channel="brief")
    return text
