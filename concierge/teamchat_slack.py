"""Answers for the Slack app, run by the worker (Slack wants the HTTP reply within 3 seconds)."""

import logging
import re

from django.db import transaction

from integrations.connectors.teamchat import SlackBot
from integrations.http import DeliveryError, send

from . import teambot
from .models import BotJob

log = logging.getLogger("flowly.concierge")


def actor_for(integration, slack_user):
    """The Flowly member with the same email as the Slack user (users:read.email)."""
    try:
        profile = SlackBot.api(integration, "users.info", user=slack_user)["user"]
    except (DeliveryError, KeyError):
        return None, "Someone"
    info = profile.get("profile") or {}
    name = info.get("real_name") or profile.get("real_name") or profile.get("name") or "Someone"
    email = (info.get("email") or "").strip()
    if not email:
        return None, name
    member = integration.workspace.memberships.filter(user__email__iexact=email).select_related("user").first()
    return (member.user if member else None), name


def answer(integration, actor, author, text) -> str:
    words = text.strip().lower()
    if words in ("", "help"):
        return (f"Hi! I'm Flowly for “{integration.workspace.name}”. Try `/flowly today`, `/flowly mine`, or ask me anything "
                "with `@Flowly …`. I recognise you by your Slack email; people who aren't in the Flowly workspace can only read.")
    if words in ("today", "oggi", "team"):
        return teambot.brief(integration)
    if words in ("mine", "me", "my tasks"):
        return teambot.mine(integration, actor) if actor else "I don't know you in Flowly yet: your Slack email isn't a member of this workspace."
    return teambot.reply(integration, actor, author, text, "Slack")


def run(job: BotJob):
    integration = job.integration
    if integration is None or not integration.enabled:
        return
    if not integration.workspace.is_enterprise:
        text_out = f"The Flowly Slack app needs the Enterprise plan for “{integration.workspace.name}”."
        actor = author = None
    if job.kind == "slack_event":
        event = job.payload["event"]
        text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text") or "").strip()
        if integration.workspace.is_enterprise:
            actor, author = actor_for(integration, event.get("user"))
            text_out = answer(integration, actor, author, text)
        thread = event.get("thread_ts") or (event.get("ts") if event.get("channel_type") != "im" else None)
        params = {"channel": event["channel"], "text": text_out, "unfurl_links": False}
        if thread:
            params["thread_ts"] = thread
        SlackBot.api(integration, "chat.postMessage", **params)
    elif job.kind == "slack_command":
        form = job.payload
        if integration.workspace.is_enterprise:
            actor, author = actor_for(integration, form.get("user_id"))
            text_out = answer(integration, actor, author, form.get("text", ""))
        send("POST", form["response_url"], json={"response_type": "in_channel", "text": text_out}, check_url=False)


def process(limit=5) -> int:
    with transaction.atomic():
        jobs = list(BotJob.objects.select_for_update(skip_locked=True).filter(status=BotJob.Status.PENDING)[:limit])
        BotJob.objects.filter(pk__in=[j.pk for j in jobs]).update(attempts=1)
    for job in jobs:
        try:
            run(job)
            job.status = BotJob.Status.DONE
        except Exception as exc:  # one bad job mustn't stop the others
            log.exception("Bot job %s failed", job.pk)
            job.status, job.error = BotJob.Status.FAILED, str(exc)[:2000]
        job.save(update_fields=["status", "error"])
    return len(jobs)
