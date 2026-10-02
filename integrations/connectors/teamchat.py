"""Team chats (Enterprise): the Flowly bot in a Telegram group or a Slack workspace, for one Flowly workspace."""

import re
import secrets

from rest_framework import serializers

from ..crypto import decrypt
from ..http import send
from .base import Connector, clean_events
from .slack import describe

TEAM_EVENTS = ["task.created", "task.completed", "task.assigned"]


def plain(text: str) -> str:
    """Slack mrkdwn → plain text (Telegram messages are sent as escaped text)."""
    text = re.sub(r"<([^|>]+)\|([^>]+)>", r"\2", text)
    return re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", text)


def clean_team(config, integration, extra_keys=()):
    current = integration.config if integration else {}
    brief_time = (config.get("brief_time") or current.get("brief_time") or "08:30").strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", brief_time):
        raise serializers.ValidationError({"brief_time": "Use HH:MM, e.g. 08:30."})
    tz = (config.get("timezone") or current.get("timezone") or "Europe/Rome").strip()
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
    except Exception as exc:
        raise serializers.ValidationError({"timezone": "Unknown time zone."}) from exc
    out = {
        **{k: current[k] for k in extra_keys if k in current},  # set by the platform, not by the form
        "events": clean_events(config) if "events" in config else current.get("events", TEAM_EVENTS),
        "brief": bool(config.get("brief", current.get("brief", True))),
        "brief_time": brief_time,
        "weekends": bool(config.get("weekends", current.get("weekends", False))),
        "timezone": tz,
    }
    return out


class TelegramGroup(Connector):
    """The Flowly Telegram bot in a group: /today, /mine, /ask, replies, photos of lists, briefs and updates."""

    kind = "telegram_group"
    outgoing = True
    enterprise = True
    default_events = TEAM_EVENTS

    def clean_config(self, config, integration):
        out = clean_team(config, integration, extra_keys=("chat_id", "title", "link_code"))
        if not out.get("chat_id") and not out.get("link_code"):
            out["link_code"] = secrets.token_hex(4)
        return out

    def info(self, integration):
        from concierge import telegram

        from ..http import DeliveryError

        cfg = integration.config
        try:
            bot = telegram.bot_username() if telegram.enabled() else ""
        except DeliveryError:
            bot = ""  # Telegram unreachable: the page still loads
        return {
            "bot": bot,
            "linked": bool(cfg.get("chat_id")),
            "title": cfg.get("title", ""),
            "link_command": f"/link@{bot} {cfg['link_code']}" if cfg.get("link_code") and bot else None,
        }

    def deliver(self, delivery):
        from concierge import telegram

        chat_id = delivery.integration.config.get("chat_id")
        if not chat_id:
            return {"status": 0, "body": "Not linked to a group yet."}
        payload = delivery.payload
        text = (f"🔔 Flowly is connected to this group ({payload['workspace']['name']})." if delivery.event == "ping"
                else plain(describe(delivery.event, payload)))
        telegram.send_message(chat_id, text)
        return {"status": 200, "body": "sent"}


class SlackBot(Connector):
    """The Flowly app in a Slack workspace: @mentions, DMs, /flowly, briefs and updates in a channel."""

    kind = "slack_bot"
    outgoing = True
    enterprise = True
    default_events = TEAM_EVENTS

    def clean_config(self, config, integration):
        out = clean_team(config, integration, extra_keys=("team_id", "team_name", "bot_user_id"))
        channel = config.get("channel", (integration.config if integration else {}).get("channel", ""))
        if channel and not re.fullmatch(r"[A-Z0-9]{6,20}", channel):
            raise serializers.ValidationError({"channel": "Pick a channel from the list."})
        out["channel"] = channel or ""
        out["channel_name"] = (config.get("channel_name") or "")[:80] if channel else ""
        return out

    def info(self, integration):
        cfg = integration.config
        return {"team": cfg.get("team_name", ""), "connected": bool(integration.credentials.get("bot_token")),
                "channel": cfg.get("channel_name", "")}

    @staticmethod
    def token(integration) -> str:
        return decrypt(integration.credentials.get("bot_token", ""))

    @classmethod
    def api(cls, integration, method, **params):
        res = send("POST", f"https://slack.com/api/{method}", json=params, check_url=False,
                   headers={"Authorization": f"Bearer {cls.token(integration)}"}).json()
        if not res.get("ok"):
            from ..http import DeliveryError

            raise DeliveryError(f"Slack {method}: {res.get('error')}", body=str(res)[:500], retry=res.get("error") == "ratelimited")
        return res

    def deliver(self, delivery):
        integration = delivery.integration
        channel = integration.config.get("channel")
        if not channel:
            return {"status": 0, "body": "No channel chosen."}
        payload = delivery.payload
        text = (f"🔔 Flowly is connected to this channel ({payload['workspace']['name']})." if delivery.event == "ping"
                else describe(delivery.event, payload))
        self.api(integration, "chat.postMessage", channel=channel, text=text, unfurl_links=False)
        return {"status": 200, "body": "posted"}
