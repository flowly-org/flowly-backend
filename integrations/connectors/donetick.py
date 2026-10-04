from rest_framework import serializers

from ..crypto import decrypt, mask
from .base import Connector


def clean_base_url(value) -> str:
    url = (value or "").strip().rstrip("/")
    if not url.startswith(("https://", "http://")) or " " in url:
        raise serializers.ValidationError({"base_url": "Enter your Donetick address, e.g. https://chores.example.org."})
    return url[:300]


class Donetick(Connector):
    """Two-way sync with a (self-hosted) Donetick: chores become tasks of the workspace or project."""

    kind = "donetick"
    has_secret = True

    def clean_config(self, config, integration):
        labels = config.get("labels") or []
        if isinstance(labels, str):
            labels = [x for x in labels.split(",")]
        mapping = config.get("label_projects") or {}
        members = config.get("members") or {}
        if not isinstance(labels, list) or not isinstance(mapping, dict) or not isinstance(members, dict):
            raise serializers.ValidationError({"config": "labels must be a list, label_projects and members objects."})
        try:
            mapping = {str(k).strip()[:100]: int(v) for k, v in mapping.items() if str(k).strip() and v}
            members = {str(int(k)): int(v) for k, v in members.items() if v}
        except (TypeError, ValueError):
            raise serializers.ValidationError({"config": "Mappings must point to ids."}) from None
        return {
            "base_url": clean_base_url(config.get("base_url")),
            "labels": [str(x).strip()[:100] for x in labels if str(x).strip()][:50],
            "label_projects": mapping,
            "members": members,
            "write_back": bool(config.get("write_back", True)),
            "create_chores": bool(config.get("create_chores", False)),
        }

    def info(self, integration):
        from donetick.engine import webhook_url
        from donetick.models import DonetickSync

        state = DonetickSync.objects.filter(integration=integration).first()
        key = decrypt(integration.credentials.get("api_key", "")) if integration.credentials.get("api_key") else ""
        return {
            "webhook_url": webhook_url(integration.token),
            "api_key": mask(key) if key else None,
            "synced_at": state.synced_at if state else None,
            "error": state.error if state else "",
            "chores": state.chores if state else 0,
            "members": state.members if state else [],
            "labels": state.labels if state else [],
        }

    def test(self, integration) -> str:
        from donetick.engine import Source, poll

        state = poll(Source(integration=integration))
        return f"Synced {state.chores} chore(s)."
