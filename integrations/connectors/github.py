import re

from rest_framework import serializers

from .. import github
from ..http import DeliveryError
from ..inbound import done_status, find_member, open_status, tags_by_names, upsert_task
from ..models import Delivery, ExternalLink, Integration
from .base import Connector, clean_schedule

REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
MARKER_RE = re.compile(r"<!--\s*flowly-task:(\d+)\s*-->")
STATUS_LABEL_PREFIX = "flowly: "


def external_id(repo, number):
    return f"{repo.lower()}#{number}"


def issue_number(link: ExternalLink) -> int:
    return int(link.external_id.rsplit("#", 1)[1])


class GitHub(Connector):
    """Two-way sync between a repository's issues and tasks, through the instance's GitHub App."""

    kind = "github"
    outgoing = True

    def clean_config(self, config, integration):
        previous = integration.config if integration else {}
        installation_id = config.get("installation_id", previous.get("installation_id"))
        repo = (config.get("repo") or "").strip()
        if repo and not REPO_RE.match(repo):
            raise serializers.ValidationError({"repo": 'Use the form "owner/repository".'})
        return {
            "installation_id": int(installation_id) if installation_id else None,
            "repo": repo,
            "schedule": clean_schedule(config),
            "sync_labels": bool(config.get("sync_labels", True)),
            "status_labels": bool(config.get("status_labels", False)),
            "create_issues": bool(config.get("create_issues", False)),
            "done_status": config.get("done_status") or None,
            "open_status": config.get("open_status") or None,
        }

    def ready(self, integration):
        return bool(integration.config.get("installation_id") and integration.config.get("repo"))

    def wants(self, integration, event, before, after, changes):
        if not self.ready(integration) or after is None:
            return False
        linked = ExternalLink.objects.filter(integration=integration, task_id=after["id"]).exists()
        if event == "task.created":
            return integration.config.get("create_issues", False) and not linked
        if event == "task.status_changed":
            return linked
        if event == "task.updated":
            return linked and ("title" in changes or "description" in changes)
        return False

    # --- outgoing -----------------------------------------------------------

    def deliver(self, delivery):
        integration = delivery.integration
        cfg = integration.config
        if not self.ready(integration):
            raise DeliveryError("Pick a repository first.", retry=False)
        repo, inst = cfg["repo"], cfg["installation_id"]

        if delivery.event == "ping":
            data = github.installation_api(inst, "GET", f"/repos/{repo}")
            return {"status": 200, "body": f"Connected to {data['full_name']}"}

        task = delivery.payload["task"]
        if delivery.event == "task.created":
            body = (task.get("description") or "") + f"\n\n<!-- flowly-task:{task['id']} -->"
            issue = github.installation_api(inst, "POST", f"/repos/{repo}/issues", {"title": task["title"], "body": body})
            ExternalLink.objects.get_or_create(
                integration=integration,
                external_id=external_id(repo, issue["number"]),
                defaults={"task_id": task["id"], "url": issue["html_url"]},
            )
            return {"status": 201, "body": issue["html_url"]}

        link = ExternalLink.objects.filter(integration=integration, task_id=task["id"]).first()
        if link is None:
            raise DeliveryError("Task is no longer linked to an issue.", retry=False)
        number = issue_number(link)
        path = f"/repos/{repo}/issues/{number}"

        if delivery.event == "task.updated":
            changes = delivery.payload["changes"]
            patch = {}
            if "title" in changes:
                patch["title"] = task["title"]
            if "description" in changes:
                patch["body"] = task["description"]
            github.installation_api(inst, "PATCH", path, patch)
            return {"status": 200, "body": f"Updated #{number}"}

        # task.status_changed
        change = delivery.payload["changes"].get("status_id", {})
        was_done = bool((change.get("from_value") or {}).get("is_done"))
        is_done = task["status"]["is_done"]
        notes = []
        if is_done != was_done:
            state = {"state": "closed", "state_reason": "completed"} if is_done else {"state": "open"}
            github.installation_api(inst, "PATCH", path, state)
            notes.append("closed" if is_done else "reopened")
        if cfg.get("status_labels"):
            old = (change.get("from_value") or {}).get("name")
            if old:
                try:
                    github.installation_api(inst, "DELETE", f"{path}/labels/{STATUS_LABEL_PREFIX}{old}")
                except DeliveryError as exc:
                    if exc.status != 404:
                        raise
            github.installation_api(inst, "POST", f"{path}/labels", {"labels": [STATUS_LABEL_PREFIX + task["status"]["name"]]})
            notes.append(f"labelled {task['status']['name']}")
        return {"status": 200, "body": f"#{number}: {', '.join(notes) or 'no change'}"}

    def info(self, integration):
        app = github.get_app()
        return {
            "app": {"slug": app.slug, "html_url": app.html_url} if app else None,
            "installed": bool(integration.config.get("installation_id")),
        }

    # --- incoming -----------------------------------------------------------

    @staticmethod
    def handle_webhook(event: str, payload: dict) -> list[Delivery]:
        if event == "installation" and payload.get("action") == "deleted":
            inst = payload["installation"]["id"]
            for integration in Integration.objects.filter(kind="github", config__installation_id=inst):
                integration.config = {**integration.config, "installation_id": None}
                integration.save(update_fields=["config"])
            return []
        if event != "issues":
            return []
        inst = (payload.get("installation") or {}).get("id")
        repo = payload["repository"]["full_name"]
        integrations = Integration.objects.filter(
            kind="github", enabled=True, config__installation_id=inst, config__repo__iexact=repo
        ).select_related("workspace", "project")
        return [GitHub().apply_issue_event(i, payload) for i in integrations]

    def apply_issue_event(self, integration, payload) -> Delivery:
        action = payload["action"]
        issue = payload["issue"]
        repo = payload["repository"]["full_name"]
        ext_id = external_id(repo, issue["number"])
        log = Delivery(
            integration=integration,
            direction=Delivery.Direction.IN,
            event=f"issues.{action}",
            payload={"repo": repo, "number": issue["number"], "title": issue["title"], "action": action},
        )

        link = ExternalLink.objects.filter(integration=integration, external_id=ext_id).select_related("task").first()
        if link is None:
            # An issue Flowly itself just opened: link it to its task instead of importing a duplicate.
            marker = MARKER_RE.search(issue.get("body") or "")
            if marker and integration.workspace.tasks.filter(pk=int(marker.group(1))).exists():
                link = ExternalLink.objects.create(
                    integration=integration, task_id=int(marker.group(1)), external_id=ext_id, url=issue["html_url"]
                )
                return self._finish(log, link.task, "ignored", "Linked to the task that created it.")
            if issue["state"] == "closed" or action in ("deleted", "transferred"):
                return self._finish(log, None, "ignored", "Closed or removed issue that isn't tracked.")

        fields, tags = {}, None
        task = link.task if link else None
        if task is None or action == "edited":
            fields["title"] = issue["title"][:255]
            fields["description"] = (issue.get("body") or "")[:20000]
        if task is None or action in ("closed", "reopened"):
            if issue["state"] == "closed":
                fields["status"] = done_status(integration)
            elif task is not None and task.status.is_done:
                fields["status"] = open_status(integration)
        if task is None or action in ("assigned", "unassigned"):
            logins = [a["login"] for a in issue.get("assignees") or []]
            member = next((m for m in (find_member(integration, github_login=l) for l in logins) if m), None)
            if member or action == "unassigned" or task is None:
                fields["assignee"] = member
        labels = [l["name"] for l in issue.get("labels") or [] if not l["name"].startswith(STATUS_LABEL_PREFIX)]
        if integration.config.get("sync_labels", True) and (task is None or action in ("labeled", "unlabeled")):
            tags = tags_by_names(integration, labels)
        fields = {k: v for k, v in fields.items() if v is not None or k == "assignee"}

        task, created = upsert_task(integration, fields, external_id=ext_id, url=issue["html_url"], tags=tags)
        return self._finish(log, task, "success", "Created task." if created else "Updated task.")

    @staticmethod
    def _finish(log, task, status, note):
        from django.utils import timezone

        log.task = task
        log.status = status
        log.response_body = note
        log.finished_at = timezone.now()
        log.save()
        return log
