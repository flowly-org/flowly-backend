"""Handing a task to an agent on Stairway, following up, and collecting what it produced.

1. `start`: an experiment `flowly-<ref>` is created on the workspace's (or project's) Stairway, briefed with the task
   (title, notes, checklist, the attributes it uses, the discussion) and asked to leave its results in
   ./deliverables and end with a summary. Stairway calls back with every finished reply.
2. `receive` (the callback): the reply becomes a comment on the task; the worker then (`collect`) fetches
   ./deliverables, attaches the files to the task (replacing the previous batch), and tells the person it's ready.
3. `follow_up` / `stop`: more instructions, or stop the agent.
"""

import base64
import hashlib
import hmac
import io
import logging
import re
import zipfile

from django.core.files.base import ContentFile

from instance import conf

from . import client
from .models import StairwayRun

log = logging.getLogger("flowly.stairway")
MAX_FILES = 30


class RunError(Exception):
    pass


def integration_for(task):
    """The enabled Stairway integration that covers the task: the project's own first, else the workspace's."""
    from integrations.models import Integration

    qs = Integration.objects.filter(workspace_id=task.workspace_id, kind="stairway", enabled=True)
    return (qs.filter(project_id=task.project_id).first() if task.project_id else None) or qs.filter(project__isnull=True).first()


def brief(task, instructions="", integration=None) -> str:
    from tasks import attributes

    values = attributes.Values()
    lines = ["You're working on a task from Flowly, the team's task planner. Do the work in this folder.", "",
             f"Task: {attributes.rendered_title(task, values)}",
             f"Workspace: {task.workspace.name}" + (f" · project {task.project.name}" if task.project_id else "")]
    if task.due_date:
        lines.append(f"Due: {task.due_date}")
    if task.description.strip():
        lines += ["", "Notes:", attributes.render(task.description, attributes.for_task(task, values)).strip()[:6000]]
    if task.checklist:
        lines += ["", "Checklist:"] + [f"- [{'x' if i.get('done') else ' '}] {i.get('text', '')}" for i in task.checklist]
    if used := attributes.context(task, values):
        lines += ["", "Context the team attached:", used[:4000]]
    comments = list(task.comments.exclude(source="stairway").select_related("author").order_by("-created_at")[:10])[::-1]
    if comments:
        lines += ["", "Discussion so far:"] + [
            f"- {(c.author.full_name or c.author.email) if c.author else c.external_author or 'Someone'}: {c.body[:600]}"
            for c in comments]
    links = [a for a in task.attachments.all() if a.url]
    if links:
        lines += ["", "Linked documents:"] + [f"- {a.name}: {a.url}" for a in links[:20]]
    extra = "\n".join(x for x in [(integration.config.get("instructions") if integration else ""), instructions] if x)
    if extra.strip():
        lines += ["", "Instructions:", extra.strip()]
    lines += ["", "How to work:",
              "- Create and edit files with your file tools (Write/Edit), not shell commands such as cat or echo: "
              "shell commands may stop and wait for someone to approve them.",
              "", "When you're done:",
              "- Put everything the person should look at in ./deliverables/ (documents, code, data, images).",
              "- End your reply with a short summary: what you did, what is in ./deliverables, and what is left for "
              "them to decide.",
              "If you need a decision first, ask in your reply and stop: they will answer from Flowly."]
    return "\n".join(lines)


def _experiment_name(task) -> str:
    base = f"flowly-{task.ref.lower()}"
    taken = set(StairwayRun.objects.filter(experiment__startswith=base).values_list("experiment", flat=True))
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name


def _comment(task, body, author=None):
    from tasks.models import Comment

    return Comment.objects.create(task=task, author=author, body=body[:20000],
                                  source=Comment.Source.FLOWLY if author else Comment.Source.STAIRWAY,
                                  external_author="" if author else "Stairway")


def callback_url(run) -> str:
    return f"{conf.get('API_PUBLIC_URL') or ''}/api/stairway/callback/{run.token}/"


def start(task, user, instructions="") -> StairwayRun:
    integration = integration_for(task)
    if integration is None:
        raise RunError("No Stairway is connected to this workspace or project (Integrations → Stairway).")
    if StairwayRun.objects.filter(task=task, status=StairwayRun.Status.WORKING).exists():
        raise RunError("Stairway is already working on this task: send it a follow-up instead.")
    run = StairwayRun.objects.create(task=task, integration=integration, experiment=_experiment_name(task), created_by=user)
    cfg = integration.config
    args = {"name": run.experiment, "prompt": brief(task, instructions, integration), "agent": cfg.get("agent", "claude"),
            "permission_mode": cfg.get("permission_mode", "acceptEdits"), "callback_url": callback_url(run),
            "callback_secret": run.secret}
    if cfg.get("model") and args["agent"] != "claude":
        args["model"] = cfg["model"]
    try:
        client.tool(integration, "create_experiment", timeout=60, **args)
        info = client.tool_json(integration, "experiment_info", name=run.experiment)
        run.session_url = info.get("session_url") or ""
    except client.StairwayError as exc:
        run.status, run.error = StairwayRun.Status.FAILED, str(exc)[:500]
        run.save(update_fields=["status", "error", "updated_at"])
        raise RunError(str(exc)) from exc
    run.save(update_fields=["session_url", "updated_at"])
    who = user.full_name or user.email
    _comment(task, f"{who} handed this task to Stairway (experiment {run.experiment})."
             + (f" Instructions: {instructions}" if instructions else ""))
    return run


def follow_up(run, user, text) -> StairwayRun:
    text = (text or "").strip()
    if not text:
        raise RunError("Write what Stairway should do next.")
    if run.integration is None:
        raise RunError("The Stairway integration was removed.")
    try:
        client.tool(run.integration, "send_prompt", name=run.experiment, prompt=text[:8000])
    except client.StairwayError as exc:
        raise RunError(str(exc)) from exc
    run.status = StairwayRun.Status.WORKING
    run.save(update_fields=["status", "updated_at"])
    _comment(run.task, f"To Stairway: {text}", author=user)
    return run


def stop(run) -> StairwayRun:
    if run.integration is not None:
        try:
            client.tool(run.integration, "stop_experiment", name=run.experiment)
        except client.StairwayError as exc:
            log.warning("Stopping %s: %s", run.experiment, exc)
    run.status = StairwayRun.Status.STOPPED
    run.save(update_fields=["status", "updated_at"])
    return run


def answer(run, user, allow: bool) -> StairwayRun:
    """Yes or No to the permission the agent is waiting on."""
    if run.integration is None:
        raise RunError("The Stairway integration was removed.")
    try:
        client.tool(run.integration, "answer_permission", name=run.experiment, allow=bool(allow))
    except client.StairwayError as exc:
        raise RunError(str(exc)) from exc
    who = user.full_name or user.email
    _comment(run.task, f"{who} {'allowed' if allow else 'refused'} what Stairway asked:\n{run.waiting_for[-600:]}", author=user)
    run.status, run.waiting_for = StairwayRun.Status.WORKING, ""
    run.save(update_fields=["status", "waiting_for", "updated_at"])
    return run


def waiting(run, payload: dict):
    """The agent stopped to ask for permission: show what it wants and tell its people."""
    from concierge import notify
    from concierge.models import Notification

    if run.status == StairwayRun.Status.STOPPED:
        return
    # The agent's own Yes/No menu is left out: Allow and Refuse stand in for it.
    text = re.split(r"\n\s*Do you want to proceed\?", str(payload.get("text") or ""))[0].strip()
    run.status, run.waiting_for = StairwayRun.Status.WAITING, text[-4000:]
    run.session_url = payload.get("url") or run.session_url
    run.save(update_fields=["status", "waiting_for", "session_url", "updated_at"])
    task = run.task
    for person in {u for u in (task.assignee, run.created_by) if u is not None}:
        notify.create(person, Notification.Kind.COMMENT, f"Stairway needs your OK on “{task.title}”",
                      "Open the task to allow or refuse what the agent wants to do.", task=task)


def verify(run, body: bytes, signature: str) -> bool:
    good = "sha256=" + hmac.new(run.secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, signature or "")


def receive(run, payload: dict):
    """A finished reply: comment it now, collect the deliverables in the worker."""
    text = str(payload.get("text") or "").strip()
    if not text or run.status == StairwayRun.Status.STOPPED:
        return
    run.last_reply = text[:20000]
    run.replies += 1
    run.session_url = payload.get("url") or run.session_url
    run.collect = True
    run.waiting_for = ""
    run.save(update_fields=["last_reply", "replies", "session_url", "collect", "waiting_for", "updated_at"])
    _comment(run.task, text)


def collect(run):
    """Attach ./deliverables (replacing the previous batch), mark the run ready for review and tell its people."""
    from concierge import notify
    from concierge.models import Notification
    from tasks.models import Attachment, uploads_enabled

    run.collect = False
    run.status = StairwayRun.Status.REVIEW
    files, note = [], ""
    if run.integration is not None:
        try:
            found = client.tool_json(run.integration, "experiment_files", timeout=120, name=run.experiment,
                                     path="deliverables", max_mb=min(int(conf.get("ATTACHMENT_MAX_MB") or 15), 50))
        except client.StairwayError as exc:
            found, note = {}, f"Couldn't fetch the deliverables: {exc}"
        if found.get("zip_base64"):
            if not uploads_enabled():
                note = "Deliverables: " + ", ".join(f["path"] for f in found["files"][:MAX_FILES]) + \
                       " (file uploads aren't set up here, so they stay on Stairway)."
            else:
                Attachment.objects.filter(pk__in=run.attachment_ids, task=run.task).delete()
                archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(found["zip_base64"])))
                for name in archive.namelist()[:MAX_FILES]:
                    data = archive.read(name)
                    att = Attachment(task=run.task, kind=Attachment.Kind.FILE, name=f"deliverables/{name}"[:255],
                                     size=len(data), content_type=_mime(name), meta={"stairway": run.experiment})
                    att.file.save(name.split("/")[-1], ContentFile(data), save=True)
                    files.append(att.pk)
                run.attachment_ids = files
        if found.get("skipped"):
            note += (" " if note else "") + "Too large to attach: " + ", ".join(found["skipped"][:10]) + "."
    run.save(update_fields=["collect", "status", "attachment_ids", "updated_at"])
    if note:
        _comment(run.task, note)
    task = run.task
    people = {u for u in (task.assignee, run.created_by) if u is not None}
    excerpt = " ".join(run.last_reply.split())[:280]
    attached = f" {len(files)} file(s) attached." if files else ""
    for person in people:
        notify.create(person, Notification.Kind.COMMENT, f"Stairway finished “{task.title}”", excerpt + attached, task=task)


def collect_due():
    """Worker: deliverables of the runs that just replied."""
    for run in StairwayRun.objects.filter(collect=True).select_related("task", "integration", "created_by", "task__assignee")[:5]:
        try:
            collect(run)
        except Exception:
            log.exception("Collecting %s failed", run.experiment)
            StairwayRun.objects.filter(pk=run.pk).update(collect=False, error="Collecting the deliverables failed.")


def _mime(name: str) -> str:
    import mimetypes

    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def runs_for(task):
    return StairwayRun.objects.filter(task=task)


def data(run) -> dict:
    return {"id": run.id, "experiment": run.experiment, "status": run.status, "session_url": run.session_url,
            "last_reply": run.last_reply, "waiting_for": run.waiting_for, "replies": run.replies, "error": run.error, "files": len(run.attachment_ids),
            "created_at": run.created_at, "updated_at": run.updated_at,
            "editor_url": f"{run.integration.config['url']}/code/{run.experiment}/" if run.integration else None}


