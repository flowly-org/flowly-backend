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
    if task.estimate_minutes:
        bits.append(f"estimate={task.estimate_minutes}min")
    from tasks.recurrence import describe as repeat_text, is_rule

    if is_rule(task.recurrence):
        bits.append(f"repeats {repeat_text(task.recurrence)}")
    blockers = [d.depends_on for d in task.blocked_by_links.select_related("depends_on__status") if not d.depends_on.status.is_done]
    if blockers:
        bits.append("blocked by " + ", ".join(f"#{b.id}" for b in blockers))
    if task.checklist:
        done = sum(1 for i in task.checklist if i.get("done"))
        bits.append(f"checklist {done}/{len(task.checklist)}")
    return "; ".join(bits)


class Toolbox:
    #: Tools that only read: the ones people who aren't linked to Flowly may use in a team chat.
    READ_ONLY = {"list_tasks", "list_workspaces", "search_wiki", "read_wiki_page"}

    def __init__(self, user, provider, today: date, workspace_id=None, *, team_only=False, read_only=False):
        self.user, self.provider, self.today = user, provider, today
        self.hidden = hidden_workspace_ids(user, provider)
        #: Workspace endpoints of the MCP server (and team chats) limit every tool to one workspace.
        self.only = workspace_id
        #: Team chats: private tasks never show, whoever asks.
        self.team_only = team_only
        self.read_only = read_only
        #: Where the conversation happens ("telegram", "web"), and things to send after the reply (e.g. files).
        self.channel = "web"
        self.outbox: list[dict] = []

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
             "priority": {"type": "string"}, "notes": {"type": "string"},
             "repeat": {"type": "object", "description": "Make it recurring: {freq: daily|weekly|monthly|yearly, interval: 1, weekdays: [0..6, Monday=0] for weekly, after_done: bool}. {} removes the repetition.",
                        "properties": {"freq": {"type": "string"}, "interval": {"type": "integer"}, "weekdays": {"type": "array", "items": {"type": "integer"}}, "after_done": {"type": "boolean"}}},
             "estimate_minutes": {"type": "integer", "description": "How long it takes, in minutes."}}}},
        {"name": "add_tasks", "description": "Create several tasks at once from a list (dictated, pasted, meeting notes). "
         "One task per line; per line you may add !priority, +Project, #tag, @tomorrow / @fri / @YYYY-MM-DD, ~30m; "
         "indented lines become notes, indented '[ ] item' lines checklist items. Prefer this over many create_task calls.",
         "parameters": {"type": "object", "required": ["lines"], "properties": {
             "lines": {"type": "string"}, "workspace": {"type": "string"},
             "day": {"type": "string", "description": "Default day for lines without @date: 'today', 'tomorrow', 'backlog' or YYYY-MM-DD (default today)."}}}},
        {"name": "update_task", "description": "Change a task: reschedule, rename, set status or priority.",
         "parameters": {"type": "object", "required": ["task_id"], "properties": {
             "task_id": {"type": "integer"}, "title": {"type": "string"},
             "day": {"type": "string", "description": "'today', 'tomorrow', 'backlog' or YYYY-MM-DD."},
             "status": {"type": "string"}, "priority": {"type": "string", "description": "Priority name or 'none'."},
             "repeat": {"type": "object", "description": "Make it recurring: {freq: daily|weekly|monthly|yearly, interval: 1, weekdays: [0..6, Monday=0] for weekly, after_done: bool}. {} removes the repetition.",
                        "properties": {"freq": {"type": "string"}, "interval": {"type": "integer"}, "weekdays": {"type": "array", "items": {"type": "integer"}}, "after_done": {"type": "boolean"}}},
             "estimate_minutes": {"type": "integer", "description": "How long it takes, in minutes."}}}},
        {"name": "complete_task", "description": "Mark a task done.",
         "parameters": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "integer"}}}},
        {"name": "add_comment", "description": "Add a comment to a task's discussion.",
         "parameters": {"type": "object", "required": ["task_id", "text"], "properties": {
             "task_id": {"type": "integer"}, "text": {"type": "string"}}}},
        {"name": "start_timer", "description": "Start tracking time on a task (stops any running timer). Find the task id "
         "with list_tasks first.",
         "parameters": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "integer"}}}},
        {"name": "stop_timer", "description": "Stop the running timer and say how long was tracked.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "list_events", "description": "List the user's calendar events (Google Calendar). They are background, "
         "not tasks. Each line has an E-id, and for events that may need preparation the reason and linked tasks.",
         "parameters": {"type": "object", "properties": {
             "day": {"type": "string", "description": "First day: 'today', 'tomorrow' or YYYY-MM-DD. Default today."},
             "days": {"type": "integer", "description": "How many days (default 1, max 60)."},
             "prep_only": {"type": "boolean", "description": "Only events that may need preparation (next 3 weeks)."}}}},
        {"name": "create_event_task", "description": "Create a preparation task for a calendar event (e.g. buy tickets, "
         "book the train) and link it to the event. Due date defaults to the event day.",
         "parameters": {"type": "object", "required": ["event_id", "title"], "properties": {
             "event_id": {"type": "integer", "description": "The number after E."}, "title": {"type": "string"},
             "workspace": {"type": "string"}, "project": {"type": "string"},
             "day": {"type": "string", "description": "When to do it: 'today', 'tomorrow', 'backlog' or YYYY-MM-DD."}}}},
        {"name": "link_event_task", "description": "Link an existing task to a calendar event (it covers the preparation).",
         "parameters": {"type": "object", "required": ["event_id", "task_id"], "properties": {
             "event_id": {"type": "integer"}, "task_id": {"type": "integer"}}}},
        {"name": "mark_event", "description": "Record the user's answer about an event: 'confirmed' (it's still on), "
         "'background' (nothing to prepare, stop asking), 'needed' (needs preparation) or 'auto'.",
         "parameters": {"type": "object", "required": ["event_id", "decision"], "properties": {
             "event_id": {"type": "integer"},
             "decision": {"type": "string", "enum": ["confirmed", "background", "needed", "auto"]}}}},
        {"name": "delegate_task", "description": "Hand a task to the AI agent (Pro Plus): it researches, drafts or plans "
         "and reports back on the task. Use when the user agrees to delegate.",
         "parameters": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "integer"}}}},
        {"name": "app_downloads", "description": "Links to the latest Flowly apps (desktop tray timer for macOS, Windows, "
         "Linux; Android app; iPhone instructions). On Telegram you can also send the installer file itself.",
         "parameters": {"type": "object", "properties": {
             "platform": {"type": "string", "description": "android, mac, windows, linux, ios, desktop or all (default all)."},
             "send_file": {"type": "boolean", "description": "Also send the installer file in the chat (Telegram only)."}}}},
        {"name": "set_dependency", "description": "Record that a task waits for another one (blocked by), or remove that link.",
         "parameters": {"type": "object", "required": ["task_id", "depends_on"], "properties": {
             "task_id": {"type": "integer"}, "depends_on": {"type": "integer"}, "remove": {"type": "boolean"}}}},
        {"name": "block_time", "description": "Block time in the user's day for a task (time blocking). Without a start, "
         "the first free slot of the day that fits.",
         "parameters": {"type": "object", "required": ["task_id"], "properties": {
             "task_id": {"type": "integer"}, "start": {"type": "string", "description": "Local YYYY-MM-DDTHH:MM, or HH:MM for today."},
             "minutes": {"type": "integer", "description": "Default: the task's estimate, else 30."}}}},
        {"name": "plan_day", "description": "Fit the day's tasks into the free time around meetings (time blocks from the "
         "estimates). apply=false only proposes; apply=true creates the blocks.",
         "parameters": {"type": "object", "properties": {
             "day": {"type": "string", "description": "'today', 'tomorrow' or YYYY-MM-DD."}, "apply": {"type": "boolean"}}}},
        {"name": "my_week", "description": "Numbers of the user's week (done, time by project, what's slipping, next week's "
         "load), for 'how was my week' and planning questions. week: any date in it (default this week).",
         "parameters": {"type": "object", "properties": {"week": {"type": "string"}}}},
        {"name": "use_template", "description": "Create the tasks of a saved task template (e.g. 'Client onboarding'), "
         "dated from a start day. Without a name, lists the templates.",
         "parameters": {"type": "object", "properties": {
             "name": {"type": "string"}, "workspace": {"type": "string"},
             "start": {"type": "string", "description": "'today', 'tomorrow' or YYYY-MM-DD (default today)."}}}},
        {"name": "set_reminder", "description": "Remind the user about a task at a moment (in-app and Telegram).",
         "parameters": {"type": "object", "required": ["task_id", "at"], "properties": {
             "task_id": {"type": "integer"}, "at": {"type": "string", "description": "Local date and time, YYYY-MM-DDTHH:MM."}}}},
        {"name": "add_steps", "description": "Break a task into small steps: adds them to the task's "
         "checklist. Use for big or vague tasks (especially in ADHD mode): 3-5 concrete steps, the first under 5 minutes.",
         "parameters": {"type": "object", "required": ["task_id", "steps"], "properties": {
             "task_id": {"type": "integer"}, "steps": {"type": "array", "items": {"type": "string"}}}}},
        {"name": "search_wiki", "description": "Search the team wikis linked to the user's workspaces and projects "
         "(conventions, how-tos, contacts, decisions). Returns matching pages (W-ids) with excerpts.",
         "parameters": {"type": "object", "required": ["query"], "properties": {
             "query": {"type": "string"}, "workspace": {"type": "string", "description": "Optional workspace name."}}}},
        {"name": "read_wiki_page", "description": "Read a wiki page in full by its W-id.",
         "parameters": {"type": "object", "required": ["page_id"], "properties": {"page_id": {"type": "string"}}}},
        {"name": "remember", "description": "Save a lasting note about the user's preferences or context.",
         "parameters": {"type": "object", "required": ["note"], "properties": {"note": {"type": "string"}}}},
        {"name": "forget", "description": "Remove remembered notes that contain these words (when the user says they're wrong or outdated).",
         "parameters": {"type": "object", "required": ["words"], "properties": {"words": {"type": "string"}}}},
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
            if self.read_only and name not in self.READ_ONLY:
                return "Error: only people who linked their Flowly account can change things here."
            return handler(**args)
        except (ValueError, TypeError) as exc:
            return f"Error: {exc}"
        except LookupError as exc:
            return f"Error: {exc}"

    # --- helpers --------------------------------------------------------------

    def _workspaces(self):
        qs = _workspaces(self.user).exclude(pk__in=self.hidden)
        return qs.filter(pk=self.only) if self.only else qs

    def _tasks(self):
        from tasks.access import member_visible_q

        qs = Task.objects.filter(workspace_id=self.only) if self.only else Task.objects.all()
        qs = qs.filter(workspace__memberships__user=self.user).filter(member_visible_q(self.user))
        if self.team_only:
            qs = qs.exclude(visibility=Task.Visibility.PRIVATE)
        return qs.exclude(workspace_id__in=self.hidden).select_related("workspace", "project", "status", "priority", "assignee")

    def _task(self, task_id) -> Task:
        task = self._tasks().filter(pk=int(task_id)).first()
        if task is None:
            raise LookupError(f"No task #{task_id} you can access.")
        return task

    def _workspace(self, name) -> Workspace:
        qs = self._workspaces()
        if self.only:
            return qs.first()
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
        for ws in self._workspaces():
            groups = {g.kind: [t.name for t in g.tags.all()] for g in ws.tag_groups.filter(kind__in=["status", "priority"])}
            projects = [p.name for p in ws.projects.filter(archived=False)]
            out.append(
                f"{ws.name}: projects={projects or 'none'}; statuses={groups.get('status')}; priorities={groups.get('priority')}"
            )
        return "\n".join(out)

    def tool_create_task(self, title, workspace="", project="", day="today", priority="", notes="", repeat=None, estimate_minutes=None):
        ws = self._workspace(workspace)
        data = {"title": title[:255], "description": notes, "assignee": self.user.id}
        if repeat:
            data["recurrence"] = repeat
        if estimate_minutes:
            data["estimate_minutes"] = int(estimate_minutes)
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

    def tool_add_tasks(self, lines, workspace="", day="today"):
        from tasks.bulk import create_from_text

        ws = self._workspace(workspace)
        created = create_from_text(self.user, ws, str(lines)[:20000], self.today, default_date=parse_day(day or "today", self.today))
        if not created:
            return "No tasks found in that list."
        return f"Created {len(created)} task(s) in {ws.name}:\n" + "\n".join(describe(t, self.today) for t in created)

    def tool_update_task(self, task_id, title=None, day=None, status=None, priority=None, repeat=None, estimate_minutes=None):
        task = self._task(task_id)
        data = {}
        if repeat is not None:
            data["recurrence"] = repeat or None
        if estimate_minutes is not None:
            data["estimate_minutes"] = int(estimate_minutes) or None
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

    def tool_start_timer(self, task_id):
        from timetracking.mcp import start_timer

        self._task(task_id)  # visibility and hidden workspaces
        return start_timer(self.user, task_id=int(task_id), scope=self.only)

    def tool_stop_timer(self):
        from timetracking.mcp import stop_timer

        return stop_timer(self.user, scope=self.only)

    def _event(self, event_id):
        from calendars.models import CalendarEvent

        event = CalendarEvent.objects.filter(user=self.user, pk=int(str(event_id).lstrip("Ee"))).first()
        if event is None:
            raise LookupError(f"No calendar event E{event_id}.")
        return event

    def tool_list_events(self, day="today", days=1, prep_only=False):
        from calendars import concierge as cal
        from calendars.sync import user_tz

        if not self.user.calendar_accounts.exists():
            return "No calendar connected (Settings → Calendars in Flowly)."
        tz = user_tz(self.user)
        if prep_only:
            events = cal.prep_events(self.user, self.today)
        else:
            start = parse_day(day, self.today) or self.today
            events = cal.window_events(self.user, start, max(1, min(int(days or 1), 60)))
        return "\n".join(cal.line(e, tz, self.user, self._tasks()) for e in events[:80]) or "No events."

    def tool_create_event_task(self, event_id, title, workspace="", project="", day="today"):
        event = self._event(event_id)
        ws = self._workspace(workspace)
        data = {"title": title, "workspace": ws.id}
        target = parse_day(day, self.today)
        data["planned_date"] = target.isoformat() if target else None
        if project:
            p = ws.projects.filter(name__iexact=project).first() or ws.projects.filter(name__icontains=project).first()
            if p is None:
                raise LookupError(f'No project "{project}" in {ws.name}.')
            data["project"] = p.id
        from calendars.services import create_task
        from rest_framework.exceptions import APIException

        try:
            task = create_task(event, self.user, data)
        except APIException as exc:
            raise ValueError(str(exc.detail)) from exc
        return f"Created and linked to E{event.id}: " + describe(task, self.today)

    def tool_link_event_task(self, event_id, task_id):
        event = self._event(event_id)
        task = self._task(task_id)
        event.tasks.add(task)
        return f"Linked #{task.id} to “{event.title}”."

    def tool_mark_event(self, event_id, decision):
        from calendars.services import decide

        event = self._event(event_id)
        if decision == "confirmed":
            decide(event, confirmed=True)
        else:
            decide(event, prep=decision)
        return f"“{event.title}”: {decision}."

    def tool_delegate_task(self, task_id):
        from delegation import engine

        task = self._task(task_id)
        try:
            d = engine.request(task, self.user, run=True)
        except (PermissionError, ValueError) as exc:
            return f"Error: {exc}"
        used = engine.quota(self.user)
        return f"Delegated #{task.id} “{task.title}” (delegation {d.id}; {used['used']}/{used['limit']} this month). I'll report on the task when done."

    def tool_app_downloads(self, platform="all", send_file=False):
        from . import downloads as apps

        text = apps.describe(platform)
        if send_file:
            if self.channel != "telegram":
                return text + "\n(Files can only be sent on Telegram: here, give the links.)"
            files = apps.sendable(platform)
            self.outbox += [{"kind": "app_file", "file": f} for f in files]
            if files:
                text += "\nThe file(s) will be sent right after your reply: " + ", ".join(files)
            else:
                text += "\nThat file is too big for Telegram: give the link."
        return text

    def tool_add_steps(self, task_id, steps):
        task = self._task(task_id)
        steps = [str(x).strip()[:200] for x in (steps or []) if str(x).strip()][:12]
        if not steps:
            return "Error: no steps given."
        items = list(task.checklist or []) + [{"text": x, "done": False} for x in steps]
        self._save(task, {"checklist": items})
        return f"Added {len(steps)} steps to the checklist of #{task.id} “{task.title}”."

    def tool_set_dependency(self, task_id, depends_on, remove=False):
        task, blocker = self._task(task_id), self._task(depends_on)
        current = list(task.blocked_by_links.values_list("depends_on_id", flat=True))
        ids = [i for i in current if i != blocker.id] if remove else current + [blocker.id]
        self._save(task, {"blocked_by": ids})
        return (f"#{task.id} no longer waits for #{blocker.id}." if remove else f"#{task.id} “{task.title}” now waits for #{blocker.id} “{blocker.title}”.")

    def tool_block_time(self, task_id, start="", minutes=None):
        from datetime import datetime, timedelta

        from calendars import blocks
        from calendars.models import TimeBlock
        from calendars.sync import user_tz

        task = self._task(task_id)
        tz = user_tz(self.user)
        length = timedelta(minutes=int(minutes or task.estimate_minutes or blocks.DEFAULT_MINUTES))
        if start:
            value = str(start).strip()
            when = datetime.fromisoformat(value[:16] if "T" in value else f"{self.today.isoformat()}T{value[:5]}")
            when = when.replace(tzinfo=tz)
        else:
            fit = [s for s, e in blocks.free_slots(self.user, self.today, tz) if e - s >= length]
            if not fit:
                return "No free slot today is long enough: suggest another day or a shorter block."
            when = fit[0]
        b = TimeBlock.objects.create(user=self.user, task=task, title=task.title[:255], start=when, end=when + length)
        return f"Blocked {b.start.astimezone(tz):%a %d %b %H:%M}–{b.end.astimezone(tz):%H:%M} for #{task.id} “{task.title}”."

    def tool_plan_day(self, day="today", apply=False):
        from calendars import blocks
        from calendars.sync import user_tz

        target = parse_day(day, self.today) or self.today
        tz = user_tz(self.user)
        result = blocks.plan(self.user, target, apply=bool(apply))
        lines = [f"{p['start'].astimezone(tz):%H:%M}–{p['end'].astimezone(tz):%H:%M} #{p['task']} {p['title']}" for p in result["placed"]]
        if result["unscheduled"]:
            lines.append("Doesn't fit: " + ", ".join(f"#{u['task']} {u['title']} ({u['minutes']} min)" for u in result["unscheduled"]))
        head = "Blocked:" if apply else "Proposed (not created yet; call again with apply=true):"
        return head + "\n" + ("\n".join(lines) or "Nothing to plan.")

    def tool_my_week(self, week=""):
        from calendars.sync import user_tz
        from insights.review import plain_text, week_numbers
        from insights.stats import monday

        day = parse_day(week or "today", self.today) or self.today
        s = week_numbers(self.user, monday(day), user_tz(self.user), self.today)
        slip = "; ".join(f"#{x['id']} {x['title']}" for x in s["slipping"][:8])
        return plain_text(self.user, s) + (f"\nSlipping: {slip}" if slip else "")

    def tool_use_template(self, name="", workspace="", start="today"):
        from tasks.models import TaskTemplate

        qs = TaskTemplate.objects.filter(workspace__in=self._workspaces()).select_related("workspace")
        if workspace:
            qs = qs.filter(workspace=self._workspace(workspace))
        if not name:
            return "\n".join(f"{t.name} ({t.workspace.name})" for t in qs[:50]) or "No templates yet (Board → Templates)."
        tpl = qs.filter(name__iexact=name).first() or qs.filter(name__icontains=name).first()
        if tpl is None:
            raise LookupError(f'No template "{name}". Call use_template without a name to list them.')
        if self.read_only:
            raise ValueError("Read-only here.")
        from tasks.bulk import create_many, parse

        day = parse_day(start, self.today) or self.today
        payloads = []
        for d in parse(tpl.body, tpl.workspace, day):
            when = day if d["date"] is ... else d["date"]
            payloads.append({"title": d["title"], "description": d["notes"], "assignee": self.user.id,
                             "planned_date": when.isoformat() if when else None,
                             "project": d["project"].id if d["project"] else tpl.project_id,
                             "priority": d["priority"].id if d["priority"] else None, "tags": [t.id for t in d["tags"]],
                             "estimate_minutes": d.get("estimate"), "checklist": d.get("checklist") or []})
        created = create_many(self.user, tpl.workspace, payloads)
        return f"Created {len(created)} tasks from “{tpl.name}”:\n" + "\n".join(describe(t, self.today) for t in created)

    def tool_set_reminder(self, task_id, at):
        from datetime import datetime

        from calendars.sync import user_tz
        from django.utils import timezone as tz

        from tasks.models import Reminder

        task = self._task(task_id)
        try:
            when = datetime.fromisoformat(str(at).strip()[:16])
        except ValueError:
            raise ValueError("Use YYYY-MM-DDTHH:MM for the moment.") from None
        when = when.replace(tzinfo=user_tz(self.user))
        if when < tz.now():
            raise ValueError("That moment has already passed.")
        Reminder.objects.get_or_create(task=task, user=self.user, remind_at=when)
        return f"I'll remind you about #{task.id} “{task.title}” on {when:%a %d %b at %H:%M}."

    def _wikis(self, workspace=""):
        from wiki.search import sources

        ids = [self._workspace(workspace).id] if workspace else list(self._workspaces().values_list("id", flat=True))
        return list(sources(ids))

    def tool_search_wiki(self, query, workspace=""):
        from wiki.search import describe_hits, search

        wikis = self._wikis(workspace)
        if not wikis:
            return "No wiki is linked (workspace managers can add one under Integrations → Wiki)."
        hits = search(wikis, query, limit=5)
        return describe_hits(hits, query) if hits else "Nothing in the wiki matches."

    def tool_read_wiki_page(self, page_id):
        from wiki.search import read

        return read(self._wikis(), page_id, limit=5500)

    def tool_remember(self, note):
        from .personality import MEMORY_LIMIT, notes

        profile = self.user.concierge
        items = notes(profile.memory)
        note = note.strip()
        if note and note.lower() not in (n.lower() for n in items):
            items.append(note)
        profile.memory = "\n".join(f"- {n}" for n in items)[-MEMORY_LIMIT:]
        profile.save(update_fields=["memory"])
        return "Noted."

    def tool_forget(self, words):
        from .personality import notes

        profile = self.user.concierge
        items = notes(profile.memory)
        keep = [n for n in items if words.lower().strip() not in n.lower()]
        if len(keep) == len(items):
            return "Nothing matched in memory."
        profile.memory = "\n".join(f"- {n}" for n in keep)
        profile.save(update_fields=["memory"])
        return f"Forgot {len(items) - len(keep)} note(s)."
