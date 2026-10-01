from rest_framework import serializers

from ..events import TASK_EVENTS


class Connector:
    """One integration kind. Subclasses override what they support."""

    kind = ""
    #: Whether this kind receives task-change deliveries (handled by the worker).
    outgoing = False
    #: Events used when the config doesn't pick any.
    default_events: list[str] = []

    def clean_config(self, config: dict, integration) -> dict:
        """Validate and normalise user-supplied config; raise serializers.ValidationError."""
        return {}

    def wants(self, integration, event, before, after, changes) -> bool:
        events = integration.config.get("events") or self.default_events
        return event in events

    def deliver(self, delivery):
        raise NotImplementedError

    def test_payload(self, integration) -> dict:
        return {
            "event": "ping",
            "workspace": {"id": integration.workspace_id, "name": integration.workspace.name},
            "integration": {"id": integration.id, "name": integration.name},
        }

    def info(self, integration) -> dict:
        """Read-only details for the UI (URLs, addresses, setup hints)."""
        return {}


def clean_events(config):
    events = config.get("events") or []
    if not isinstance(events, list) or any(e not in TASK_EVENTS for e in events):
        raise serializers.ValidationError({"events": f"Pick events from: {', '.join(TASK_EVENTS)}."})
    return events


def clean_schedule(config):
    schedule = config.get("schedule", "backlog")
    if schedule not in ("backlog", "today"):
        raise serializers.ValidationError({"schedule": 'Use "backlog" or "today".'})
    return schedule
