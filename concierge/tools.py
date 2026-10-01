"""Tools the concierge LLM can call. Everything runs as the user, with the same rules as the API."""

import json
from datetime import date, timedelta

from django.db.models import Q

from tasks.models import Comment, Tag, TagGroup, Task
from workspaces.models import Workspace

from . import notify


def _workspaces(user):
    return Workspace.objects.filter(memberships__user=user).order_by("name")


def hidden_workspace_ids(user, provider) -> set[int]:
    """Workspaces with an *exclusive* AI provider other than the one answering: their data stays out."""
    from integrations.models import Integration

    rows = Integration.objects.filter(
        workspace__memberships__user=user, enabled=True, kind__in=["openrouter", "openai_compatible"], config__exclusive=True
    ).values_list("id", "workspace_id")
    return {ws for iid, ws in rows if iid != getattr(provider, "integration_id", None)}


def parse_day(value, today: date):
    v = (value or "today").strip().lower()
    if v == "today":
        return today
    if v == "tomorrow":
        return today + timedelta(days=1)
    if v == "yesterday":
        return today - timedelta(days=1)
    if v in ("backlog", "none", "unscheduled"):
        return None
    return date.fromisoformat(v[:10])


def describe(task: Task, today: date) -> str:
    bits = [f"#{task.id} {task.title}", f"status={task.status.name}"]
    if task.priority_id:
        bits.append(f"priority={task.priority.name}")
    bits.append(f"workspace={task.workspace.name}")
    if task.project_id:
        bits.append(f"project={task.project.name}")
    if task.planned_date and task.planned_date < today and not task.status.is_done:
        bits.append(f"carried over from {task.planned_date}")
    if task.due_date:
        bits.append(f"due={task.due_date}")
    if task.assignee_id:
        bits.append(f"assignee={task.assignee.full_name or task.assignee.email}")
    return "; ".join(bits)


class Toolbox:
    def __init__(self, user, provider, today: date):
        self.user, self.provider, self.today = user, provider, today
        self.hidden = hidden_workspace_ids(user, provider)

    # --- schema ---------------------------------------------------------------

    SCHEMA = [
        {"name": "list_tasks", "description": "List tasks. Use before answering questions about the user's day or tasks.",
         "parameters": {"type": "object", "properties": {
             "day": {"type": "string", "description": "'today', 'tomorrow', 'backlog' or YYYY-MM-DD. Default today."},
             "include_overdue": {"type": "boolean", "description": "Also unfinished tasks planned earlier (default true)."},
             "everyone": {"type": "boolean", "description": "Include tasks assigned to others (default false)."},
             "query": {"type": "string", "description": "Optional text to search in titles."}}}},
        {"name": "list_workspaces", "description": "Workspaces with their projects, statuses and priorities.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "create_task", "description": "Create a task assigned to the user.",
         "parameters": {"type": "object", "required": ["title"], "properties": {
             "title": {"type": "string"}, "workspace": {"type": "string"}, "project": {"type": "string"},
             "day": {"type": "string", "description": "'today', 'tomorrow', 'backlog' or YYYY-MM-DD (default today)."},
             "priority": {"type": "string"}, "notes": {"type": "string"}}}},
        {"name": "update_task", "description": "Change a task: reschedule, rename, set status or priority.",
         "parameters": {"type": "object", "required": ["task_id"], "properties": {
             "task_id": {"type": "integer"}, "title": {"type": "string"},
             "day": {"type": "string", "description": "'today', 'tomorrow', 'backlog' or YYYY-MM-DD."},
             "status": {"type": "string"}, "priority": {"type": "string", "description": "Priority name or 'none'."}}}},
        {"name": "complete_task", "description": "Mark a task done.",
         "parameters": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "integer"}}}},
        {"name": "add_comment", "description": "Add a comment to a task's discussion.",
         "parameters": {"type": "object", "required": ["task_id", "text"], "properties": {
             "task_id": {"type": "integer"}, "text": {"type": "string"}}}},
        {"name": "remember", "description": "Save a lasting note about the user's preferences or context.",
         "parameters": {"type": "object", "required": ["note"], "properties": {"note": {"type": "string"}}}},
    ]

    @classmethod
    def openai_tools(cls):
        return [{"type": "function", "function": t} for t in cls.SCHEMA]

    def run(self, name: str, raw_args: str) -> str:
        try:
            args = json.loads(raw_args or "{}")
            handler = getattr(self, f"tool_{name}", None)
            if handler is None:
                return f"Unknown tool {name}."
            return handler(**args)
        except (ValueError, TypeError) as exc:
            return f"Error: {exc}"
        except LookupError as exc:
            return f"Error: {exc}"

    # --- helpers --------------------------------------------------------------

    def _tasks(self):
        return (
            Task.objects.filter(workspace__memberships__user=self.user)
            .exclude(workspace_id__in=self.hidden)
            .select_related("workspace", "project", "status", "priority", "assignee")
        )

    def _task(self, task_id) -> Task:
        task = self._tasks().filter(pk=int(task_id)).first()
        if task is None:
            raise LookupError(f"No task #{task_id} you can access.")
        return task

    def _workspace(self, name) -> Workspace:
        qs = _workspaces(self.user).exclude(pk__in=self.hidden)
        if name:
            ws = qs.filter(name__iexact=name).first() or qs.filter(name__icontains=name).first()
            if ws is None:
                raise LookupError(f'No workspace called "{name}". Call list_workspaces.')
            return ws
        return qs.filter(name__iexact="Personal").first() or qs.first()

    def _tag(self, ws, kind, name):
        return Tag.objects.filter(workspace=ws, group__kind=kind, name__iexact=name.strip()).first()

    def _save(self, task, data):
        from integrations.events import emit, has_outgoing, snapshot
        from tasks.serializers import TaskSerializer

        tracked = has_outgoing(task.workspace_id) if task.pk else False
        before = snapshot(task) if tracked else None
        before_light = notify.light(task) if task.pk else None
        serializer = TaskSerializer(task if task.pk else None, data=data, partial=bool(task.pk), context={"workspace": task.workspace})
        if not serializer.is_valid():
            raise ValueError(json.dumps(serializer.errors))
        saved = serializer.save(**({} if task.pk else {"workspace": task.workspace, "created_by": self.user}))
        if not task.pk or tracked:
            if has_outgoing(saved.workspace_id):
                emit(before, saved)
        notify.task_changed(before_light, saved, actor=self.user)
        return saved

    # --- tools ----------------------------------------------------------------

    def tool_list_tasks(self, day="today", include_overdue=True, everyone=False, query=""):
        qs = self._tasks()
        if not everyone:
            qs = qs.filter(assignee=self.user)
        target = parse_day(day, self.today)
        if target is None:
            qs = qs.filter(planned_date__isnull=True)
        else:
            cond = Q(planned_date=target)
            if include_overdue and target == self.today:
                cond |= Q(planned_date__lt=target, status__is_done=False)
            qs = qs.filter(cond)
        if query:
            qs = qs.filter(title__icontains=query)
        lines = [describe(t, self.today) for t in qs.order_by("planned_date", "position")[:60]]
        note = f"\n({len(self.hidden)} workspace(s) hidden: they use their own private AI endpoint.)" if self.hidden else ""
        return ("\n".join(lines) or "No tasks.") + note

    def tool_list_workspaces(self):
        out = []
        for ws in _workspaces(self.user).exclude(pk__in=self.hidden):
            groups = {g.kind: [t.name for t in g.tags.all()] for g in ws.tag_groups.filter(kind__in=["status", "priority"])}
            projects = [p.name for p in ws.projects.filter(archived=False)]
            out.append(
                f"{ws.name}: projects={projects or 'none'}; statuses={groups.get('status')}; priorities={groups.get('priority')}"
            )
        return "\n".join(out)

    def tool_create_task(self, title, workspace="", project="", day="today", priority="", notes=""):
        ws = self._workspace(workspace)
        data = {"title": title[:255], "description": notes, "assignee": self.user.id}
        target = parse_day(day, self.today)
        data["planned_date"] = target.isoformat() if target else None
        if project:
            p = ws.projects.filter(name__iexact=project).first() or ws.projects.filter(name__icontains=project).first()
            if p is None:
                raise LookupError(f'No project "{project}" in {ws.name}.')
            data["project"] = p.id
        if priority:
            tag = self._tag(ws, TagGroup.Kind.PRIORITY, priority)
            if tag is None:
                raise LookupError(f'No priority "{priority}" in {ws.name}.')
            data["priority"] = tag.id
        task = self._save(Task(workspace=ws), data)
        return "Created " + describe(task, self.today)

    def tool_update_task(self, task_id, title=None, day=None, status=None, priority=None):
        task = self._task(task_id)
        data = {}
        if title:
            data["title"] = title[:255]
        if day is not None:
            target = parse_day(day, self.today)
            data["planned_date"] = target.isoformat() if target else None
        if status:
            tag = self._tag(task.workspace, TagGroup.Kind.STATUS, status)
            if tag is None:
                raise LookupError(f'No status "{status}" in {task.workspace.name}.')
            data["status"] = tag.id
        if priority:
            if priority.lower() == "none":
                data["priority"] = None
            else:
                tag = self._tag(task.workspace, TagGroup.Kind.PRIORITY, priority)
                if tag is None:
                    raise LookupError(f'No priority "{priority}" in {task.workspace.name}.')
                data["priority"] = tag.id
        task = self._save(task, data)
        return "Updated " + describe(task, self.today)

    def tool_complete_task(self, task_id):
        task = self._task(task_id)
        done = task.workspace.tags.filter(group__kind=TagGroup.Kind.STATUS, is_done=True).first()
        if done is None:
            return f"{task.workspace.name} has no 'done' status."
        task = self._save(task, {"status": done.id})
        return "Completed " + describe(task, self.today)

    def tool_add_comment(self, task_id, text):
        from integrations.events import emit_comment

        task = self._task(task_id)
        comment = Comment.objects.create(task=task, author=self.user, body=text.strip())
        emit_comment(comment)
        notify.comment_added(comment)
        return f"Commented on #{task.id}."

    def tool_remember(self, note):
        profile = self.user.concierge
        profile.memory = (profile.memory + "\n- " + note.strip()).strip()[-4000:]
        profile.save(update_fields=["memory"])
        return "Noted."
