"""Handling of incoming Telegram messages."""

import logging

from django.utils import timezone

from . import brain, telegram
from .models import ConciergeProfile

log = logging.getLogger("flowly.concierge")

from .i18n import t


def help_text(profile):
    return t("help", brain.lang(profile.user), name=profile.name or "Flo")


def image_of(message: dict):
    """(file_id, mime) of a photo, or of an image sent as a file; else None."""
    if message.get("photo"):
        return message["photo"][-1]["file_id"], "image/jpeg"  # the largest size
    doc = message.get("document") or {}
    if doc.get("mime_type") in ("image/jpeg", "image/png", "image/webp"):
        return doc["file_id"], doc["mime_type"]
    return None


def handle_update(update: dict):
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()
    chat_id = chat.get("id")
    image = image_of(message)
    if not chat_id or not (text or image or message.get("caption") or message.get("voice") or message.get("audio")):
        return
    if chat.get("type") in ("group", "supergroup"):
        from . import teamchat_telegram

        try:
            return teamchat_telegram.handle(message)
        except Exception:
            log.exception("Telegram group update failed")
            return
    if chat.get("type") != "private":
        return
    voice = message.get("voice") or message.get("audio") or (message.get("document") if str((message.get("document") or {}).get("mime_type", "")).startswith("audio/") else None)
    try:
        if voice:
            return _handle_voice(chat_id, voice, (message.get("caption") or "").strip())
        if image:
            return _handle_photo(chat_id, *image, (message.get("caption") or "").strip())
        _handle(chat_id, text, (message.get("from") or {}).get("username", ""))
    except Exception:
        log.exception("Telegram update failed")
        telegram.send_message(chat_id, "Sorry, something went wrong on my side.")


def _handle_voice(chat_id, voice, caption):
    from .voice import handle

    profile = ConciergeProfile.objects.filter(telegram_chat_id=chat_id).select_related("user").first()
    if profile is None:
        return telegram.send_message(chat_id, "Hi! To connect, open Flowly → Settings → Concierge and tap “Connect Telegram”.")
    handle(profile, chat_id, voice["file_id"], voice.get("mime_type", "audio/ogg"), int(voice.get("duration") or 0), caption)


def _handle_photo(chat_id, file_id, mime, caption):
    from .photo_chat import handle

    profile = ConciergeProfile.objects.filter(telegram_chat_id=chat_id).select_related("user").first()
    if profile is None:
        return telegram.send_message(chat_id, "Hi! To connect, open Flowly → Settings → Concierge and tap “Connect Telegram”.")
    handle(profile, chat_id, file_id, mime, caption)


def _handle(chat_id, text, username):
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            return link(chat_id, parts[1].strip(), username)
    profile = ConciergeProfile.objects.filter(telegram_chat_id=chat_id).select_related("user").first()
    if profile is None:
        return telegram.send_message(chat_id, "Hi! To connect, open Flowly → Settings → Concierge and tap “Connect Telegram”.")
    user = profile.user
    from accounts.activity import touch

    touch(user)
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    code = brain.lang(user)
    if command in ("/start", "/help"):
        return telegram.send_message(chat_id, help_text(profile))
    if command == "/alerts_off":
        profile.alerts = False
        profile.save(update_fields=["alerts"])
        return telegram.send_message(chat_id, t("alerts_off", code))
    if command == "/alerts_on":
        profile.alerts = True
        profile.save(update_fields=["alerts"])
        return telegram.send_message(chat_id, t("alerts_on", code))
    if command == "/undo":
        from .photo_chat import undo

        return undo(profile, chat_id)
    if command == "/forget":
        profile.memory = ""
        profile.save(update_fields=["memory"])
        return telegram.send_message(chat_id, t("forgot", code))
    if command == "/apps":
        from . import downloads as apps

        platform = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else "all"
        telegram.send_message(chat_id, apps.describe(platform))
        if platform != "all":
            deliver(chat_id, [{"kind": "app_file", "file": f} for f in apps.sendable(platform)])
        return
    reason = brain.availability(user)
    if reason:
        return telegram.send_message(chat_id, reason)
    telegram.call("sendChatAction", chat_id=chat_id, action="typing")
    if command == "/today":
        return telegram.send_message(chat_id, brain.brief(user))
    outbox: list[dict] = []
    telegram.send_message(chat_id, brain.reply(user, text, outbox=outbox))
    deliver(chat_id, outbox)


def deliver(chat_id, outbox):
    """Things the concierge decided to send after its reply (installer files)."""
    from . import downloads as apps

    for item in outbox:
        if item.get("kind") == "app_file":
            try:
                telegram.call("sendChatAction", chat_id=chat_id, action="upload_document")
                apps.send_file(chat_id, item["file"])
            except Exception as exc:
                log.warning("Sending %s failed: %s", item["file"], exc)
                telegram.send_message(chat_id, f"I couldn't send {item['file']}: here's the link instead: {apps.DOWNLOAD}{item['file']}")


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
    who = profile.user.full_name or profile.user.email
    telegram.send_message(chat_id, t("connected", brain.lang(profile.user), user=who) + "\n\n" + help_text(profile))
