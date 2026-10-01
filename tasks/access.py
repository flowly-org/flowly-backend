"""Who can see a task.

private   → its creator and assignee
team      → every member of the workspace (default)
shareable → team + people in TaskShare (they can view and comment through the task link)
public    → anyone with the link, read-only
"""

from django.db.models import Q

from .models import Task, TaskShare


def member_visible_q(user) -> Q:
    """Filter for tasks a workspace member may see (apply on top of workspace membership)."""
    return ~Q(visibility=Task.Visibility.PRIVATE) | Q(created_by=user) | Q(assignee=user)


def link_access(task: Task, user) -> str | None:
    """'member', 'shared', 'public' or None for someone opening /t/<ref>."""
    authed = user is not None and user.is_authenticated
    if authed and task.workspace.memberships.filter(user=user).exists():
        if task.visibility != Task.Visibility.PRIVATE or user.id in (task.created_by_id, task.assignee_id):
            return "member"
    if authed and task.visibility in (Task.Visibility.SHAREABLE, Task.Visibility.PUBLIC):
        if TaskShare.objects.filter(task=task).filter(Q(user=user) | Q(email__iexact=user.email)).exists():
            return "shared"
    if task.visibility == Task.Visibility.PUBLIC:
        return "public"
    return None
