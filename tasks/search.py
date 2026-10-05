"""One search box across tasks (titles, notes, checklists, comments), projects, templates and wiki pages."""

from django.db.models import Q
from rest_framework.response import Response
from rest_framework.views import APIView

from .access import member_visible_q
from .models import Comment, Project, Task, TaskTemplate


def snippet(text: str, q: str, size=120) -> str:
    text = " ".join((text or "").split())
    i = text.lower().find(q.lower())
    if i < 0:
        return text[:size]
    start = max(0, i - size // 3)
    return ("…" if start else "") + text[start:start + size] + ("…" if start + size < len(text) else "")


def search(user, q: str, limit=20) -> list[dict]:
    q = q.strip()
    if len(q) < 2:
        return []
    words = q.split()
    workspaces = list(user.memberships.values_list("workspace_id", flat=True))
    visible = Task.objects.filter(workspace_id__in=workspaces).filter(member_visible_q(user))

    def all_words(*fields):
        cond = Q()
        for w in words:
            cond &= _any(fields, w)
        return cond

    results = []
    title_hits = visible.filter(all_words("title")).select_related("status", "workspace", "project").order_by("status__is_done", "-updated_at")[:limit]
    seen = set()
    for t in title_hits:
        seen.add(t.id)
        results.append(_task(t, "", 3))
    if len(results) < limit:
        more = (visible.filter(all_words("description", "checklist__icontains_text")).exclude(pk__in=seen)
                .select_related("status", "workspace", "project").order_by("status__is_done", "-updated_at")[:limit])
        for t in more:
            seen.add(t.id)
            results.append(_task(t, snippet(t.description or " ".join(i["text"] for i in t.checklist or []), words[0]), 2))
    if len(results) < limit:
        comments = (Comment.objects.filter(task__in=visible).filter(all_words("body")).exclude(task_id__in=seen)
                    .select_related("task__status", "task__workspace", "task__project").order_by("-created_at")[:limit])
        for c in comments:
            if c.task_id in seen:
                continue
            seen.add(c.task_id)
            results.append(_task(c.task, "💬 " + snippet(c.body, words[0]), 1))
    for p in Project.objects.filter(workspace_id__in=workspaces).filter(all_words("name")).select_related("workspace")[:5]:
        results.append({"type": "project", "id": p.id, "workspace": p.workspace_id, "workspace_name": p.workspace.name,
                        "title": p.name, "color": p.color, "archived": p.archived, "score": 2.5})
    for tpl in TaskTemplate.objects.filter(workspace_id__in=workspaces).filter(all_words("name")).select_related("workspace")[:5]:
        results.append({"type": "template", "id": tpl.id, "workspace": tpl.workspace_id, "workspace_name": tpl.workspace.name,
                        "title": tpl.name, "score": 2})
    try:
        from wiki.models import WikiPage

        pages = WikiPage.objects.filter(integration__workspace_id__in=workspaces, integration__enabled=True).filter(all_words("title", "content"))
        for page in pages.select_related("integration__workspace")[:5]:
            results.append({"type": "wiki", "id": page.id, "workspace": page.integration.workspace_id,
                            "workspace_name": page.integration.workspace.name, "title": page.title, "url": page.url,
                            "snippet": snippet(page.content, words[0]), "score": 1.5 if q.lower() in page.title.lower() else 0.5})
    except Exception:  # the wiki app is optional
        pass
    results.sort(key=lambda r: -r.pop("score"))
    return results[:limit]


def _any(fields, word):
    cond = Q()
    for f in fields:
        if f == "checklist__icontains_text":
            cond |= Q(checklist__icontains=word)  # JSON as text
        else:
            cond |= Q(**{f"{f}__icontains": word})
    return cond


def _task(t, snip, score):
    return {"type": "task", "id": t.id, "ref": t.ref, "workspace": t.workspace_id, "workspace_name": t.workspace.name,
            "title": t.title, "snippet": snip, "done": t.status.is_done, "planned_date": t.planned_date,
            "project": t.project.name if t.project_id else None, "score": score + (0 if t.status.is_done else 0.5)}


class SearchView(APIView):
    """GET /api/my/search/?q=… — tasks, projects, templates and wiki pages the user can see."""

    def get(self, request):
        return Response(search(request.user, request.query_params.get("q", "")))
