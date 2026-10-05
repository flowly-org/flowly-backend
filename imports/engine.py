"""Create Flowly tasks from items read out of another tool (see trello.py, asana.py).

An item: {"id", "title", "notes", "done", "due" (date|None), "start" (date|None), "list" (section/list name),
"labels" [names], "checklist" [{"text", "done"}], "assignee" {"name", "email"} | None,
"comments" [{"author", "text", "date"}]}.
"""

import secrets
from datetime import date

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from tasks.models import Comment, Project, Tag, TagGroup, Task
from tasks.services import default_status

from .models import ImportRun

MAX_ITEMS = 3000
LIST_GROUP = "List"


def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace("-", " ").replace("_", " ").split())


def match_member(members, who):
    if not who:
        return None
    email = (who.get("email") or "").lower()
    name = _norm(who.get("name") or "")
    for u in members:
        if email and u.email.lower() == email:
            return u
    for u in members:
        if name and (_norm(u.full_name or "") == name or _norm(u.email.split("@")[0].replace(".", " ")) == name):
            return u
    return None


def _project(workspace, name, project_id=None):
    if project_id:
        return Project.objects.get(pk=project_id, workspace=workspace)
    base = (name or "Imported").strip()[:110]
    project = Project.objects.filter(workspace=workspace, name__iexact=base).first()
    if project:
        return project
    top = Project.objects.filter(workspace=workspace).aggregate(m=Max("position"))["m"] or 0
    return Project.objects.create(workspace=workspace, name=base, position=top + 1)


def _list_tag(workspace, cache, name):
    if not name:
        return None
    key = name.strip().lower()
    if key in cache:
        return cache[key]
    group = TagGroup.objects.filter(workspace=workspace, kind=TagGroup.Kind.CUSTOM, name=LIST_GROUP).first()
    if group is None:
        top = TagGroup.objects.filter(workspace=workspace).aggregate(m=Max("position"))["m"] or 0
        group = TagGroup.objects.create(workspace=workspace, name=LIST_GROUP, kind=TagGroup.Kind.CUSTOM, position=top + 1)
    tag = Tag.objects.filter(workspace=workspace, group=group, name__iexact=name.strip()[:60]).first() or Tag.objects.create(
        workspace=workspace, group=group, name=name.strip()[:60], position=Tag.objects.filter(group=group).count())
    cache[key] = tag
    return tag


def _label(workspace, cache, name, color=""):
    key = name.strip().lower()
    if key in cache:
        return cache[key]
    tag = (Tag.objects.filter(workspace=workspace, group__isnull=True, name__iexact=name.strip()[:60]).first()
           or Tag.objects.create(workspace=workspace, group=None, name=name.strip()[:60], color=color or "#64748b"))
    cache[key] = tag
    return tag


DONE_LISTS = {"done", "completed", "complete", "fatto", "fatti", "completato", "completati", "hecho", "terminé", "fait", "erledigt", "feito", "concluído"}
COLORS = {"green": "#22c55e", "yellow": "#eab308", "orange": "#f97316", "red": "#ef4444", "purple": "#a855f7", "blue": "#3b82f6",
          "sky": "#0ea5e9", "lime": "#84cc16", "pink": "#ec4899", "black": "#334155"}


@transaction.atomic
def run(user, workspace, *, source, source_id, name, items, project_id=None, members_note=True) -> dict:
    from accounts.models import User

    items = items[:MAX_ITEMS]
    imp, _ = ImportRun.objects.get_or_create(workspace=workspace, source=source, source_id=str(source_id)[:100],
                                             defaults={"name": name[:255], "created_by": user})
    project = _project(workspace, name, project_id) if project_id or not imp.project_id else imp.project
    imp.project, imp.name = project, name[:255]
    members = list(User.objects.filter(memberships__workspace=workspace, is_bot=False))
    done_status = Tag.objects.filter(workspace=workspace, group__kind="status", is_done=True).first()
    todo_status = default_status(workspace)
    top = Task.objects.filter(workspace=workspace).aggregate(m=Max("position"))["m"] or 0
    list_cache, label_cache = {}, {}
    created = skipped = 0
    for item in items:
        if str(item["id"]) in imp.items and Task.objects.filter(pk=imp.items[str(item["id"])]).exists():
            skipped += 1
            continue
        assignee = match_member(members, item.get("assignee"))
        notes = (item.get("notes") or "").strip()
        if item.get("assignee") and assignee is None and members_note:
            who = item["assignee"].get("name") or item["assignee"].get("email")
            notes = (notes + "\n\n" if notes else "") + f"(Assigned to {who} in {source.title()}.)"
        done = item.get("done") or _norm(item.get("list") or "") in DONE_LISTS
        top += 1024
        task = Task.objects.create(
            workspace=workspace, project=project, title=(item.get("title") or "(untitled)")[:255], description=notes[:20000],
            status=done_status if done and done_status else todo_status, completed_at=timezone.now() if done else None,
            due_date=item.get("due"), planned_date=item.get("start"), assignee=assignee,
            created_by=user, position=top,
            checklist=[{"id": secrets.token_hex(4), "text": c["text"][:300], "done": bool(c.get("done"))} for c in item.get("checklist") or []][:100],
        )
        tags = []
        if (t := _list_tag(workspace, list_cache, item.get("list"))) is not None:
            tags.append(t)
        for label in item.get("labels") or []:
            if label.get("name", "").strip():
                tags.append(_label(workspace, label_cache, label["name"], COLORS.get(label.get("color", ""), "")))
        if tags:
            task.tags.set(tags)
        for c in (item.get("comments") or [])[:200]:
            if c.get("text", "").strip():
                Comment.objects.create(task=task, body=c["text"][:10000], source=Comment.Source.IMPORT,
                                       external_author=(c.get("author") or source.title())[:100])
        imp.items[str(item["id"])] = task.id
        created += 1
    imp.save()
    return {"created": created, "skipped": skipped, "project": project.id, "project_name": project.name, "import": imp.id}


def parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
