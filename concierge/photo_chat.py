"""Photos sent to the concierge on Telegram: read the to-do list and add it as tasks."""

import logging
import re
from datetime import timedelta

from django.utils import timezone

from integrations.http import DeliveryError

from . import brain, telegram
from .i18n import t
from .llm import LLMUnavailable
from .models import BotState, ConciergeMessage

log = logging.getLogger("flowly.concierge")
UNDO_HOURS = 24


def pick_workspace(user, caption: str):
    """A workspace named in the caption (longest match wins), else Personal, else the first one."""
    from workspaces.models import Workspace

    qs = Workspace.objects.filter(memberships__user=user).order_by("id")
    low = (caption or "").lower()
    named = sorted((ws for ws in qs if ws.name.lower() in low), key=lambda ws: -len(ws.name))
    return named[0] if named else (qs.filter(name__iexact="Personal").first() or qs.first())


def default_day(caption: str, today):
    low = (caption or "").lower()
    if re.search(r"\b(backlog|someday|qualche giorno)\b", low):
        return None
    if re.search(r"\b(tomorrow|domani|mañana|demain|morgen|amanhã)\b", low):
        return today + timedelta(days=1)
    return today


def handle(profile, chat_id: int, file_id: str, mime: str, caption: str = ""):
    from tasks.bulk import create_from_text

    from .notify import local_today
    from .photo import read_list

    user = profile.user
    code = brain.lang(user)
    reason = brain.availability(user)
    if reason:
        return telegram.send_message(chat_id, reason)
    telegram.send_message(chat_id, t("photo_reading", code))
    telegram.call("sendChatAction", chat_id=chat_id, action="typing")
    image = telegram.download(file_id)
    workspace = pick_workspace(user, caption)
    today = local_today(user)
    try:
        text, _ = read_list(user, workspace, image, mime, note=caption)
    except LLMUnavailable as exc:
        return telegram.send_message(chat_id, str(exc))
    except DeliveryError as exc:
        log.warning("Telegram photo failed for %s: %s %s", user.pk, exc, exc.body[:300])
        return telegram.send_message(chat_id, t("error", code))
    when = default_day(caption, today)
    tasks = create_from_text(user, workspace, text, today, default_date=when) if text else []
    if not tasks:
        return telegram.send_message(chat_id, t("photo_none", code))
    BotState.objects.update_or_create(
        key=f"undo:{chat_id}", defaults={"value": f"{timezone.now().timestamp():.0f}:" + ",".join(str(x.id) for x in tasks)[:180]}
    )
    label = t("when_backlog" if when is None else ("when_today" if when == today else "when_tomorrow"), code)
    lines = [t("photo_added", code, n=len(tasks), ws=workspace.name, when=label)]
    for task in tasks:
        extra = []
        if task.status.is_done:
            extra.append("✓")
        if task.planned_date and task.planned_date != when:
            extra.append(task.planned_date.strftime("%a %d %b"))
        if task.project_id:
            extra.append(task.project.name)
        lines.append(f"• {task.title}" + (f" ({', '.join(extra)})" if extra else ""))
    lines.append("")
    lines.append(t("photo_undo_hint", code))
    reply = "\n".join(lines)
    # Keep it in the chat history, so "move the second one to Friday" works next.
    ids = ", ".join(f"#{x.id} {x.title}" for x in tasks)
    ConciergeMessage.objects.create(user=user, role="user", content=f"[Sent a photo of a to-do list] {caption}"[:4000], channel="telegram")
    ConciergeMessage.objects.create(user=user, role="assistant", content=f"{reply}\n(Created: {ids})"[:4000], channel="telegram")
    telegram.send_message(chat_id, reply)


def undo(profile, chat_id: int):
    from tasks.models import Task

    code = brain.lang(profile.user)
    state = BotState.objects.filter(key=f"undo:{chat_id}").first()
    if state is None or ":" not in state.value:
        return telegram.send_message(chat_id, t("undo_nothing", code))
    stamp, _, ids = state.value.partition(":")
    state.delete()
    if timezone.now().timestamp() - float(stamp) > UNDO_HOURS * 3600:
        return telegram.send_message(chat_id, t("undo_nothing", code))
    pks = [int(x) for x in ids.split(",") if x.isdigit()]
    Task.objects.filter(pk__in=pks, created_by=profile.user).delete()
    deleted = len(pks) - Task.objects.filter(pk__in=pks).count()
    return telegram.send_message(chat_id, t("undo_done", code, n=deleted) if deleted else t("undo_nothing", code))
