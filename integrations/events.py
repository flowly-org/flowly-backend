"""Turns task changes into outgoing deliveries for the integrations that care."""

from django.utils import timezone

from tasks.models import Task

from instance import conf

from .models import Delivery, Integration

TASK_EVENTS = [
    "task.created", "task.updated", "task.status_changed", "task.completed", "task.assigned", "task.deleted",
    "comment.created",
]
TRACKED_FIELDS = [
    "title", "description", "status_id", "priority_id", "project_id", "assignee_id",
    "planned_date", "due_date", "tags",
]


def has_outgoing(workspace_id) -> bool:
    from .connectors import REGISTRY

    return Integration.objects.filter(
        workspace_id=workspace_id, enabled=True, kind__in=[k for k, c in REGISTRY.items() if c.outgoing]
    ).exists()


def snapshot(task: Task) -> dict:
    return {
        "id": task.id,
        "workspace_id": task.workspace_id,
        "title": task.title,
        "description": task.description,
        "status_id": task.status_id,
        "status_done": task.status.is_done if task.status_id else False,
        "priority_id": task.priority_id,
        "project_id": task.project_id,
        "assignee_id": task.assignee_id,
        "planned_date": task.planned_date.isoformat() if task.planned_date else None,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "tags": sorted(task.tags.values_list("id", flat=True)),
        "payload": task_payload(task),
    }


def board_url(workspace_id, planned_date=None):
    base = conf.get("PUBLIC_URL")
    if not base:
        return ""
    url = f"{base}/w/{workspace_id}/board"
    return f"{url}?date={planned_date}" if planned_date else url


def task_payload(task: Task) -> dict:
    """Self-contained description of a task for outgoing payloads."""
    tags = list(task.tags.select_related("group"))
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "workspace": {"id": task.workspace_id, "name": task.workspace.name},
        "project": {"id": task.project_id, "name": task.project.name} if task.project_id else None,
        "status": {"id": task.status_id, "name": task.status.name, "is_done": task.status.is_done},
        "priority": {"id": task.priority_id, "name": task.priority.name} if task.priority_id else None,
        "assignee": (
            {"id": task.assignee_id, "email": task.assignee.email, "name": task.assignee.full_name}
            if task.assignee_id
            else None
        ),
        "tags": [{"id": t.id, "name": t.name, "group": t.group.name if t.group_id else None} for t in tags],
        "planned_date": task.planned_date.isoformat() if task.planned_date else None,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "url": board_url(task.workspace_id, task.planned_date),
    }


def events_for(before: dict | None, after: dict | None) -> tuple[list[str], dict]:
    if before is None:
        events = ["task.created"]
        if after["assignee_id"]:
            events.append("task.assigned")
        return events, {}
    if after is None:
        return ["task.deleted"], {}
    changes = {
        field: {"from": before[field], "to": after[field]}
        for field in TRACKED_FIELDS
        if before[field] != after[field]
    }
    if not changes:
        return [], {}
    events = ["task.updated"]
    if "status_id" in changes:
        events.append("task.status_changed")
        if after["status_done"] and not before["status_done"]:
            events.append("task.completed")
    if "assignee_id" in changes and after["assignee_id"]:
        events.append("task.assigned")
    # Readable values next to ids.
    for field, key in (("status_id", "status"), ("priority_id", "priority"), ("project_id", "project"), ("assignee_id", "assignee")):
        if field in changes:
            changes[field]["from_value"] = before["payload"].get(key)
            changes[field]["to_value"] = after["payload"].get(key)
    return events, changes


def emit(before: dict | None, task: Task | None, *, origin: Integration | None = None):
    """Queue deliveries for a task change. `before`/`task` None mean created/deleted.

    `origin` is the integration the change came from; it doesn't get its own change echoed back.
    """
    from .connectors import REGISTRY

    after = snapshot(task) if task is not None else None
    events, changes = events_for(before, after)
    if not events:
        return []
    current = after or before
    project_ids = {s["project_id"] for s in (before, after) if s}
    integrations = Integration.objects.filter(
        workspace_id=current["workspace_id"], enabled=True,
        kind__in=[k for k, c in REGISTRY.items() if c.outgoing],
    ).select_related("workspace")
    if origin is not None:
        integrations = integrations.exclude(pk=origin.pk)

    now = timezone.now()
    created = []
    for integration in integrations:
        if integration.project_id is not None and integration.project_id not in project_ids:
            continue
        connector = REGISTRY[integration.kind]
        for event in events:
            if not connector.wants(integration, event, before, after, changes):
                continue
            created.append(
                Delivery.objects.create(
                    integration=integration,
                    task=task,
                    direction=Delivery.Direction.OUT,
                    event=event,
                    payload={
                        "event": event,
                        "occurred_at": now.isoformat(),
                        "workspace": current["payload"]["workspace"],
                        "task": current["payload"],
                        "changes": changes,
                    },
                    next_attempt_at=now,
                )
            )
    return created


def comment_payload(comment) -> dict:
    author = comment.author
    return {
        "id": comment.id,
        "body": comment.body,
        "source": comment.source,
        "author": (
            {"id": author.id, "email": author.email, "name": author.full_name}
            if author
            else {"id": None, "email": None, "name": comment.external_author}
        ),
        "created_at": comment.created_at.isoformat(),
    }


def emit_comment(comment, *, origin: Integration | None = None):
    """Queue comment.created deliveries (e.g. post it on the linked GitHub issue)."""
    from .connectors import REGISTRY

    task = comment.task
    if not has_outgoing(task.workspace_id):
        return []
    after = snapshot(task)
    integrations = Integration.objects.filter(
        workspace_id=task.workspace_id, enabled=True, kind__in=[k for k, c in REGISTRY.items() if c.outgoing]
    ).select_related("workspace")
    if origin is not None:
        integrations = integrations.exclude(pk=origin.pk)
    now = timezone.now()
    created = []
    for integration in integrations:
        if integration.project_id is not None and integration.project_id != task.project_id:
            continue
        if not REGISTRY[integration.kind].wants(integration, "comment.created", after, after, {}):
            continue
        created.append(
            Delivery.objects.create(
                integration=integration,
                task=task,
                direction=Delivery.Direction.OUT,
                event="comment.created",
                payload={
                    "event": "comment.created",
                    "occurred_at": now.isoformat(),
                    "workspace": after["payload"]["workspace"],
                    "task": after["payload"],
                    "comment": comment_payload(comment),
                },
                next_attempt_at=now,
            )
        )
    return created
