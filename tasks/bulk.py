"""The bulk-add list format on the server (same rules as the web dialog's bulk-parse.ts).

    - Call the accountant !high @tomorrow #calls
    - [x] Already done +Website
      an indented line becomes a note on the task above
"""

import re
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q

from .models import Tag, TagGroup

BULLET = re.compile(r"^(?:[-*•+]|\d+[.)])\s+")
CHECKBOX = re.compile(r"^\[( |x|X)\]\s*")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _norm(s: str) -> str:
    return re.sub(r"[-_]+", " ", s.lower()).strip()


def parse_date(token: str, today: date):
    """date, None (backlog) or ... (not a date)."""
    t = token.lower()
    if t == "today":
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t in ("backlog", "someday"):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        try:
            return date.fromisoformat(t)
        except ValueError:
            return ...
    for i, wd in enumerate(WEEKDAYS):
        if t.startswith(wd):
            return today + timedelta(days=(i - today.weekday()) % 7 or 7)
    return ...


def parse(text: str, workspace, today: date) -> list[dict]:
    """→ [{"title", "notes", "done", "priority", "tags", "project", "date"}]; "date" is ... when the line has none."""
    priorities = list(Tag.objects.filter(workspace=workspace, group__kind=TagGroup.Kind.PRIORITY))
    labels = list(
        Tag.objects.filter(workspace=workspace).filter(Q(group__isnull=True) | Q(group__kind=TagGroup.Kind.CUSTOM))
    )
    projects = list(workspace.projects.filter(archived=False))
    lines = (text or "").replace("\t", "  ").splitlines()
    indents = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    base = min(indents) if indents else 0
    drafts: list[dict] = []
    for raw in lines:
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip())
        line = BULLET.sub("", raw.strip(), count=1)
        if indent > base and drafts:
            last = drafts[-1]
            last["notes"] = (last["notes"] + "\n" if last["notes"] else "") + line
            continue
        draft = {"title": "", "notes": "", "done": False, "priority": None, "tags": [], "project": None, "date": ...}
        box = CHECKBOX.match(line)
        if box:
            draft["done"] = box.group(1).lower() == "x"
            line = line[box.end():]
        words = []
        for word in line.split():
            sigil, value = word[0], word[1:]
            if value and sigil == "!":
                p = next((t for t in priorities if t.name.lower().startswith(value.lower())), None)
                if p:
                    draft["priority"] = p
                    continue
            elif value and sigil in "#@":
                tag = next((t for t in labels if _norm(t.name) in (_norm(value), _norm("@" + value))), None)
                if tag:
                    draft["tags"] = [t for t in draft["tags"] if tag.group_id is None or t.group_id != tag.group_id] + [tag]
                    continue
                if sigil == "@":
                    when = parse_date(value, today)
                    if when is not ...:
                        draft["date"] = when
                        continue
            elif value and sigil == "+":
                project = next((p for p in projects if _norm(p.name) == _norm(value)), None) or next(
                    (p for p in projects if _norm(p.name).startswith(_norm(value))), None
                )
                if project:
                    draft["project"] = project
                    continue
            words.append(word)
        draft["title"] = " ".join(words).strip()[:255]
        if draft["title"]:
            drafts.append(draft)
    return drafts


def create_many(user, workspace, payloads: list[dict], request=None):
    """Create tasks all-or-nothing, then send integration events and notifications. Raises ValidationError."""
    from rest_framework.exceptions import ValidationError

    from concierge import notify
    from integrations.events import emit, has_outgoing

    from .serializers import TaskSerializer

    context = {"workspace": workspace, "request": request}
    serializers_ = [TaskSerializer(data=item, context=context) for item in payloads]
    errors = {i: s.errors for i, s in enumerate(serializers_) if not s.is_valid()}
    if errors:
        raise ValidationError({"tasks": errors})
    with transaction.atomic():
        created = [s.save(workspace=workspace, created_by=user) for s in serializers_]
    tracked = has_outgoing(workspace.id)
    for task in created:
        if tracked:
            emit(None, task)
        notify.task_changed(None, task, actor=user)
    return created


def create_from_text(user, workspace, text: str, today: date, default_date=...):
    """Parse a list and create its tasks, assigned to the user. default_date: a date, None (backlog) or ... (today)."""
    drafts = parse(text, workspace, today)
    if not drafts:
        return []
    statuses = Tag.objects.filter(workspace=workspace, group__kind=TagGroup.Kind.STATUS)
    done = statuses.filter(is_done=True).first()
    fallback = today if default_date is ... else default_date
    payloads = []
    for d in drafts:
        when = fallback if d["date"] is ... else d["date"]
        item = {
            "title": d["title"], "description": d["notes"], "assignee": user.id,
            "planned_date": when.isoformat() if when else None,
            "project": d["project"].id if d["project"] else None,
            "priority": d["priority"].id if d["priority"] else None,
            "tags": [t.id for t in d["tags"]],
        }
        if d["done"] and done:
            item["status"] = done.id
        payloads.append(item)
    return create_many(user, workspace, payloads)
