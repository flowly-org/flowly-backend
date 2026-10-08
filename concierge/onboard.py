"""Getting started with Flo: "what's on your plate this week?" → projects and tasks spread over the week.

Two steps so nothing is created behind the person's back: `propose` asks the model for a plan (shown for review),
`apply` creates what they kept. Open to every plan, not just Pro, a few times a day: it's how people meet Flo.
"""

import json
import logging
import re
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.http import DeliveryError
from workspaces.models import WORKSPACE_COLORS, Membership

from .llm import LLMUnavailable, Provider, chat
from .models import ConciergeMessage

log = logging.getLogger("flowly.concierge")
MAX_CHARS = 6000
MAX_TASKS = 30
MAX_PROJECTS = 6
PER_DAY = 5

PROMPT = """You help someone start with Flowly, a daily planner. They tell you what's on their plate; you turn it into
projects and tasks for their week. Today is {today}, a {weekday}.

Reply with JSON only, no code fences, exactly in this shape:
{{"message": "…", "projects": ["Name"], "tasks": [{{"title": "…", "project": "Name or null", "date": "YYYY-MM-DD or null",
"minutes": 30, "priority": "high or null", "notes": "…"}}]}}

Rules:
- Write in the person's language. Titles are short imperatives ("Call the plumber").
- Projects: only for clear areas of work or life (a client, a product, "Home", "Studies"), 1 to {max_projects}. Reuse
  these existing ones when they fit: {projects}. Tasks that fit no area get "project": null.
- Turn a vague goal into its concrete first step ("thesis" → "Outline chapter 2").
- Dates: keep days and deadlines they state; spread the rest over the working days from today on, at most about 5 a
  day, urgent things first; null for someday or maybe things.
- minutes: a realistic estimate between 10 and 240, or null when you can't tell.
- priority: "high" only when they say it's urgent or important, otherwise null.
- notes: optional, one line of context they gave; "" otherwise.
- At most {max_tasks} tasks. Never invent things they didn't mention.
- message: one short, warm sentence on what you set up and what to start with."""


class SetupError(Exception):
    def __init__(self, message, code=status.HTTP_400_BAD_REQUEST):
        super().__init__(message)
        self.code = code


def workspace_for(user, workspace_id=None):
    memberships = Membership.objects.filter(user=user).select_related("workspace").order_by("joined_at", "id")
    if workspace_id:
        m = memberships.filter(workspace_id=workspace_id).first()
        if m is None:
            raise SetupError("Pick one of your workspaces.", status.HTTP_404_NOT_FOUND)
        return m.workspace
    m = memberships.filter(role=Membership.Role.OWNER).first() or memberships.first()
    if m is None:
        raise SetupError("You have no workspace yet.")
    return m.workspace


def provider(user, workspace) -> Provider:
    from .notes import text_provider

    if user.has_pro:
        return text_provider(user, workspace, "Planning with Flo")
    platform = Provider.platform_default()
    if platform is None:
        raise LLMUnavailable("No AI provider is configured. Ask the admin to set the OpenRouter key.")
    return platform


def used_today(user) -> int:
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return ConciergeMessage.objects.filter(user=user, channel="setup", role="user", created_at__gte=start).count()


def parse(raw: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    return json.loads(text[start : end + 1])


def _date(value, today):
    try:
        d = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    # A plan for the coming weeks: drop dates in the past or absurdly far away.
    return d if today <= d <= today + timedelta(days=90) else None


def _minutes(value):
    try:
        m = int(value)
    except (TypeError, ValueError):
        return None
    return m if 5 <= m <= 480 else None


def normalize(data: dict, workspace, today) -> dict:
    """Clean whatever the model (or the person editing the proposal) sent into a plan we can create."""
    existing = {p.name.lower(): p.name for p in workspace.projects.filter(archived=False)}
    names = []
    for name in data.get("projects") or []:
        name = str(name.get("name") if isinstance(name, dict) else name or "").strip()[:120]
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(existing.get(name.lower(), name))
    names = names[:MAX_PROJECTS]
    by_lower = {n.lower(): n for n in names}
    tasks = []
    for item in (data.get("tasks") or [])[:MAX_TASKS]:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:255]
        if not title:
            continue
        project = str(item.get("project") or "").strip()
        project = by_lower.get(project.lower()) or existing.get(project.lower()) if project and project.lower() != "null" else None
        if project and project not in names:
            names.append(project)
        tasks.append({
            "title": title,
            "project": project,
            "date": (d.isoformat() if (d := _date(item.get("date"), today)) else None),
            "minutes": _minutes(item.get("minutes")),
            "priority": "high" if str(item.get("priority") or "").lower() == "high" else None,
            "notes": " ".join(str(item.get("notes") or "").split())[:500],
        })
    used = {t["project"] for t in tasks}
    return {
        "projects": [{"name": n, "exists": n.lower() in existing} for n in names if n in used],
        "tasks": tasks,
    }


def propose(user, text: str, workspace, today=None) -> dict:
    text = (text or "").strip()
    if not text:
        raise SetupError("Tell Flo what's on your plate first.")
    if len(text) > MAX_CHARS:
        raise SetupError(f"That's long: at most {MAX_CHARS // 1000}k characters.")
    if used_today(user) >= PER_DAY:
        raise SetupError("That's enough planning for today: add the rest by hand, or ask Flo tomorrow.", status.HTTP_429_TOO_MANY_REQUESTS)
    from .brain import local_now

    today = today or local_now(user).date()
    projects = ", ".join(p.name for p in workspace.projects.filter(archived=False)) or "none"
    prompt = PROMPT.format(today=today.isoformat(), weekday=today.strftime("%A"), projects=projects,
                           max_projects=MAX_PROJECTS, max_tasks=MAX_TASKS)
    try:
        result = chat(provider(user, workspace), [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
                      max_tokens=4000, user=user)
    except LLMUnavailable as exc:
        raise SetupError(str(exc), status.HTTP_402_PAYMENT_REQUIRED) from exc
    except DeliveryError as exc:
        log.warning("Setup LLM error for %s: %s", user.pk, exc)
        raise SetupError("Flo couldn't think that through just now. Try again in a moment.", status.HTTP_502_BAD_GATEWAY) from exc
    ConciergeMessage.objects.create(user=user, role="user", content=text[:4000], channel="setup")
    try:
        data = parse(result["message"].get("content") or "")
    except ValueError as exc:
        raise SetupError("Flo's answer came out garbled. Try again, maybe with a shorter list.", status.HTTP_502_BAD_GATEWAY) from exc
    plan = normalize(data, workspace, today)
    if not plan["tasks"]:
        raise SetupError("Flo found nothing to plan in that. Try listing a few things you need to do.", status.HTTP_422_UNPROCESSABLE_ENTITY)
    return {**plan, "message": " ".join(str(data.get("message") or "").split())[:300], "workspace": workspace.pk}


@transaction.atomic
def apply(user, plan: dict, workspace, today=None) -> dict:
    from tasks.models import Project, Tag, Task
    from tasks.services import default_status

    from .brain import local_now

    today = today or local_now(user).date()
    plan = normalize(plan, workspace, today)
    if not plan["tasks"]:
        raise SetupError("Nothing to add.")
    existing = {p.name.lower(): p for p in workspace.projects.all()}
    created_projects = 0
    count = workspace.projects.count()
    for p in plan["projects"]:
        if p["name"].lower() not in existing:
            existing[p["name"].lower()] = Project.objects.create(
                workspace=workspace, name=p["name"], color=WORKSPACE_COLORS[(count + created_projects) % len(WORKSPACE_COLORS)],
                position=count + created_projects,
            )
            created_projects += 1
    status_tag = default_status(workspace)
    high = Tag.objects.filter(workspace=workspace, group__kind="priority").order_by("-position", "-id").first()
    for i, t in enumerate(plan["tasks"]):
        Task.objects.create(
            workspace=workspace, project=existing.get((t["project"] or "").lower()), title=t["title"], description=t["notes"],
            status=status_tag, priority=high if t["priority"] else None, assignee=user, created_by=user,
            planned_date=date.fromisoformat(t["date"]) if t["date"] else None, estimate_minutes=t["minutes"], position=i,
        )
    summary = f"I set up {len(plan['tasks'])} tasks" + (f" and {created_projects} projects" if created_projects else "") + \
        f" in {workspace.name}: " + "; ".join(t["title"] for t in plan["tasks"][:12])
    ConciergeMessage.objects.create(user=user, role="assistant", content=summary[:4000], channel="setup")
    return {"tasks": len(plan["tasks"]), "projects": created_projects, "workspace": workspace.pk}


class SetupView(APIView):
    """POST {"text", "workspace"?} → a proposed plan; POST {"apply": true, "plan": {...}, "workspace"?} creates it."""

    def post(self, request):
        try:
            workspace = workspace_for(request.user, request.data.get("workspace"))
            if request.data.get("apply"):
                return Response(apply(request.user, request.data.get("plan") or {}, workspace))
            return Response(propose(request.user, request.data.get("text") or "", workspace))
        except SetupError as exc:
            return Response({"detail": str(exc)}, status=exc.code)
