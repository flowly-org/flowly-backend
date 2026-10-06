"""Move a project (with its tasks, time and project-level setup) to another workspace, or promote it to its own.

Tasks point at workspace-specific records (status, priority and custom tags, sprints, integrations), so each move
maps them onto the target workspace:

* status / priority: same name, else the equivalent (same "done" kind and rank); no new board columns are added;
* custom tags: same group and name, created in the target when missing, so no label is lost;
* assignees who aren't members of the target are unassigned;
* project-level integrations, agile setup, billing and imports travel with the project; links to the source
  workspace's own integrations (e.g. a workspace-wide GitHub sync) are dropped, so the source stops updating them;
* invoices stay in the source workspace (they are issued documents), detached from the project;
* dependencies on tasks that stay behind are removed.

`plan()` reports all of that without changing anything; `move()` and `promote()` apply it.
"""

from django.db import transaction
from django.db.models import Q

from workspaces.models import Membership, Workspace

from .models import Project, Tag, TagGroup, Task, TaskDependency


class RelocateError(Exception):
    pass


def _people(project):
    """Users involved in the project: assignees, creators and people who logged time on it."""
    from timetracking.models import TimeEntry

    tasks = Task.objects.filter(project=project)
    ids = set(tasks.exclude(assignee=None).values_list("assignee_id", flat=True))
    ids |= set(tasks.exclude(created_by=None).values_list("created_by_id", flat=True))
    ids |= set(TimeEntry.objects.filter(Q(project=project) | Q(task__project=project)).values_list("user_id", flat=True))
    return ids


def _source_links(project, task_ids):
    """Sync links of moved tasks that belong to integrations staying in the source workspace."""
    from donetick.models import DonetickLink
    from integrations.models import ExternalLink

    moving = Q(integration__project=project)
    ext = ExternalLink.objects.filter(task_id__in=task_ids).exclude(moving)
    dt = DonetickLink.objects.filter(task_id__in=task_ids, integration__isnull=False).exclude(moving)
    return ext, dt


def plan(project, target_member_ids=None):
    """What a move would do. `target_member_ids`: members of the target (None = a new workspace with everyone kept)."""
    from timetracking.models import Invoice, TimeEntry

    task_ids = list(Task.objects.filter(project=project).values_list("id", flat=True))
    unassigned = 0
    if target_member_ids is not None:
        unassigned = Task.objects.filter(id__in=task_ids).exclude(assignee=None).exclude(assignee_id__in=target_member_ids).count()
    ext, dt = _source_links(project, task_ids)
    deps = TaskDependency.objects.filter(
        (Q(task_id__in=task_ids) & ~Q(depends_on_id__in=task_ids)) | (Q(depends_on_id__in=task_ids) & ~Q(task_id__in=task_ids))
    ).count()
    return {
        "tasks": len(task_ids),
        "open_tasks": Task.objects.filter(id__in=task_ids, status__is_done=False).count(),
        "time_entries": TimeEntry.objects.filter(Q(project=project) | Q(task_id__in=task_ids)).count(),
        "integrations": project.integrations.count(),
        "unassigned": unassigned,
        "dropped_links": ext.count() + dt.count(),
        "dropped_dependencies": deps,
        "invoices_staying": Invoice.objects.filter(project=project).count(),
        "people": len(_people(project)),
    }


def _free_project_name(workspace, name, exclude=None):
    qs = Project.objects.filter(workspace=workspace)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    candidate, n = name, 1
    while qs.filter(name__iexact=candidate).exists():
        n += 1
        candidate = f"{name} ({n})"
    return candidate


class _TagMap:
    """Maps the source workspace's tags onto the target's, creating custom tags as needed."""

    def __init__(self, source, target):
        self.source, self.target, self.cache = source, target, {}

    def _system(self, tag, kind):
        group = TagGroup.objects.filter(workspace=self.target, kind=kind).first()
        tags = list(Tag.objects.filter(group=group)) if group else []
        if not tags:
            return None
        same = next((t for t in tags if t.name.lower() == tag.name.lower()), None)
        if same:
            return same
        if kind == TagGroup.Kind.STATUS:
            # Same kind (open / done) at the same rank among its siblings, else the closest of that kind.
            siblings = list(Tag.objects.filter(group=tag.group, is_done=tag.is_done))
            rank = next((i for i, t in enumerate(siblings) if t.pk == tag.pk), 0)
            pool = [t for t in tags if t.is_done == tag.is_done] or tags
            return pool[min(rank, len(pool) - 1)]
        siblings = list(Tag.objects.filter(group=tag.group))
        rank = next((i for i, t in enumerate(siblings) if t.pk == tag.pk), 0)
        return tags[min(rank, len(tags) - 1)]

    def _custom(self, tag):
        group = None
        if tag.group_id:
            group = TagGroup.objects.filter(workspace=self.target, kind=TagGroup.Kind.CUSTOM, name__iexact=tag.group.name).first()
            if group is None:
                last = TagGroup.objects.filter(workspace=self.target).order_by("-position").first()
                group = TagGroup.objects.create(
                    workspace=self.target, name=tag.group.name, kind=TagGroup.Kind.CUSTOM, position=(last.position + 1) if last else 0
                )
        found = Tag.objects.filter(workspace=self.target, group=group, name__iexact=tag.name).first()
        if found:
            return found
        last = Tag.objects.filter(workspace=self.target, group=group).order_by("-position").first()
        return Tag.objects.create(
            workspace=self.target, group=group, name=tag.name, color=tag.color, position=(last.position + 1) if last else 0
        )

    def __call__(self, tag):
        if tag is None:
            return None
        if tag.pk not in self.cache:
            kind = tag.group.kind if tag.group_id else TagGroup.Kind.CUSTOM
            self.cache[tag.pk] = self._custom(tag) if kind == TagGroup.Kind.CUSTOM else self._system(tag, kind)
        return self.cache[tag.pk]


@transaction.atomic
def _relocate(project, target):
    from agile.models import AgileSetup
    from donetick.models import DonetickAccount, DonetickRoute
    from imports.models import ImportRun
    from integrations.models import Integration
    from timetracking.models import Invoice, TimeEntry
    from tasks.models import TaskTemplate

    source = project.workspace
    if source.pk == target.pk:
        raise RelocateError("The project is already in that workspace.")
    summary = plan(project, set(Membership.objects.filter(workspace=target).values_list("user_id", flat=True)))
    members = set(Membership.objects.filter(workspace=target).values_list("user_id", flat=True))
    tags = _TagMap(source, target)
    tasks = list(Task.objects.filter(project=project).select_related("status__group", "priority__group", "sprint__setup"))
    task_ids = [t.pk for t in tasks]

    ext, dt = _source_links(project, task_ids)
    ext.delete()
    dt.update(task=None)
    TaskDependency.objects.filter(
        (Q(task_id__in=task_ids) & ~Q(depends_on_id__in=task_ids)) | (Q(depends_on_id__in=task_ids) & ~Q(task_id__in=task_ids))
    ).delete()

    for task in tasks:
        task.workspace = target
        task.status = tags(task.status)
        task.priority = tags(task.priority)
        if task.assignee_id and task.assignee_id not in members:
            task.assignee = None
        # Workspace-wide sprints stay behind; a project's own sprints travel with it.
        if task.sprint_id and task.sprint.setup.project_id != project.pk:
            task.sprint = None
        new_tags = [tags(t) for t in task.tags.select_related("group")]
        task.save(update_fields=["workspace", "status", "priority", "assignee", "sprint", "updated_at"])
        task.tags.set([t for t in new_tags if t is not None])

    TimeEntry.objects.filter(Q(project=project) | Q(task_id__in=task_ids)).update(workspace=target, project=project)
    Invoice.objects.filter(project=project).update(project=None)
    Integration.objects.filter(project=project).update(workspace=target)
    AgileSetup.objects.filter(project=project).update(workspace=target)
    ImportRun.objects.filter(project=project).update(workspace=target)
    TaskTemplate.objects.filter(project=project).update(project=None)
    DonetickRoute.objects.filter(project=project).update(workspace=target)
    DonetickAccount.objects.filter(default_project=project).update(default_workspace=target)

    project.workspace = target
    project.name = _free_project_name(target, project.name, exclude=project)
    last = Project.objects.filter(workspace=target).exclude(pk=project.pk).order_by("-position").first()
    project.position = (last.position + 1) if last else 0
    project.save(update_fields=["workspace", "name", "position"])
    return summary


def move(project, target, user):
    """Move `project` into `target` (an existing workspace `user` belongs to)."""
    if not Membership.objects.filter(workspace=target, user=user).exists():
        raise RelocateError("You aren't a member of that workspace.")
    summary = _relocate(project, target)
    return {**summary, "workspace": target.pk, "project": project.pk, "project_name": project.name}


@transaction.atomic
def promote(project, user, name="", members="involved"):
    """Turn `project` into a new workspace owned by `user`.

    `members`: "involved" (people with tasks or time on the project), "all" (everyone in the source workspace) or
    "me". Roles carry over (the source owner becomes an admin; `user` owns the new workspace).
    """
    from timetracking.models import WorkspaceBilling
    from workspaces.services import create_workspace

    source = project.workspace
    workspace = create_workspace((name or project.name).strip()[:120], user)
    workspace.color = project.color
    workspace.time_tracking = source.time_tracking
    workspace.save(update_fields=["color", "time_tracking"])
    _copy_system_tags(source, workspace)

    if members != "me":
        people = Membership.objects.filter(workspace=source).exclude(user=user)
        if members == "involved":
            people = people.filter(user_id__in=_people(project))
        for m in people:
            role = Membership.Role.ADMIN if m.role == Membership.Role.OWNER else m.role
            Membership.objects.get_or_create(workspace=workspace, user_id=m.user_id, defaults={"role": role})

    billing = WorkspaceBilling.objects.filter(workspace=source).first()
    if billing:
        # Same rates and terms; invoice numbering starts fresh, as in any new workspace.
        WorkspaceBilling.objects.create(
            workspace=workspace, enabled=billing.enabled, currency=billing.currency, default_rate=billing.default_rate,
            rounding_minutes=billing.rounding_minutes, billable_by_default=billing.billable_by_default,
            tax_rate=billing.tax_rate, payment_terms_days=billing.payment_terms_days, invoice_notes=billing.invoice_notes,
        )

    summary = _relocate(project, workspace)
    return {**summary, "workspace": workspace.pk, "project": project.pk, "project_name": project.name, "members": workspace.memberships.count()}


def _copy_system_tags(source, target):
    """Give a new workspace the same statuses and priorities as the source, so nothing gets merged."""
    for kind in (TagGroup.Kind.STATUS, TagGroup.Kind.PRIORITY):
        src = TagGroup.objects.filter(workspace=source, kind=kind).first()
        dst = TagGroup.objects.filter(workspace=target, kind=kind).first()
        if not src or not dst:
            continue
        dst.name = src.name
        dst.save(update_fields=["name"])
        dst.tags.all().delete()
        for t in src.tags.order_by("position", "id"):
            Tag.objects.create(workspace=target, group=dst, name=t.name, color=t.color, is_done=t.is_done, position=t.position)
