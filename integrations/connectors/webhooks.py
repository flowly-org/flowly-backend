import hashlib
import hmac
import json

from django.conf import settings
from rest_framework import serializers

from ..events import TASK_EVENTS
from ..http import send
from .base import Connector, clean_events, clean_schedule


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class OutgoingWebhook(Connector):
    """POSTs signed JSON to a URL on task events."""

    kind = "outgoing_webhook"
    outgoing = True
    default_events = TASK_EVENTS

    def clean_config(self, config, integration):
        url = (config.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise serializers.ValidationError({"url": "Enter an http(s) URL."})
        return {"url": url, "events": clean_events(config)}

    def deliver(self, delivery):
        integration = delivery.integration
        body = json.dumps({**delivery.payload, "delivery_id": delivery.id}, separators=(",", ":")).encode()
        return send(
            "POST",
            integration.config["url"],
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Flowly-Event": delivery.event,
                "X-Flowly-Delivery": str(delivery.id),
                "X-Flowly-Signature": sign(integration.secret, body),
            },
        )

    def info(self, integration):
        return {
            "signature_header": "X-Flowly-Signature: sha256=HMAC_SHA256(secret, raw body)",
            "events": TASK_EVENTS,
        }


class IncomingWebhook(Connector):
    """A secret URL other systems POST to, creating or updating tasks."""

    kind = "incoming_webhook"

    def clean_config(self, config, integration):
        return {
            "schedule": clean_schedule(config),
            "require_signature": bool(config.get("require_signature", False)),
        }

    def info(self, integration):
        base = settings.PUBLIC_URL or ""
        return {
            "url": f"{base}/api/hooks/{integration.token}/",
            "example": {
                "external_id": "TICKET-42",
                "title": "Investigate login error",
                "description": "Reported by …",
                "status": "To do",
                "priority": "High",
                "tags": ["bug"],
                "assignee": "someone@example.com",
                "planned_date": "2026-10-01",
                "due_date": None,
                "url": "https://tracker.example.com/TICKET-42",
            },
        }
