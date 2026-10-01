"""Handling of incoming Telegram messages."""

import logging

from django.utils import timezone

from . import brain, telegram
from .models import ConciergeProfile

log = logging.getLogger("flowly.concierge")

HELP = (
    "I'm your Flowly concierge. Just write to me, e.g.\n"
    "• what's on my plate today?\n"
    "• add “call the accountant” tomorrow in Personal\n"
    "• move the proposal to Friday\n\n"
    "Commands: /today brief · /alerts_off · /alerts_on · /forget (clear what I remember) · /help"
)


def handle_update(update: dict):
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()
    chat_id = chat.get("id")
    if not chat_id or not text or chat.get("type") != "private":
        return
    try:
        _handle(chat_id, text, (message.get("from") or {}).get("username", ""))
    except Exception:
        log.exception("Telegram update failed")
        telegram.send_message(chat_id, "Sorry, something went wrong on my side.")


def _handle(chat_id, text, username):
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            return link(chat_id, parts[1].strip(), username)
    profile = ConciergeProfile.objects.filter(telegram_chat_id=chat_id).select_related("user").first()
    if profile is None:
        return telegram.send_message(chat_id, "Hi! To connect, open Flowly → Settings → Concierge and tap “Connect Telegram”.")
    user = profile.user
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if command in ("/start", "/help"):
        return telegram.send_message(chat_id, HELP)
    if command == "/alerts_off":
        profile.alerts = False
        profile.save(update_fields=["alerts"])
        return telegram.send_message(chat_id, "Alerts paused. /alerts_on to resume.")
    if command == "/alerts_on":
        profile.alerts = True
        profile.save(update_fields=["alerts"])
        return telegram.send_message(chat_id, "Alerts are on.")
    if command == "/forget":
        profile.memory = ""
        profile.save(update_fields=["memory"])
        return telegram.send_message(chat_id, "I've cleared what I remembered about you.")
    reason = brain.availability(user)
    if reason:
        return telegram.send_message(chat_id, reason + " Manage your plan in Flowly settings.")
    telegram.call("sendChatAction", chat_id=chat_id, action="typing")
    if command == "/today":
        return telegram.send_message(chat_id, brain.brief(user))
    telegram.send_message(chat_id, brain.reply(user, text))


def link(chat_id, code, username):
    profile = ConciergeProfile.objects.filter(link_code=code, link_expires_at__gt=timezone.now()).select_related("user").first()
    if profile is None:
        return telegram.send_message(chat_id, "That link has expired. Create a new one in Flowly → Settings → Concierge.")
    ConciergeProfile.objects.filter(telegram_chat_id=chat_id).exclude(pk=profile.pk).update(telegram_chat_id=None, telegram_username="")
    profile.telegram_chat_id = chat_id
    profile.telegram_username = username or ""
    profile.link_code = ""
    profile.link_expires_at = None
    profile.save()
    name = profile.user.full_name or profile.user.email
    telegram.send_message(chat_id, f"Connected to Flowly as {name}. 🎉\n\n{HELP}")
