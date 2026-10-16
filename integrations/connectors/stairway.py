from rest_framework import serializers

from ..crypto import decrypt, mask
from .base import Connector

AGENTS = ("claude", "opencode", "crush")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions")


class Stairway(Connector):
    """A Stairway server (github.com/sirmmo/stairway): tasks can be handed to an AI agent that works in its own
    experiment folder; its replies come back as comments and its deliverables as attachments."""

    kind = "stairway"
    has_secret = True

    def token(self, integration) -> str:
        return decrypt(integration.credentials.get("api_key", ""))

    def clean_config(self, config, integration):
        url = (config.get("url") or "").strip().rstrip("/")
        if url.endswith("/mcp"):
            url = url[:-4]
        if not url.startswith("https://"):
            raise serializers.ValidationError({"url": "Your Stairway's https:// address, e.g. https://stairway.example.org."})
        agent = config.get("agent") or "claude"
        if agent not in AGENTS:
            raise serializers.ValidationError({"agent": f"Pick one of: {', '.join(AGENTS)}."})
        mode = config.get("permission_mode") or "acceptEdits"
        if mode not in PERMISSION_MODES:
            raise serializers.ValidationError({"permission_mode": f"Pick one of: {', '.join(PERMISSION_MODES)}."})
        return {"url": url, "agent": agent, "model": (config.get("model") or "").strip()[:120],
                "permission_mode": mode, "instructions": (config.get("instructions") or "").strip()[:2000]}

    def info(self, integration):
        token = self.token(integration)
        return {"token": mask(token) if token else None}

    def test(self, integration) -> str:
        from stairway_tasks.client import call

        tools = call(integration, "tools/list")
        names = [t["name"] for t in tools.get("tools", [])]
        missing = [n for n in ("create_experiment", "experiment_files", "experiment_info") if n not in names]
        if missing:
            raise RuntimeError(f"This Stairway is too old (no {', '.join(missing)}): update it.")
        return f"Connected: {len(names)} tools available."
