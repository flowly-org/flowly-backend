import re

from rest_framework import serializers

from ..http import send
from .base import Connector, clean_events

WEBHOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+/?$")
#: Embed side colours per event (Discord wants an integer).
COLORS = {
    "task.created": 0x4F46E5,
    "task.completed": 0x16A34A,
    "task.status_changed": 0x0EA5E9,
    "task.assigned": 0xA855F7,
    "task.updated": 0x64748B,
    "task.deleted": 0xEF4444,
    "comment.created": 0xF59E0B,
}
#: Nobody gets pinged by a task title like "@everyone lunch".
NO_MENTIONS = {"parse": []}


def escape(text: str) -> str:
    """Discord markdown → literal text."""
    return re.sub(r"([\\*_~`|>#\[\]()-])", r"\\\1", text or "")


def person(user: dict | None) -> str:
    return (user or {}).get("name") or (user or {}).get("email") or "someone"


def embed(event: str, payload: dict) -> dict:
    task = payload["task"]
    where = " · ".join(filter(None, [task["workspace"]["name"], (task.get("project") or {}).get("name")]))
    status = task.get("status") or {}
    lines = {
        "task.created": "New task",
        "task.completed": "Done",
        "task.status_changed": f"Moved to **{escape(status.get('name', ''))}**",
        "task.assigned": f"Assigned to **{escape(person(task.get('assignee')))}**",
        "task.deleted": "Deleted",
        "task.updated": "Updated: " + ", ".join(k.replace("_id", "").replace("_", " ") for k in payload.get("changes", {})),
    }
    if event == "comment.created":
        comment = payload["comment"]
        body = " ".join(comment["body"].split())
        description = f"**{escape(person(comment.get('author')))}** commented:\n> {escape(body[:300])}{'…' if len(body) > 300 else ''}"
    else:
        description = lines.get(event, "Changed")
    fields = []
    if event != "task.deleted":
        if task.get("assignee") and event != "task.assigned":
            fields.append({"name": "Assignee", "value": escape(person(task["assignee"])), "inline": True})
        if task.get("due_date"):
            fields.append({"name": "Due", "value": task["due_date"], "inline": True})
        if task.get("priority"):
            fields.append({"name": "Priority", "value": escape(task["priority"]["name"]), "inline": True})
    out = {
        "title": escape(task["title"])[:256],
        "description": description[:4000],
        "color": COLORS.get(event, 0x64748B),
        "footer": {"text": where[:2048]},
        "fields": fields,
    }
    link = task.get("link") or task.get("url")
    if link and event != "task.deleted":
        out["url"] = link
    return out


class Discord(Connector):
    """Posts task events to a Discord channel through its webhook (Channel settings → Integrations → Webhooks)."""

    kind = "discord"
    outgoing = True
    default_events = ["task.created", "task.completed", "task.assigned"]

    def clean_config(self, config, integration):
        url = (config.get("webhook_url") or "").strip()
        if not WEBHOOK.match(url):
            raise serializers.ValidationError(
                {"webhook_url": "Paste the channel's webhook URL (Edit channel → Integrations → Webhooks → Copy Webhook URL)."}
            )
        return {"webhook_url": url, "events": clean_events(config)}

    def deliver(self, delivery):
        payload = delivery.payload
        if delivery.event == "ping":
            body = {"content": f"Flowly is connected to this channel ({escape(payload['workspace']['name'])}).", "allowed_mentions": NO_MENTIONS}
        else:
            body = {"embeds": [embed(delivery.event, payload)], "allowed_mentions": NO_MENTIONS}
        return send("POST", delivery.integration.config["webhook_url"], json={"username": "Flowly", **body})
