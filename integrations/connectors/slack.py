from rest_framework import serializers

from ..http import send
from .base import Connector, clean_events

ICONS = {
    "task.created": "🆕",
    "task.completed": "✅",
    "task.status_changed": "➡️",
    "task.assigned": "👤",
    "task.updated": "✏️",
    "task.deleted": "🗑️",
}


def describe(event, payload):
    task = payload["task"]
    title = f"<{task['url']}|{task['title']}>" if task.get("url") else f"*{task['title']}*"
    where = " · ".join(filter(None, [task["workspace"]["name"], (task.get("project") or {}).get("name")]))
    changes = payload.get("changes", {})
    if event == "task.completed":
        line = f"completed {title}"
    elif event == "task.status_changed":
        line = f"{title} moved to *{task['status']['name']}*"
    elif event == "task.assigned":
        who = task["assignee"]["name"] or task["assignee"]["email"]
        line = f"{title} assigned to *{who}*"
    elif event == "task.created":
        line = f"new task {title}"
    elif event == "task.deleted":
        line = f"deleted {title}"
    else:
        line = f"updated {title} ({', '.join(k.replace('_id', '') for k in changes)})"
    return f"{ICONS.get(event, '•')} {line}  _({where})_"


class Slack(Connector):
    """Posts task events to a Slack channel through an incoming-webhook URL."""

    kind = "slack"
    outgoing = True
    default_events = ["task.created", "task.completed", "task.assigned"]

    def clean_config(self, config, integration):
        url = (config.get("webhook_url") or "").strip()
        if not url.startswith("https://"):
            raise serializers.ValidationError({"webhook_url": "Paste the Slack incoming-webhook URL (https://hooks.slack.com/…)."})
        return {"webhook_url": url, "events": clean_events(config)}

    def deliver(self, delivery):
        payload = delivery.payload
        text = (
            f"🔔 Flowly is connected to this channel ({payload['workspace']['name']})."
            if delivery.event == "ping"
            else describe(delivery.event, payload)
        )
        return send("POST", delivery.integration.config["webhook_url"], json={"text": text})
