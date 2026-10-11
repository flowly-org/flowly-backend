"""Triage ("can an AI do this?") and the agent that does it, run by the worker.

The agent only produces things (research, drafts, documents, subtasks) and reports back on the task. It never
contacts people, buys or sends anything on the user's behalf.
"""

import json
import logging
import re

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from instance import conf
from integrations.http import DeliveryError

from .models import Delegation

log = logging.getLogger("flowly.delegation")
MONTHLY_RUNS = 60
MONTHLY_ASSESSMENTS = 300
MAX_STEPS = 14
MAX_TOKENS = 150_000
AUTHOR = "Flo (AI)"

TRIAGE_PROMPT = """You decide whether an AI agent can do a task from someone's to-do list.
The agent can: search the web and read pages, read the task's attachments, write texts and documents (drafts,
summaries, plans, comparisons, checklists), read the team wiki if one is linked, and add subtasks for the person. It cannot: make calls, send emails or
messages, buy or book anything, sign in anywhere, be somewhere physically, or decide on the person's behalf.

Answer with JSON only: {"verdict": "full" | "partial" | "no", "plan": "...", "reason": "..."}
- "full": the agent can deliver what the task asks (e.g. research, a draft to review, a summary).
- "partial": it can do a useful part (e.g. find options or write the email) and leave the rest to the person.
- "no": it can't help meaningfully (errands, calls, personal or physical things, too vague).
"plan": one or two short sentences on what the agent would deliver. "reason": one short sentence.
Write plan and reason in the language of the task."""

AGENT_PROMPT = """You are Flo, Flowly's assistant, working on a task {name} delegated to you. Do as much of it as you can
with your tools, then call finish. Rules:
- Only produce things: research with sources, drafts, documents, summaries, plans, subtasks. Never pretend to have
  sent, booked, bought or contacted anything: you can't. Leave those steps to the person (as subtasks if useful).
- If a team wiki is given, it is the workspace's own knowledge: follow its conventions, templates and facts, and
  cite the pages you used. Search it before the web for anything internal.
- Use web_search for facts that may have changed and cite source URLs. Read pages with fetch_page when needed.
- Put deliverables (drafts, comparisons, plans) in write_document: one document per deliverable, in Markdown.
- Write in the language of the task. Be concise and practical.
- You have at most {steps} steps. Call finish with a short summary, outcome "done" or "partial", and what's left."""

TOOLS = [
    {"name": "web_search", "description": "Search the web; returns findings with source URLs.",
     "parameters": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}},
    {"name": "fetch_page", "description": "Read a web page (text only).",
     "parameters": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}}},
    {"name": "read_attachment", "description": "Read one of the task's attachments (text, HTML or PDF) by its id.",
     "parameters": {"type": "object", "required": ["attachment_id"], "properties": {"attachment_id": {"type": "integer"}}}},
    {"name": "search_wiki", "description": "Search the team wiki linked to the task's workspace or project.",
     "parameters": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}},
    {"name": "read_wiki_page", "description": "Read a team wiki page in full by its W-id.",
     "parameters": {"type": "object", "required": ["page_id"], "properties": {"page_id": {"type": "string"}}}},
    {"name": "write_document", "description": "Save a deliverable (Markdown) on the task.",
     "parameters": {"type": "object", "required": ["title", "content"], "properties": {
         "title": {"type": "string"}, "content": {"type": "string"}}}},
    {"name": "add_subtask", "description": "Add a follow-up task for a step you can't do (e.g. 'Call the hotel').",
     "parameters": {"type": "object", "required": ["title"], "properties": {
         "title": {"type": "string"}, "notes": {"type": "string"},
         "day": {"type": "string", "description": "'today', 'tomorrow', 'backlog' or YYYY-MM-DD; default backlog."}}}},
    {"name": "finish", "description": "End the work and report.",
     "parameters": {"type": "object", "required": ["summary", "outcome"], "properties": {
         "summary": {"type": "string"}, "outcome": {"type": "string", "enum": ["done", "partial"]},
         "left_for_you": {"type": "string", "description": "What the person still has to do, if anything."}}}},
]


# --- limits & providers -----------------------------------------------------------


def month_start():
    now = timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def runs_used(user) -> int:
    return Delegation.objects.filter(user=user, run=True, created_at__gte=month_start()).exclude(
        status__in=[Delegation.Status.DECLINED, Delegation.Status.FAILED]
    ).count()


def monthly_limit():
    """Runs per month on Pro Plus; no cap on an instance that doesn't sell plans (it pays for its own AI)."""
    from instance.conf import plans_enabled

    return MONTHLY_RUNS if plans_enabled() else None


def quota(user) -> dict:
    return {"used": runs_used(user), "limit": monthly_limit()}


def provider_for(task):
    """The workspace's own AI (Enterprise, keeps data there) or Flowly's, with the delegation models."""
    from concierge.teambot import provider_for as workspace_provider

    provider = workspace_provider(task.workspace)
    if provider is None:
        raise RuntimeError("No AI provider is available.")
    return provider


def with_model(provider, key):
    from concierge.llm import Provider

    if not provider.platform:
        return provider
    model = conf.get(key) or conf.get("DELEGATION_MODEL") or provider.model
    return Provider(provider.base_url, provider.api_key, model, provider.label, platform=True)


def call(delegation, provider, messages, **kw):
    """One model call. Usage is recorded under its own label: delegations have their own monthly allowance and
    mustn't eat the concierge's daily token cap."""
    from concierge.llm import Provider, chat, record_usage

    result = chat(provider, messages, **kw)
    usage = result.get("usage") or {}
    delegation.tokens += int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    record_usage(delegation.user, Provider(provider.base_url, "", provider.model, "Delegation"), usage)
    return result


# --- context -----------------------------------------------------------------------


def describe_task(task, wiki_budget=0) -> str:
    from tasks import attributes

    values = attributes.Values()
    lines = [f"Task: {attributes.rendered_title(task, values)}", f"Workspace: {task.workspace.name}"]
    if task.project_id:
        lines.append(f"Project: {task.project.name}")
    if task.due_date:
        lines.append(f"Due: {task.due_date}")
    if task.description.strip():
        notes = attributes.render(task.description, attributes.for_task(task, values))
        lines.append(f"Notes:\n{notes.strip()[:4000]}")
    if used := attributes.context(task, values):
        lines.append(f"Context the team attached (workspace/project attributes this task uses):\n{used[:4000]}")
    comments = list(task.comments.exclude(source="ai").order_by("-created_at")[:8])[::-1]
    if comments:
        lines.append("Discussion:\n" + "\n".join(
            f"- {(c.author.full_name or c.author.email) if c.author else c.external_author or 'Someone'}: {c.body[:500]}" for c in comments))
    attachments = list(task.attachments.all()[:20])
    if attachments:
        lines.append("Attachments:\n" + "\n".join(f"- id {a.id}: {a.name} ({a.kind}{', ' + a.url if a.url else ''})" for a in attachments))
    if wiki_budget:
        from wiki.search import task_context

        try:
            wiki = task_context(task, budget=wiki_budget)
        except Exception:
            log.exception("Wiki context failed for task %s", task.pk)
            wiki = ""
        if wiki:
            lines.append("\n" + wiki)
    return "\n".join(lines)


def parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("No JSON in the answer.")
    return json.loads(match.group(0))


# --- triage --------------------------------------------------------------------------


def assess(delegation: Delegation):
    task = delegation.task
    provider = with_model(provider_for(task), "DELEGATION_TRIAGE_MODEL")
    messages = [{"role": "system", "content": TRIAGE_PROMPT}, {"role": "user", "content": describe_task(task, wiki_budget=1500)}]
    # Generous limit: reasoning models spend part of it thinking before they answer.
    result = call(delegation, provider, messages, max_tokens=1500)
    content = result["message"].get("content") or ""
    try:
        data = parse_json(content)
    except (ValueError, json.JSONDecodeError):
        # A cut-off answer still usually carries the verdict: don't turn it into a "no".
        found = re.search(r'"verdict"\s*:\s*"(full|partial|no)"', content)
        if not found:
            raise RuntimeError(f"The triage answer couldn't be read: {content[:200]!r}")
        plan = re.search(r'"plan"\s*:\s*"([^"]*)', content)
        data = {"verdict": found.group(1), "plan": plan.group(1) if plan else ""}
    verdict = data.get("verdict") if data.get("verdict") in ("full", "partial", "no") else "no"
    delegation.verdict = verdict
    delegation.plan = str(data.get("plan") or "")[:2000]
    delegation.reason = str(data.get("reason") or "")[:1000]


# --- the agent ------------------------------------------------------------------------


class Workbench:
    """What the agent's tools act on: the task, the person, and what it produced."""

    def __init__(self, delegation, provider):
        self.d, self.provider = delegation, provider
        self.task, self.user = delegation.task, delegation.user
        self.documents: list[tuple[str, str]] = []
        self.subtasks = []
        self.finished: dict | None = None

    def run(self, name, raw) -> str:
        try:
            args = json.loads(raw or "{}")
            return getattr(self, f"tool_{name}")(**args)
        except AttributeError:
            return f"Unknown tool {name}."
        except (TypeError, ValueError, DeliveryError) as exc:
            return f"Error: {exc}"

    def tool_web_search(self, query):
        from concierge.llm import Provider

        if not self.provider.platform:
            return "Web search isn't available with this workspace's own AI provider. Work from what you know and the attachments."
        model = conf.get("DELEGATION_SEARCH_MODEL") or f"{self.provider.model.split(':')[0]}:online"
        searcher = Provider(self.provider.base_url, self.provider.api_key, model, self.provider.label, platform=True)
        messages = [{"role": "user", "content": f"Search the web and report the relevant findings, each with its source URL. Be factual and concise.\n\nQuery: {query}"}]
        result = call(self.d, searcher, messages, max_tokens=3000)
        return (result["message"].get("content") or "No results.")[:8000]

    def tool_fetch_page(self, url):
        from .web import fetch

        return fetch(url)

    def tool_read_attachment(self, attachment_id):
        from .web import fetch, to_text

        a = self.task.attachments.filter(pk=int(attachment_id)).first()
        if a is None:
            return "No such attachment on this task."
        if a.kind == "file":
            if not a.file:
                return "The file isn't available."
            with a.file.open("rb") as fh:
                data = fh.read(3 * 1024 * 1024)
            _, text = to_text(data, a.content_type, a.name)
            return text[:15000]
        if not a.url:
            return "This attachment has no address."
        return fetch(a.url)

    def tool_search_wiki(self, query):
        from wiki.search import describe_hits, search, task_sources

        wikis = list(task_sources(self.task))
        if not wikis:
            return "This task's workspace has no wiki."
        hits = search(wikis, query, limit=5, boost_project=self.task.project_id)
        return describe_hits(hits, query) if hits else "Nothing in the wiki matches."

    def tool_read_wiki_page(self, page_id):
        from wiki.search import read, task_sources

        return read(list(task_sources(self.task)), page_id)

    def tool_write_document(self, title, content):
        self.documents.append((str(title)[:200], str(content)[:30000]))
        return f"Saved “{title}”."

    def tool_add_subtask(self, title, notes="", day="backlog"):
        from concierge.tools import parse_day
        from tasks.bulk import create_many

        if len(self.subtasks) >= 10:
            return "Error: at most 10 subtasks."
        target = parse_day(day or "backlog", timezone.localdate())
        payload = {"title": str(title)[:255], "assignee": self.user.id, "planned_date": target.isoformat() if target else None,
                   "description": (str(notes).strip() + "\n\n" if notes else "") + f"From Flo's work on {self.task.ref} “{self.task.title}”.",
                   "project": self.task.project_id}
        task = create_many(self.user, self.task.workspace, [payload])[0]
        self.subtasks.append(task)
        return f"Added subtask {task.ref} “{task.title}”."

    def tool_finish(self, summary, outcome="done", left_for_you=""):
        self.finished = {"summary": str(summary), "outcome": "partial" if outcome == "partial" else "done",
                         "left_for_you": str(left_for_you or "")}
        return "Finished."


def work(delegation: Delegation):
    from concierge.brain import language_rule

    task, user = delegation.task, delegation.user
    provider = with_model(provider_for(task), "DELEGATION_MODEL")
    bench = Workbench(delegation, provider)
    name = f"for {user.full_name or user.email}"
    messages = [
        {"role": "system", "content": AGENT_PROMPT.format(name=name, steps=MAX_STEPS) + "\n" + language_rule(user)},
        {"role": "user", "content": describe_task(task, wiki_budget=7000) + (f"\n\nPlan: {delegation.plan}" if delegation.plan else "")},
    ]
    tools = [{"type": "function", "function": t} for t in TOOLS]
    for step in range(MAX_STEPS):
        delegation.steps = step + 1
        last = step == MAX_STEPS - 1 or delegation.tokens > MAX_TOKENS
        if last:
            messages.append({"role": "user", "content": "Budget reached: call finish now with what you have."})
        result = call(delegation, provider, messages, tools=tools, max_tokens=6000)
        message = result["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            content = (message.get("content") or "").strip()
            bench.finished = bench.finished or {"summary": content or "Done.", "outcome": "partial", "left_for_you": ""}
            break
        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        for c in calls:
            out = bench.run(c["function"]["name"], c["function"].get("arguments") or "{}")
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": out[:12000]})
        if bench.finished:
            break
    if not bench.finished:
        bench.finished = {"summary": "I ran out of steps before finishing.", "outcome": "partial", "left_for_you": ""}
    report(delegation, bench)


def report(delegation: Delegation, bench: Workbench):
    from concierge import notify
    from integrations.events import emit_comment
    from tasks.models import Comment

    task = delegation.task
    f = bench.finished
    delegation.summary = f["summary"][:8000]
    delegation.left_for_you = f["left_for_you"][:4000]
    delegation.status = Delegation.Status.PARTIAL if f["outcome"] == "partial" else Delegation.Status.DONE
    lines = [f"🤖 {'Done' if delegation.status == 'done' else 'Partly done'}: {delegation.summary}"]
    if bench.documents:
        lines.append("\nDocuments: " + ", ".join(f"“{t}”" for t, _ in bench.documents) + " (below)")
    if bench.subtasks:
        lines.append("\nSubtasks: " + ", ".join(f"{t.ref} “{t.title}”" for t in bench.subtasks))
    if delegation.left_for_you:
        lines.append(f"\nLeft for you: {delegation.left_for_you}")
    comments = [Comment.objects.create(task=task, body="\n".join(lines), source=Comment.Source.AI, external_author=AUTHOR)]
    for title, content in bench.documents:
        comments.append(Comment.objects.create(task=task, body=f"📄 {title}\n\n{content}", source=Comment.Source.AI, external_author=AUTHOR))
    for c in comments:
        emit_comment(c)
    notify.create(delegation.user, "comment", f"Flo finished “{task.title}”" if delegation.status == "done" else f"Flo did part of “{task.title}”",
                  delegation.summary[:280], task=task)


# --- queue --------------------------------------------------------------------------


def handle(delegation: Delegation):
    delegation.started_at = timezone.now()
    try:
        if not delegation.verdict:
            delegation.status = Delegation.Status.ASSESSING
            delegation.save(update_fields=["status", "started_at"])
            assess(delegation)
        if not delegation.run or delegation.verdict == Delegation.Verdict.NO:
            delegation.status = Delegation.Status.ASSESSED if not delegation.run else Delegation.Status.DECLINED
            if delegation.run:
                from concierge import notify

                notify.create(delegation.user, "comment", f"Flo can't take on “{delegation.task.title}”", delegation.reason, task=delegation.task)
        else:
            delegation.status = Delegation.Status.RUNNING
            delegation.save(update_fields=["status", "verdict", "plan", "reason", "tokens"])
            work(delegation)
    except Exception as exc:  # report, never crash the worker
        log.exception("Delegation %s failed", delegation.pk)
        delegation.status, delegation.error = Delegation.Status.FAILED, str(exc)[:2000]
    delegation.finished_at = timezone.now()
    delegation.save()


def process(limit=2) -> int:
    with transaction.atomic():
        batch = list(Delegation.objects.select_for_update(skip_locked=True, of=("self",)).filter(status=Delegation.Status.QUEUED)
                     .select_related("task__workspace", "task__project", "user")[:limit])
        Delegation.objects.filter(pk__in=[d.pk for d in batch]).update(status=Delegation.Status.ASSESSING)
    for d in batch:
        handle(d)
    return len(batch)


def request(task, user, run: bool) -> Delegation:
    """Queue a check or a run. Raises PermissionError / ValueError with a readable message."""
    if not user.has_pro_plus:
        raise PermissionError("Delegating tasks to AI is part of Flowly Pro Plus.")
    active = Delegation.objects.filter(task=task, user=user, status__in=Delegation.ACTIVE).first()
    if active:
        if run and not active.run:
            active.run = True
            active.save(update_fields=["run"])
        return active
    if run and monthly_limit() is not None and runs_used(user) >= MONTHLY_RUNS:
        raise ValueError(f"You've used this month's {MONTHLY_RUNS} delegations. They reset on the 1st.")
    if not run and Delegation.objects.filter(user=user, created_at__gte=month_start()).count() >= MONTHLY_ASSESSMENTS:
        raise ValueError("Too many checks this month.")
    recent = Delegation.objects.filter(task=task, user=user, status=Delegation.Status.ASSESSED,
                                       created_at__gte=timezone.now() - timezone.timedelta(days=7)).first()
    if run and recent and task.updated_at <= recent.created_at:
        # Reuse a fresh check instead of asking again.
        return Delegation.objects.create(task=task, user=user, run=True, verdict=recent.verdict, plan=recent.plan, reason=recent.reason)
    return Delegation.objects.create(task=task, user=user, run=run)


def suggestions(user, today, limit=5):
    """Today's open tasks that look delegable (checking a few unchecked ones on the way). For the morning brief."""
    from tasks.access import member_visible_q
    from tasks.models import Task

    if not user.has_pro_plus:
        return []
    tasks = (Task.objects.filter(workspace__memberships__user=user, assignee=user, status__is_done=False)
             .filter(member_visible_q(user)).filter(Q(planned_date__lte=today) | Q(due_date__lte=today + timezone.timedelta(days=3)))
             .select_related("workspace", "project").distinct()[:20])
    out = []
    checked = 0
    for task in tasks:
        last = Delegation.objects.filter(task=task, user=user).first()
        if last is None and checked < 6:
            last = Delegation.objects.create(task=task, user=user, run=False, status=Delegation.Status.ASSESSING)
            try:
                assess(last)
                last.status = Delegation.Status.ASSESSED
            except Exception as exc:
                log.warning("Brief triage failed for task %s: %s", task.pk, exc)
                last.status, last.error = Delegation.Status.FAILED, str(exc)[:500]
            last.finished_at = timezone.now()
            last.save()
            checked += 1
        if last and last.status == Delegation.Status.ASSESSED and last.verdict in ("full", "partial"):
            out.append((task, last))
        if len(out) >= limit:
            break
    return out
