"""Telegram groups linked to a workspace (Enterprise).

Telegram bots in groups (privacy mode) only receive commands and replies to their own messages, so:
  /link <code>   link this group to a workspace (code from Flowly → Integrations → Telegram group)
  /today         the team's plan for today          /mine   your open tasks (linked accounts)
  /ask <text>    ask or tell the assistant           (or just reply to one of its messages)
  /tasks         as a photo caption: turn a photo of a written list into tasks   /undo  remove them
  /unlink        stop (workspace owners and admins)  /help
"""

import logging

from django.utils import timezone

from integrations.http import DeliveryError

from . import teambot, telegram
from .llm import LLMUnavailable
from .models import BotState, ConciergeProfile

log = logging.getLogger("flowly.concierge")

HELP = (
    "Flowly team assistant for “{ws}”.\n"
    "• /today: the team's plan for today\n"
    "• /mine: your open tasks\n"
    "• /ask …: ask or tell me something (or reply to one of my messages)\n"
    "• send a photo of a written list with the caption /tasks to add it\n"
    "• /undo: remove the tasks from the last photo\n\n"
    "To change tasks, link your Flowly account with me in a private chat first (Flowly → Settings → Concierge)."
)


def group_integration(chat_id):
    from integrations.models import Integration

    return (
        Integration.objects.filter(kind="telegram_group", enabled=True, config__chat_id=chat_id)
        .select_related("workspace", "created_by")
        .first()
    )


def actor_for(integration, from_id):
    """The Flowly user behind a Telegram account (linked to the concierge in private), if they're in the workspace."""
    profile = ConciergeProfile.objects.filter(telegram_chat_id=from_id).select_related("user").first()
    if profile and integration.workspace.memberships.filter(user=profile.user).exists():
        return profile.user
    return None


def command_of(text: str, bot: str):
    """('/today', 'rest') for commands meant for us; (None, text) otherwise."""
    if not text.startswith("/"):
        return None, text
    head, _, rest = text.partition(" ")
    name, _, target = head.partition("@")
    if target and bot and target.lower() != bot.lower():
        return None, text  # another bot's command
    return name.lower(), rest.strip()


def handle(message: dict):
    chat = message["chat"]
    chat_id = chat["id"]
    sender = message.get("from") or {}
    author = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or sender.get("username") or "Someone"
    bot = telegram.bot_username()
    text = (message.get("text") or message.get("caption") or "").strip()
    command, rest = command_of(text, bot)
    reply_to = message.get("message_id")
    replying_to_us = ((message.get("reply_to_message") or {}).get("from") or {}).get("username", "").lower() == bot.lower() if bot else False

    if command == "/link":
        return link(chat, rest, reply_to)
    integration = group_integration(chat_id)
    if integration is None:
        if command in ("/help", "/start", "/today", "/ask"):
            telegram.send_message(chat_id, "This group isn't linked to Flowly yet. In Flowly: Integrations → Telegram group, then send the /link command shown there.", reply_to)
        return
    if not integration.workspace.is_enterprise:
        if command:
            telegram.send_message(chat_id, f"Team chats need the Enterprise plan for “{integration.workspace.name}”.", reply_to)
        return
    actor = actor_for(integration, sender.get("id"))
    photo = message.get("photo") or ((message.get("document") or {}) if (message.get("document") or {}).get("mime_type", "").startswith("image/") else None)

    if photo and (command == "/tasks" or replying_to_us):
        return photo_tasks(integration, actor, message, rest if command else text, reply_to)
    if command in ("/help", "/start"):
        return telegram.send_message(chat_id, HELP.format(ws=integration.workspace.name), reply_to)
    if command == "/today":
        return telegram.send_message(chat_id, teambot.brief(integration), reply_to)
    if command == "/mine":
        if actor is None:
            return telegram.send_message(chat_id, "Link your Flowly account with me in a private chat first (Flowly → Settings → Concierge).", reply_to)
        return telegram.send_message(chat_id, teambot.mine(integration, actor), reply_to)
    if command == "/undo":
        return undo(integration, actor, chat_id, reply_to)
    if command == "/unlink":
        m = integration.workspace.memberships.filter(user=actor).first() if actor else None
        if not (m and m.can_manage):
            return telegram.send_message(chat_id, "Only workspace owners and admins can unlink the group.", reply_to)
        integration.config = {**integration.config, "chat_id": None, "title": ""}
        integration.save(update_fields=["config"])
        return telegram.send_message(chat_id, "Unlinked. Bye 👋", reply_to)
    if command == "/ask" or (command is None and replying_to_us and text):
        if not rest:
            return telegram.send_message(chat_id, "Ask me something after /ask, e.g. /ask what's left for today?", reply_to)
        telegram.call("sendChatAction", chat_id=chat_id, action="typing")
        return telegram.send_message(chat_id, teambot.reply(integration, actor, author, rest, "Telegram group"), reply_to)


def link(chat, code, reply_to):
    from integrations.models import Integration

    chat_id = chat["id"]
    code = (code or "").strip()
    integration = Integration.objects.filter(kind="telegram_group", enabled=True, config__link_code=code).select_related("workspace").first() if code else None
    if integration is None:
        return telegram.send_message(chat_id, "That link code isn't valid. Copy the /link command from Flowly → Integrations → Telegram group.", reply_to)
    if not integration.workspace.is_enterprise:
        return telegram.send_message(chat_id, f"Team chats need the Enterprise plan for “{integration.workspace.name}”.", reply_to)
    for other in Integration.objects.filter(kind="telegram_group", config__chat_id=chat_id).exclude(pk=integration.pk):
        other.config = {**other.config, "chat_id": None, "title": ""}
        other.save(update_fields=["config"])
    cfg = {**integration.config, "chat_id": chat_id, "title": chat.get("title", "")}
    cfg.pop("link_code", None)
    integration.config = cfg
    integration.save(update_fields=["config"])
    telegram.send_message(chat_id, f"✅ Linked to the Flowly workspace “{integration.workspace.name}”.\n\n" + HELP.format(ws=integration.workspace.name), reply_to)


def photo_tasks(integration, actor, message, caption, reply_to):
    from tasks.bulk import create_from_text

    from .photo import read_list
    from .photo_chat import default_day

    chat_id = message["chat"]["id"]
    if actor is None:
        return telegram.send_message(chat_id, "Only people who linked their Flowly account can add tasks (Flowly → Settings → Concierge).", reply_to)
    if message.get("photo"):
        file_id, mime = message["photo"][-1]["file_id"], "image/jpeg"
    else:
        file_id, mime = message["document"]["file_id"], message["document"]["mime_type"]
    telegram.call("sendChatAction", chat_id=chat_id, action="typing")
    ws = integration.workspace
    today = teambot.local_now(integration).date()
    try:
        provider = teambot.provider_for(ws)
        if provider is None:
            raise LLMUnavailable("No AI provider is available for this workspace.")
        if provider.platform:
            from instance import conf

            provider.model = conf.get("OPENROUTER_VISION_MODEL") or provider.model
        text, _ = read_list(actor, ws, telegram.download(file_id), mime, note=caption, provider=provider)
    except LLMUnavailable as exc:
        return telegram.send_message(chat_id, str(exc), reply_to)
    except DeliveryError as exc:
        log.warning("Group photo failed: %s", exc)
        return telegram.send_message(chat_id, "Sorry, I couldn't read that photo. Try again in a moment.", reply_to)
    when = default_day(caption, today)
    tasks = create_from_text(actor, ws, text, today, default_date=when) if text else []
    if not tasks:
        return telegram.send_message(chat_id, "I couldn't find any to-do items in that photo.", reply_to)
    BotState.objects.update_or_create(
        key=f"undo:{chat_id}", defaults={"value": f"{timezone.now().timestamp():.0f}:" + ",".join(str(t.id) for t in tasks)[:180]}
    )
    lines = [f"📋 Added {len(tasks)} tasks to {ws.name}:"] + [f"• {t.title}" + (" ✓" if t.status.is_done else "") for t in tasks]
    telegram.send_message(chat_id, "\n".join(lines + ["", "/undo removes them."]), reply_to)


def undo(integration, actor, chat_id, reply_to):
    from tasks.models import Task

    state = BotState.objects.filter(key=f"undo:{chat_id}").first()
    if actor is None or state is None or ":" not in state.value:
        return telegram.send_message(chat_id, "There's nothing to undo.", reply_to)
    stamp, _, ids = state.value.partition(":")
    if timezone.now().timestamp() - float(stamp) > 24 * 3600:
        state.delete()
        return telegram.send_message(chat_id, "There's nothing to undo.", reply_to)
    pks = [int(x) for x in ids.split(",") if x.isdigit()]
    mine = Task.objects.filter(pk__in=pks, created_by=actor, workspace=integration.workspace)
    count = mine.count()
    if not count:
        return telegram.send_message(chat_id, "Only the person who added them can undo.", reply_to)
    mine.delete()
    state.delete()
    telegram.send_message(chat_id, f"Removed {count} tasks.", reply_to)
