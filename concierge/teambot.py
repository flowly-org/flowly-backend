"""The team bot: the concierge in a group chat (Telegram group, Slack workspace) for one Flowly workspace.

Differences from the personal concierge:
- everything is limited to the linked workspace, and private tasks never show (whoever asks);
- people who linked their Flowly account can change things, as themselves; everyone else can only read;
- it runs on the workspace's own AI provider when it has one, else on Flowly's (the Enterprise plan covers it).
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone

from integrations.http import DeliveryError

from .llm import LLMUnavailable, Provider, chat, record_usage
from .models import TeamMessage
from .tools import Toolbox, describe

log = logging.getLogger("flowly.concierge")
HISTORY = 10
MAX_STEPS = 6
TEAM_TOOLS = {"list_tasks", "create_task", "update_task", "complete_task", "add_comment", "start_timer", "stop_timer"}


def provider_for(workspace) -> Provider | None:
    from integrations.models import Integration

    if workspace.is_enterprise:
        own = Integration.objects.filter(
            workspace=workspace, kind__in=["openrouter", "openai_compatible"], enabled=True
        ).order_by("-config__exclusive", "id").first()
        if own is not None:
            return Provider.from_integration(own)
    return Provider.platform_default()


def tz_of(integration) -> ZoneInfo:
    try:
        return ZoneInfo(integration.config.get("timezone") or "Europe/Rome")
    except Exception:
        return ZoneInfo("UTC")


def local_now(integration) -> datetime:
    return timezone.now().astimezone(tz_of(integration))


def reader(integration):
    """Identity used for reading when the person asking isn't linked: whoever set the chat up, or an owner."""
    ws = integration.workspace
    if integration.created_by_id and ws.memberships.filter(user_id=integration.created_by_id).exists():
        return integration.created_by
    m = ws.memberships.filter(role="owner").select_related("user").first()
    return m.user if m else None


def team_tasks(integration):
    from tasks.models import Task

    return (
        Task.objects.filter(workspace=integration.workspace)
        .exclude(visibility=Task.Visibility.PRIVATE)
        .select_related("status", "priority", "assignee", "project", "workspace")
    )


def brief(integration) -> str:
    """Today's team plan, without the LLM: open tasks for today (and carried over) by person, urgent first."""
    from .notify import is_urgent

    today = local_now(integration).date()
    ws = integration.workspace
    open_tasks = team_tasks(integration).filter(planned_date__lte=today, status__is_done=False).order_by("position")
    due = team_tasks(integration).filter(due_date=today, status__is_done=False)
    done_today = team_tasks(integration).filter(status__is_done=True, completed_at__date=today).count()
    by_person: dict[str, list] = {}
    for task in open_tasks:
        who = (task.assignee.full_name or task.assignee.email.split("@")[0]) if task.assignee_id else "—"
        by_person.setdefault(who, []).append(task)
    lines = [f"☀ {ws.name} · {today:%a %d %b}"]
    if not by_person:
        lines.append("Nothing planned for today.")
    for who in sorted(by_person, key=lambda w: (w == "—", w.lower())):
        tasks = sorted(by_person[who], key=lambda t: (not is_urgent(t), t.position))
        lines.append(f"\n{who if who != '—' else 'Unassigned'} ({len(tasks)})")
        for task in tasks[:12]:
            flags = ("🔴 " if is_urgent(task) else "") + ("↻ " if task.planned_date and task.planned_date < today else "")
            project = f" · {task.project.name}" if task.project_id else ""
            lines.append(f"• {flags}{task.title}{project}")
    if due:
        lines.append("\nDue today: " + ", ".join(t.title for t in due[:10]))
    if done_today:
        lines.append(f"\n✅ {done_today} done so far today")
    return "\n".join(lines)


def mine(integration, user) -> str:
    today = local_now(integration).date()
    tasks = team_tasks(integration).filter(assignee=user, planned_date__lte=today, status__is_done=False)
    name = user.full_name or user.email
    if not tasks:
        return f"{name}: nothing open for today 🎉"
    return f"{name}, today:\n" + "\n".join(f"• {t.title}" for t in tasks.order_by("planned_date", "position")[:20])


def system_prompt(integration, actor, author: str, platform: str) -> str:
    ws = integration.workspace
    now = local_now(integration)
    who = (f"{author} (Flowly user {actor.full_name or actor.email}: changes you make are made as them)"
           if actor else f"{author} (not linked to Flowly: you can only read, never change anything)")
    return (
        f"You are Flowly's team assistant in the {platform} chat of the workspace “{ws.name}”. Everything you see and do "
        "is limited to this workspace, and only tasks visible to the whole team. Reply in the language of the message, "
        "briefly, as plain text for a chat: short lines, '•' bullets, no markdown headings or tables.\n"
        f"Now: {now:%A %d %B %Y, %H:%M}. Today's date is {now.date().isoformat()}.\n"
        f"The person writing: {who}. 'me'/'my' means them. Use list_tasks with everyone=true for team questions. "
        "Never invent tasks or ids; when you change something, say what you changed."
    )


def reply(integration, actor, author: str, text: str, platform: str) -> str:
    ws = integration.workspace
    if not ws.is_enterprise:
        return f"Team chats need the Enterprise plan for “{ws.name}”."
    provider = provider_for(ws)
    identity = actor or reader(integration)
    if provider is None or identity is None:
        return "No AI provider is available for this workspace."
    today = local_now(integration).date()
    toolbox = Toolbox(identity, provider, today, workspace_id=ws.id, team_only=True, read_only=actor is None)
    allowed = {"list_tasks"} if actor is None else TEAM_TOOLS
    tools = [t for t in Toolbox.openai_tools() if t["function"]["name"] in allowed]
    history = list(TeamMessage.objects.filter(integration=integration).order_by("-created_at", "-id")[:HISTORY])[::-1]
    messages = [{"role": "system", "content": system_prompt(integration, actor, author, platform)}]
    messages += [{"role": m.role, "content": m.content if m.role == "assistant" else f"{m.author}: {m.content}"} for m in history]
    messages.append({"role": "user", "content": f"{author}: {text}"})
    usage_label = Provider(provider.base_url, "", provider.model, f"Team chat · {ws.name}"[:100])
    try:
        answer = ""
        for _ in range(MAX_STEPS):
            result = chat(provider, messages, tools=tools, max_tokens=700)
            record_usage(identity, usage_label, result["usage"])
            message = result["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                answer = (message.get("content") or "").strip()
                break
            messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
            for call in calls:
                name = call["function"]["name"]
                out = toolbox.run(name, call["function"].get("arguments") or "{}") if name in allowed else "Error: not available here."
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": out[:6000]})
        else:
            answer = "I got a bit lost doing that. Could you rephrase?"
    except LLMUnavailable as exc:
        return str(exc)
    except DeliveryError as exc:
        log.warning("Team bot LLM error for integration %s: %s %s", integration.pk, exc, exc.body[:300])
        return "Sorry, the AI provider didn't answer. Try again in a moment."
    TeamMessage.objects.create(integration=integration, role="user", author=author[:120], content=text[:4000])
    TeamMessage.objects.create(integration=integration, role="assistant", content=answer[:4000])
    old = TeamMessage.objects.filter(integration=integration).order_by("-created_at", "-id")[50:]
    TeamMessage.objects.filter(pk__in=[m.pk for m in old]).delete()
    return answer or "Done."


def due_briefs():
    """Team-chat integrations whose daily brief is due now (once a day, at the configured local time)."""
    from integrations.models import Integration

    from .models import BotState

    for integration in Integration.objects.filter(kind__in=["telegram_group", "slack_bot"], enabled=True).select_related("workspace"):
        cfg = integration.config
        if not cfg.get("brief") or not integration.workspace.is_enterprise:
            continue
        if integration.kind == "telegram_group" and not cfg.get("chat_id"):
            continue
        if integration.kind == "slack_bot" and not cfg.get("channel"):
            continue
        now = local_now(integration)
        try:
            hh, mm = (int(x) for x in (cfg.get("brief_time") or "08:30").split(":"))
        except ValueError:
            hh, mm = 8, 30
        scheduled = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < scheduled or now - scheduled > timedelta(hours=3) or now.weekday() >= 5 and not cfg.get("weekends"):
            continue
        state, _ = BotState.objects.get_or_create(key=f"teambrief:{integration.pk}", defaults={"value": ""})
        if state.value == now.date().isoformat():
            continue
        state.value = now.date().isoformat()
        state.save(update_fields=["value"])
        yield integration
