"""Creating/updating tasks from things that arrive from outside (webhooks, GitHub, email)."""

from datetime import date

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from tasks.models import Tag, TagGroup, Task
from tasks.services import default_status

from .events import emit, snapshot
from .models import ExternalLink, Integration


def members(integration):
    from accounts.models import User

    return User.objects.filter(memberships__workspace=integration.workspace_id)


def find_member(integration, *, email=None, github_login=None):
    qs = members(integration)
    if email:
        return qs.filter(email__iexact=email.strip()).first()
    if github_login:
        return qs.filter(github_login__iexact=github_login.strip()).first()
    return None


def _system_tags(integration, kind):
    return Tag.objects.filter(workspace=integration.workspace_id, group__kind=kind)


def status_by_name(integration, name):
    return _system_tags(integration, TagGroup.Kind.STATUS).filter(name__iexact=name.strip()).first() if name else None


def done_status(integration):
    configured = integration.config.get("done_status")
    tags = _system_tags(integration, TagGroup.Kind.STATUS)
    return (configured and tags.filter(pk=configured).first()) or tags.filter(is_done=True).first()


def open_status(integration):
    configured = integration.config.get("open_status")
    tags = _system_tags(integration, TagGroup.Kind.STATUS)
    return (configured and tags.filter(pk=configured).first()) or default_status(integration.workspace)


def priority_by_name(integration, name):
    return _system_tags(integration, TagGroup.Kind.PRIORITY).filter(name__iexact=name.strip()).first() if name else None


def tags_by_names(integration, names):
    """Existing free/custom tags matching the names (case-insensitive); one per group."""
    wanted = {n.strip().lower() for n in names if n and n.strip()}
    if not wanted:
        return []
    picked, groups = [], set()
    candidates = Tag.objects.filter(workspace=integration.workspace_id).filter(
        Q(group__isnull=True) | Q(group__kind=TagGroup.Kind.CUSTOM)
    )
    for tag in candidates:
        if tag.name.lower() in wanted and (tag.group_id is None or tag.group_id not in groups):
            picked.append(tag)
            if tag.group_id:
                groups.add(tag.group_id)
    return picked


def scheduled_date(integration):
    return timezone.localdate() if integration.config.get("schedule") == "today" else None


def linked_task(integration, external_id):
    link = ExternalLink.objects.filter(integration=integration, external_id=external_id).select_related("task").first()
    return link.task if link else None


@transaction.atomic
def upsert_task(integration: Integration, fields: dict, *, external_id=None, url="", tags=None):
    """Create or update the task linked to `external_id` (or always create when it's None).

    `fields` holds model values (title, description, status, priority, assignee, planned_date, due_date).
    `tags`, when given, replaces the task's free/custom tags. Returns (task, created).
    """
    task = linked_task(integration, external_id) if external_id else None
    created = task is None
    before = None if created else snapshot(task)

    if created:
        top = Task.objects.filter(workspace=integration.workspace_id).aggregate(m=Max("position"))["m"] or 0
        task = Task(
            workspace=integration.workspace,
            project=integration.project,
            position=top + 1024,
            status=fields.pop("status", None) or open_status(integration),
            planned_date=fields.pop("planned_date", scheduled_date(integration)),
        )
    for key, value in fields.items():
        setattr(task, key, value)
    if task.status.is_done and not task.completed_at:
        task.completed_at = timezone.now()
    elif not task.status.is_done:
        task.completed_at = None
    task.save()

    if tags is not None:
        system = task.tags.exclude(Q(group__isnull=True) | Q(group__kind=TagGroup.Kind.CUSTOM))
        task.tags.set([*system, *tags])
    if created and external_id:
        ExternalLink.objects.create(integration=integration, task=task, external_id=external_id, url=url)
    elif external_id and url:
        ExternalLink.objects.filter(integration=integration, external_id=external_id).update(url=url)

    emit(before, task, origin=integration)
    return task, created


def parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
