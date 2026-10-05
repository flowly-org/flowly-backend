"""Task dependencies ("blocked by"): validation (same workspace, no loops) and the unblocked notification."""

from .models import Task, TaskDependency


def would_loop(task_id: int, blocker_id: int) -> bool:
    """Adding "task waits for blocker" makes a loop if the blocker already (transitively) waits for the task."""
    seen, stack = set(), [blocker_id]
    while stack:
        current = stack.pop()
        if current == task_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(TaskDependency.objects.filter(task_id=current).values_list("depends_on_id", flat=True))
    return False


def set_blockers(task, blockers):
    """Replace the task's blockers (validated beforehand)."""
    wanted = {b.id for b in blockers}
    TaskDependency.objects.filter(task=task).exclude(depends_on_id__in=wanted).delete()
    have = set(TaskDependency.objects.filter(task=task).values_list("depends_on_id", flat=True))
    TaskDependency.objects.bulk_create([TaskDependency(task=task, depends_on_id=b) for b in wanted - have])


def open_blockers(task):
    return Task.objects.filter(blocking_links__task=task, status__is_done=False)


def notify_unblocked(done_task, actor=None):
    """A blocker was finished: tell the assignees of tasks that have nothing left blocking them."""
    from concierge import notify
    from concierge.models import Notification

    for t in Task.objects.filter(blocked_by_links__depends_on=done_task, status__is_done=False).select_related("assignee", "workspace"):
        if t.assignee_id and t.assignee_id != getattr(actor, "id", None) and not open_blockers(t).exists():
            notify.create(t.assignee, Notification.Kind.ASSIGNED, f"🔓 Unblocked: {t.title}",
                          f"“{done_task.title}” is done, nothing is holding this up any more.", task=t)
