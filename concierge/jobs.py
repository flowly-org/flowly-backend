"""Periodic concierge work, driven by the integrations worker loop."""

import logging
import time
from datetime import datetime, timedelta

from integrations.http import DeliveryError
from instance import conf

from . import bot, brain, notify, telegram
from .models import BotState, ConciergeProfile, Notification

log = logging.getLogger("flowly.concierge")
_last_brief_check = 0.0


def tick():
    for job in (team_jobs, team_briefs):
        try:
            job()
        except Exception:
            log.exception("%s crashed", job.__name__)
    if not telegram.enabled():
        return
    for job in (send_alerts, poll_updates, send_briefs):
        try:
            job()
        except DeliveryError as exc:
            log.warning("%s: %s", job.__name__, exc)
        except Exception:
            log.exception("%s crashed", job.__name__)


def send_alerts(limit=20):
    pending = Notification.objects.filter(telegram=Notification.Telegram.PENDING).select_related("user__concierge")[:limit]
    for n in pending:
        profile = getattr(n.user, "concierge", None)
        if not profile or not profile.telegram_chat_id:
            n.telegram = Notification.Telegram.NONE
        else:
            icon = {"urgent": "🔴", "assigned": "📌", "comment": "💬"}.get(n.kind, "🔔")
            try:
                telegram.send_message(profile.telegram_chat_id, f"{icon} {n.title}\n{n.body}".strip())
                n.telegram = Notification.Telegram.SENT
            except DeliveryError as exc:
                log.warning("Alert %s failed: %s", n.pk, exc)
                n.telegram = Notification.Telegram.FAILED
        n.save(update_fields=["telegram"])


def poll_updates():
    if conf.get("TELEGRAM_MODE") != "polling":
        return
    state, _ = BotState.objects.get_or_create(key="telegram_offset", defaults={"value": ""})
    offset = int(state.value) if state.value else None
    for update in telegram.get_updates(offset, timeout=0):
        state.value = str(update["update_id"] + 1)
        state.save(update_fields=["value"])  # advance first: a bad update must not be retried forever
        bot.handle_update(update)


def send_briefs():
    global _last_brief_check
    if time.monotonic() - _last_brief_check < 60:
        return
    _last_brief_check = time.monotonic()
    for profile in ConciergeProfile.objects.filter(
        enabled=True, morning_brief=True, telegram_chat_id__isnull=False
    ).select_related("user"):
        user = profile.user
        if not user.has_pro:
            continue
        now = brain.local_now(user)
        if profile.last_brief_on == now.date():
            continue
        scheduled = datetime.combine(now.date(), profile.brief_time, tzinfo=now.tzinfo)
        if now < scheduled:
            continue
        profile.last_brief_on = now.date()
        profile.save(update_fields=["last_brief_on"])
        if now - scheduled > timedelta(hours=3):
            continue  # e.g. the worker was down all morning: skip rather than brief at night
        text = brain.brief(user)
        telegram.send_message(profile.telegram_chat_id, text)
        notify.create(user, Notification.Kind.BRIEF, f"Your brief for {now:%A %d %B}", text, telegram=Notification.Telegram.SENT)


def team_jobs():
    from . import teamchat_slack

    teamchat_slack.process()


_last_team_brief_check = 0.0


def team_briefs():
    """Morning team briefs in linked Telegram groups and Slack channels (Enterprise)."""
    global _last_team_brief_check
    if time.monotonic() - _last_team_brief_check < 60:
        return
    _last_team_brief_check = time.monotonic()
    from integrations.connectors.teamchat import SlackBot

    from . import teambot

    for integration in teambot.due_briefs():
        text = teambot.brief(integration)
        try:
            if integration.kind == "telegram_group":
                telegram.send_message(integration.config["chat_id"], text)
            else:
                SlackBot.api(integration, "chat.postMessage", channel=integration.config["channel"], text=text)
        except DeliveryError as exc:
            log.warning("Team brief for integration %s failed: %s", integration.pk, exc)
