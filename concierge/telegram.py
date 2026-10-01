"""Telegram Bot API calls."""

import html

from django.conf import settings

from integrations.http import send

from instance import conf

_me_cache: dict = {}


def enabled() -> bool:
    return bool(conf.get("TELEGRAM_BOT_TOKEN"))


def call(method: str, **params):
    url = f"{settings.TELEGRAM_API_URL}/bot{conf.get("TELEGRAM_BOT_TOKEN")}/{method}"
    res = send("POST", url, json=params, timeout=40, check_url=False).json()
    return res.get("result")


def bot_username() -> str:
    token = conf.get("TELEGRAM_BOT_TOKEN")
    if token not in _me_cache:
        _me_cache.clear()
        _me_cache[token] = (call("getMe") or {}).get("username", "")
    return _me_cache[token]


def send_message(chat_id: int, text: str):
    """Send plain text (escaped for Telegram's HTML mode), split into Telegram-sized chunks."""
    text = text.strip() or "…"
    for start in range(0, len(text), 3800):
        call("sendMessage", chat_id=chat_id, text=html.escape(text[start : start + 3800]), parse_mode="HTML",
             disable_web_page_preview=True)


def get_updates(offset: int | None, timeout: int = 0):
    params = {"timeout": timeout, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    return call("getUpdates", **params) or []


def set_webhook(url: str, secret: str):
    return call("setWebhook", url=url, secret_token=secret, allowed_updates=["message"], drop_pending_updates=False)


def delete_webhook():
    return call("deleteWebhook", drop_pending_updates=False)
