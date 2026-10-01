"""`search` / `fetch`: the read-only pair ChatGPT uses for connectors and deep research.

search returns {"results": [{id, title, url}]}; fetch returns {id, title, text, url, metadata}.
Ids are task refs, urls the task links.
"""

import json

from django.db.models import Q

from concierge.tools import hidden_workspace_ids
from instance import conf
from tasks.access import member_visible_q
from tasks.models import Task

from .tools import register


def _tasks(user, scope=None):
    qs = Task.objects.filter(workspace_id=scope) if scope else Task.objects.all()
    return (
        qs.filter(workspace__memberships__user=user)
        .filter(member_visible_q(user))
        .exclude(workspace_id__in=hidden_workspace_ids(user, None))
        .select_related("workspace", "project", "status", "priority", "assignee")
    )


def _url(task):
    return f"{conf.get('PUBLIC_URL')}/t/{task.ref}"


def search(user, query="", scope=None):
    words = [w for w in str(query).split() if w]
    qs = _tasks(user, scope)
    for word in words[:8]:
        qs = qs.filter(Q(title__icontains=word) | Q(description__icontains=word) | Q(project__name__icontains=word))
    results = [
        {"id": t.ref, "title": f"{t.title} ({t.workspace.name}{' / ' + t.project.name if t.project_id else ''})", "url": _url(t)}
        for t in qs.order_by("-updated_at")[:20]
    ]
    return json.dumps({"results": results})


def fetch(user, id="", scope=None):
    task = _tasks(user, scope).filter(ref=str(id)).first()
    if task is None:
        raise LookupError(f"No task {id} you can access.")
    lines = [task.title, ""]
    if task.description:
        lines += [task.description, ""]
    lines.append(f"Status: {task.status.name}")
    if task.priority_id:
        lines.append(f"Priority: {task.priority.name}")
    if task.planned_date:
        lines.append(f"Planned for: {task.planned_date}")
    if task.due_date:
        lines.append(f"Due: {task.due_date}")
    if task.assignee_id:
        lines.append(f"Assignee: {task.assignee.full_name or task.assignee.email}")
    tags = ", ".join(t.name for t in task.tags.all())
    if tags:
        lines.append(f"Tags: {tags}")
    comments = list(task.comments.select_related("author").order_by("created_at")[:50])
    if comments:
        lines += ["", "Discussion:"]
        for c in comments:
            who = (c.author.full_name or c.author.email) if c.author else f"@{c.external_author}"
            lines.append(f"- {who} ({c.created_at:%Y-%m-%d}): {c.body}")
    return json.dumps({
        "id": task.ref, "title": task.title, "text": "\n".join(lines), "url": _url(task),
        "metadata": {"workspace": task.workspace.name, "project": task.project.name if task.project_id else None,
                     "status": task.status.name, "task_id": task.id},
    })


register("search", "Search tasks", "Search the user's Flowly tasks by keywords. Returns ids to pass to fetch.",
         {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}, search, read_only=True)
register("fetch", "Fetch task", "Full details and discussion of one task, by the id returned from search.",
         {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}, fetch, read_only=True)
