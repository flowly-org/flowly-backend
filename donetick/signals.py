"""Flowly → Donetick: notice when a linked task is completed, renamed or rescheduled (and new tasks, for
integrations that create chores), and queue the change for the worker."""

import threading
from contextlib import contextmanager

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from tasks.models import Task

_local = threading.local()


@contextmanager
def quiet():
    """Changes made by the sync itself must not travel back to Donetick."""
    previous = getattr(_local, "quiet", False)
    _local.quiet = True
    try:
        yield
    finally:
        _local.quiet = previous


def is_quiet() -> bool:
    return getattr(_local, "quiet", False)


@receiver(pre_save, sender=Task)
def remember_before(sender, instance, **kwargs):
    if not instance.pk or is_quiet():
        return
    from .models import DonetickLink

    if not DonetickLink.objects.filter(task_id=instance.pk, done=False).exists():
        return
    instance._donetick_before = (Task.objects.filter(pk=instance.pk)
                                 .values("title", "description", "planned_date", "status__is_done").first())


@receiver(post_save, sender=Task)
def queue_changes(sender, instance, created, **kwargs):
    if is_quiet():
        return
    from . import engine
    from .models import DonetickLink

    if created:
        _maybe_create_chore(instance)
        return
    before = getattr(instance, "_donetick_before", None)
    if not before:
        return
    instance._donetick_before = None
    link = DonetickLink.objects.filter(task_id=instance.pk, done=False).select_related("integration", "account").first()
    if link is None:
        return
    src = engine.Source.of_link(link)
    if not src.enabled or not src.write_back:
        return
    done_now = instance.status.is_done if instance.status_id else False
    if done_now and not before["status__is_done"]:
        engine.queue("complete", link=link, task=instance)
        return
    update = {}
    if instance.title != before["title"]:
        update["name"] = instance.title
    if instance.description != before["description"]:
        update["description"] = instance.description
    if instance.planned_date != before["planned_date"] and instance.planned_date and not done_now:
        update["dueDate"] = src.wire_due(instance.planned_date)
    if update:
        engine.queue("update", link=link, task=instance, payload=update)


def _maybe_create_chore(task):
    """Workspace/project integrations with "create chores" on: a new task in their scope becomes a chore."""
    from integrations.models import Integration

    from . import engine

    for integration in Integration.objects.filter(workspace_id=task.workspace_id, kind="donetick", enabled=True):
        cfg = integration.config
        if not cfg.get("create_chores") or not cfg.get("write_back", True):
            continue
        if integration.project_id and integration.project_id != task.project_id:
            continue
        src = engine.Source(integration=integration)
        payload = {"name": task.title, "description": task.description or None}
        if task.planned_date or task.due_date:
            payload["dueDate"] = src.wire_due(task.due_date or task.planned_date)
        engine.queue("create", integration=integration, task=task, payload=payload)
        break
