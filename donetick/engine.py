"""Two-way sync between Donetick chores and Flowly tasks.

Donetick → Flowly: its webhook (instant: created, completed, skipped) plus a poll every few minutes (edits,
reschedules, and anything a webhook missed). Flowly → Donetick: completing, renaming or rescheduling a linked task
(see signals.py), sent through a small retried outbox.

Each occurrence of a recurring chore is its own Flowly task: when it's done, the next one is created for the new
due date, so time tracking and history stay per occurrence.
"""

import html
import logging
import re
import time
from datetime import datetime, timedelta

from django.db.models import Max
from django.utils import timezone

from integrations.crypto import decrypt
from integrations.http import DeliveryError, send

from .models import DonetickAccount, DonetickLink, DonetickOutbox, DonetickSync

log = logging.getLogger("flowly.donetick")
ONE_OFF = {"once", "no_repeat", "trigger", "always"}
POLL_EVERY = timedelta(minutes=10)
BACKOFF = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6)]
AUTHOR = "Donetick"


# --- helpers -------------------------------------------------------------------------


def norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def text_of(description) -> str:
    """Donetick descriptions can be HTML (rich text editor): keep readable plain text."""
    value = (description or "").strip()
    if "<" in value and ">" in value:
        value = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", value)
        value = re.sub(r"(?i)<li[^>]*>", "- ", value)
        value = html.unescape(re.sub(r"<[^>]+>", "", value))
        value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value


def labels_of(chore) -> list[str]:
    return [str(lab.get("name", "")).strip() for lab in (chore.get("labelsV2") or []) if lab.get("name")]


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def assignee_ids(chore) -> set[int]:
    ids = {a.get("userId") for a in (chore.get("assignees") or [])}
    if chore.get("assignedTo"):
        ids.add(chore["assignedTo"])
    return {i for i in ids if i}


# --- sources ------------------------------------------------------------------------


class Source:
    """A Donetick connection: a workspace integration or a personal account, behind one interface."""

    def __init__(self, integration=None, account=None):
        self.integration, self.account = integration, account

    @classmethod
    def of_link(cls, link):
        return cls(integration=link.integration, account=link.account)

    # identity & settings

    @property
    def obj(self):
        return self.integration or self.account

    @property
    def link_filter(self) -> dict:
        return {"integration": self.integration} if self.integration else {"account": self.account}

    def links(self):
        return DonetickLink.objects.filter(**self.link_filter)

    @property
    def enabled(self) -> bool:
        return bool(self.obj and self.obj.enabled)

    @property
    def base_url(self) -> str:
        return (self.integration.config.get("base_url") if self.integration else self.account.base_url).rstrip("/")

    @property
    def token(self) -> str:
        raw = self.integration.credentials.get("api_key", "") if self.integration else self.account.token
        return decrypt(raw) if raw else ""

    @property
    def owner(self):
        return self.integration.created_by if self.integration else self.account.user

    @property
    def write_back(self) -> bool:
        return self.integration.config.get("write_back", True) if self.integration else self.account.write_back

    @property
    def label(self) -> str:
        return self.integration.name if self.integration else self.account.name

    def state(self) -> DonetickSync:
        state, _ = DonetickSync.objects.get_or_create(**({"integration": self.integration} if self.integration else {"account": self.account}))
        return state

    def tz(self):
        from calendars.sync import user_tz

        return user_tz(self.owner) if self.owner else timezone.get_default_timezone()

    def today(self):
        return timezone.now().astimezone(self.tz()).date()

    def local_due(self, chore) -> str:
        dt = parse_dt(chore.get("nextDueDate"))
        return dt.astimezone(self.tz()).date().isoformat() if dt else ""

    def wire_due(self, day) -> str | None:
        """A local date as an RFC3339 instant at local noon (so no timezone moves it to another day)."""
        if not day:
            return None
        return datetime.combine(day, datetime.min.time().replace(hour=12), tzinfo=self.tz()).isoformat()

    # routing

    def route(self, chore):
        """(workspace, project) the chore belongs in, or None when this connection doesn't take it."""
        from tasks.models import Project

        labels = [x.lower() for x in labels_of(chore)]
        if self.integration:
            cfg = self.integration.config
            wanted = [x.lower() for x in cfg.get("labels") or []]
            if wanted and not set(wanted) & set(labels):
                return None
            mapping = {k.lower(): v for k, v in (cfg.get("label_projects") or {}).items()}
            project = self.integration.project
            for name in labels:
                if name in mapping:
                    project = Project.objects.filter(pk=mapping[name], workspace=self.integration.workspace_id).first() or project
                    break
            return self.integration.workspace, project
        account = self.account
        if account.only_mine and account.donetick_user_id and account.donetick_user_id not in assignee_ids(chore):
            return None
        member_ws = set(account.user.memberships.values_list("workspace_id", flat=True))
        target = None
        for r in account.routes.select_related("workspace", "project"):
            if r.label.lower() in labels and r.workspace_id in member_ws:
                target = (r.workspace, r.project if r.project and r.project.workspace_id == r.workspace_id else None)
                break
        if target is None and account.default_workspace_id in member_ws:
            p = account.default_project
            target = (account.default_workspace, p if p and p.workspace_id == account.default_workspace_id else None)
        if target is None:
            return None
        # A workspace that has its own integration with this server gets the chore from there (no duplicates).
        from integrations.models import Integration

        for other in Integration.objects.filter(workspace=target[0], kind="donetick", enabled=True):
            if norm_url(other.config.get("base_url")) == norm_url(account.base_url):
                return None
        return target

    def member_map(self) -> dict[int, int]:
        """Donetick user id → Flowly user id (configured, else matched by name)."""
        from accounts.models import User

        if self.account:
            return {self.account.donetick_user_id: self.account.user_id} if self.account.donetick_user_id else {}
        ws = self.integration.workspace_id
        users = list(User.objects.filter(memberships__workspace=ws))
        out = {}
        for m in self.state().members:
            keys = {str(m.get("username", "")).lower(), str(m.get("displayName", "")).lower()} - {""}
            for u in users:
                names = {u.email.lower(), u.email.split("@")[0].lower(), (u.full_name or "").lower()} - {""}
                if keys & names:
                    out[m["userId"]] = u.id
                    break
        for k, v in (self.integration.config.get("members") or {}).items():
            if v and any(u.id == int(v) for u in users):
                out[int(k)] = int(v)
        return out

    def assignee(self, chore):
        from accounts.models import User

        if self.account:
            return self.account.user
        uid = chore.get("assignedTo")
        mapped = self.member_map().get(uid) if uid else None
        return User.objects.filter(pk=mapped).first() if mapped else None

    def donetick_uid(self, user) -> int | None:
        if user is None:
            return None
        for uid, fid in self.member_map().items():
            if fid == user.id:
                return uid
        return None

    @property
    def visibility(self) -> str:
        return "private" if self.account and self.account.private else "team"


def sources():
    from integrations.models import Integration

    for integration in Integration.objects.filter(kind="donetick", enabled=True).select_related("workspace", "project", "created_by"):
        yield Source(integration=integration)
    for account in DonetickAccount.objects.filter(enabled=True).select_related("user"):
        yield Source(account=account)


def by_hook_token(token):
    from integrations.models import Integration

    integration = Integration.objects.filter(kind="donetick", token=token).first()
    if integration:
        return Source(integration=integration)
    account = DonetickAccount.objects.filter(hook_token=token).first()
    return Source(account=account) if account else None


# --- the Donetick API ------------------------------------------------------------------


def api(src: Source, method, path, payload=None):
    if not src.token:
        raise DeliveryError("Add a Donetick access token first.", retry=False)
    res = send(method, f"{src.base_url}{path}", json=payload, timeout=20,
               headers={"secretkey": src.token, "Accept": "application/json"})
    try:
        return res.json() if res.content else None
    except ValueError as exc:
        raise DeliveryError("Donetick answered with something that isn't JSON: is the address right?", retry=False) from exc


# --- Flowly side ------------------------------------------------------------------------


def _priority(workspace, level):
    from tasks.models import Tag

    if not level:
        return None
    tags = list(Tag.objects.filter(workspace=workspace, group__kind="priority").order_by("position", "id"))
    return tags[min(int(level), len(tags)) - 1] if tags else None


def _save(task, *, created=False, before=None, before_light=None):
    from concierge import notify
    from integrations.events import emit, has_outgoing

    from .signals import quiet

    with quiet():
        task.save()
    if has_outgoing(task.workspace_id):
        emit(before, task)
    notify.task_changed(before_light, task, source=AUTHOR)


def _snapshots(task):
    from concierge import notify
    from integrations.events import has_outgoing, snapshot

    return (snapshot(task) if has_outgoing(task.workspace_id) else None), notify.light(task)


def create_occurrence(src: Source, chore, workspace, project, due: str):
    from datetime import date

    from tasks.models import Task
    from tasks.services import default_status

    top = Task.objects.filter(workspace=workspace).aggregate(m=Max("position"))["m"] or 0
    day = date.fromisoformat(due) if due else None
    assignee = src.assignee(chore)
    task = Task(
        workspace=workspace, project=project, title=str(chore.get("name") or "Chore")[:255],
        description=text_of(chore.get("description")), status=default_status(workspace), position=top + 1024,
        planned_date=day, due_date=day, priority=_priority(workspace, chore.get("priority")),
        assignee=assignee, created_by=src.owner, visibility=src.visibility,
    )
    _save(task, created=True)
    return DonetickLink.objects.create(**src.link_filter, chore_id=chore["id"], due=due, task=task, seen=seen_of(chore))


def seen_of(chore) -> dict:
    return {"name": chore.get("name") or "", "description": text_of(chore.get("description")), "assignee": chore.get("assignedTo")}


def close_occurrence(link: DonetickLink, note=""):
    """The occurrence is done in Donetick: tick the task (if it still exists and isn't done)."""
    from tasks.models import Comment, Tag

    link.done = True
    link.save(update_fields=["done"])
    task = link.task
    if task is None or task.status.is_done:
        return
    done = Tag.objects.filter(workspace=task.workspace_id, group__kind="status", is_done=True).first()
    if done is None:
        return
    before, light = _snapshots(task)
    task.status = done
    task.completed_at = timezone.now()
    _save(task, before=before, before_light=light)
    if note:
        Comment.objects.create(task=task, body=note, source=Comment.Source.DONETICK, external_author=AUTHOR)


def reconcile_chore(src: Source, chore, *, today=None):
    from datetime import date

    if not chore or not chore.get("id"):
        return
    today = today or src.today()
    route = src.route(chore) if chore.get("isActive", True) else None
    links = src.links().filter(chore_id=chore["id"])
    current = links.filter(done=False).select_related("task__status").first()
    due = src.local_due(chore)
    if route is None:
        if current:  # no longer ours (label or assignee changed): stop syncing, keep the task
            current.done = True
            current.save(update_fields=["done"])
        return
    workspace, project = route
    if current is None:
        if not links.filter(due=due).exists():  # this occurrence may already be done (or its task deleted)
            create_occurrence(src, chore, workspace, project, due)
        return
    if current.task is None:  # deleted in Flowly: leave this occurrence alone, pick up the next one
        if current.due != due:
            current.done = True
            current.save(update_fields=["done"])
            if not links.filter(due=due).exists():
                create_occurrence(src, chore, workspace, project, due)
        return
    task = current.task
    seen = current.seen or {}
    if current.due != due:
        recurring = chore.get("frequencyType") not in ONE_OFF
        moved_on = current.due and due and due > current.due and current.due <= today.isoformat()
        if recurring and moved_on and not seen.get("skipped"):
            close_occurrence(current, "✓ Done in Donetick.")
            create_occurrence(src, chore, workspace, project, due)
            return
        before, light = _snapshots(task)
        task.planned_date = task.due_date = date.fromisoformat(due) if due else None
        _save(task, before=before, before_light=light)
        current.due = due
        seen.pop("skipped", None)
    fresh = seen_of(chore)
    changed = {}
    if fresh["name"] != seen.get("name"):
        changed["title"] = fresh["name"][:255] or task.title
    if fresh["description"] != seen.get("description"):
        changed["description"] = fresh["description"]
    if src.integration and fresh["assignee"] != seen.get("assignee"):
        changed["assignee"] = src.assignee(chore)
    if changed:
        before, light = _snapshots(task)
        for k, v in changed.items():
            setattr(task, k, v)
        _save(task, before=before, before_light=light)
    current.seen = {**seen, **fresh}
    current.save(update_fields=["due", "seen"])


def poll(src: Source) -> DonetickSync:
    """Fetch every chore and bring the tasks in line. Raises DeliveryError (also recorded on the state)."""
    state = src.state()
    try:
        chores = api(src, "GET", "/eapi/v1/chore") or []
        try:
            members = api(src, "GET", "/eapi/v1/circle/members") or []
            state.members = [{"userId": m.get("userId"), "username": m.get("username", ""), "displayName": m.get("displayName", "")}
                             for m in members if m.get("userId")]
        except DeliveryError as exc:
            log.info("Donetick members for %s: %s", src.label, exc)
        state.save(update_fields=["members"])
        today = src.today()
        active = set()
        for chore in chores:
            if not isinstance(chore, dict) or not chore.get("id"):
                continue
            if chore.get("isActive", True):
                active.add(chore["id"])
            reconcile_chore(src, chore, today=today)
        # Chores gone from the list were completed (one-off), archived or deleted.
        for link in src.links().filter(done=False).exclude(chore_id__in=active).select_related("task__status"):
            close_occurrence(link, "✓ Done or removed in Donetick.")
        state.labels = sorted({name for c in chores if isinstance(c, dict) for name in labels_of(c)}, key=str.lower)[:200]
        state.chores = len(active)
        state.error = ""
    except DeliveryError as exc:
        state.error = str(exc) + (f": {exc.body[:200]}" if getattr(exc, "body", "") else "")
        raise
    finally:
        state.synced_at = timezone.now()
        state.next_poll_at = timezone.now() + POLL_EVERY
        state.save()
    return state


def handle_hook(src: Source, payload: dict) -> str:
    kind = payload.get("type") or ""
    chore = (payload.get("data") or {}).get("chore") or {}
    if not chore.get("id"):
        return "ignored"
    current = src.links().filter(chore_id=chore["id"], done=False).select_related("task__status").first()
    if kind == "task.created":
        # Not applied here: Donetick sends this before answering our own "create chore" call, and the worker
        # (which makes that call) picks the new chore up on its next poll, a few seconds away, without duplicates.
        pass
    elif kind == "task.completed":
        # The payload carries the chore as it was before completion: only close that occurrence.
        if current and current.due == src.local_due(chore):
            who = (payload.get("data") or {}).get("display_name") or (payload.get("data") or {}).get("username") or ""
            close_occurrence(current, f"✓ Done in Donetick{' by ' + who if who else ''}.")
    elif kind == "task.skipped":
        if current:
            current.seen = {**(current.seen or {}), "skipped": True}
            current.save(update_fields=["seen"])
    else:
        return "ignored"
    # Fetch the new state (next due date, next assignee) soon.
    DonetickSync.objects.filter(pk=src.state().pk).update(next_poll_at=timezone.now())
    return kind


# --- Flowly → Donetick ---------------------------------------------------------------


def queue(action, *, link=None, integration=None, task=None, payload=None):
    return DonetickOutbox.objects.create(action=action, link=link, integration=integration, task=task,
                                         payload=payload or {}, next_attempt_at=timezone.now())


def send_one(item: DonetickOutbox):
    src = Source.of_link(item.link) if item.link_id else Source(integration=item.integration)
    if not src.obj or not src.enabled:
        item.done, item.error = True, "Connection disabled or removed."
        item.save()
        return
    item.attempts += 1
    try:
        if item.action == "complete":
            link = item.link
            if link.done:
                item.done = True
            else:
                performer = src.donetick_uid(item.task.assignee if item.task and item.task.assignee_id else src.owner)
                query = f"?completedBy={performer}" if performer else ""
                updated = api(src, "POST", f"/eapi/v1/chore/{link.chore_id}/complete{query}")
                link.done = True
                link.save(update_fields=["done"])
                if isinstance(updated, dict) and updated.get("id"):
                    reconcile_chore(src, updated)  # the next occurrence of a recurring chore
        elif item.action == "update":
            link = item.link
            updated = api(src, "PUT", f"/eapi/v1/chore/{link.chore_id}", item.payload)
            if isinstance(updated, dict):
                # Only what we sent counts as seen: a change made in Donetick meanwhile still comes through.
                fresh, sent = seen_of(updated), item.payload
                link.seen = {**(link.seen or {}), **{k: fresh[k] for k, key in (("name", "name"), ("description", "description")) if key in sent}}
                if "dueDate" in sent:
                    link.due = src.local_due(updated)
                link.save(update_fields=["seen", "due"])
        elif item.action == "create":
            task = item.task
            created = api(src, "POST", "/eapi/v1/chore", item.payload)
            if isinstance(created, dict) and created.get("id") and task:
                DonetickLink.objects.create(**src.link_filter, chore_id=created["id"], due=src.local_due(created), task=task, seen=seen_of(created))
        item.done, item.error = True, ""
    except DeliveryError as exc:
        item.error = str(exc) + (f": {exc.body[:300]}" if exc.body else "")
        if exc.retry and item.attempts <= len(BACKOFF):
            item.next_attempt_at = timezone.now() + BACKOFF[item.attempts - 1]
        else:
            item.done = True
            state = src.state()
            state.error = f"Couldn't update Donetick ({item.action}): {item.error}"[:2000]
            state.save(update_fields=["error"])
    item.save()


def send_outbox(limit=10) -> int:
    due = list(DonetickOutbox.objects.filter(done=False, next_attempt_at__lte=timezone.now())
               .select_related("link__integration", "link__account", "integration", "task")[:limit])
    for item in due:
        try:
            send_one(item)
        except Exception:
            log.exception("Donetick outbox %s crashed", item.pk)
            item.done, item.error = True, "Internal error"
            item.save()
    return len(due)


_last_poll_check = 0.0


def tick() -> int:
    """Worker: send queued changes; poll connections that are due (checked every 30 s)."""
    global _last_poll_check
    done = send_outbox()
    if time.monotonic() - _last_poll_check < 30:
        return done
    _last_poll_check = time.monotonic()
    now = timezone.now()
    for src in sources():
        state = src.state()
        if state.next_poll_at and state.next_poll_at > now:
            continue
        try:
            poll(src)
        except DeliveryError as exc:
            log.info("Donetick poll %s: %s", src.label, exc)
        except Exception:
            log.exception("Donetick poll %s crashed", src.label)
    return done


def webhook_url(token: str) -> str:
    from instance import conf

    base = conf.get("API_PUBLIC_URL") or ""
    return f"{base}/api/hooks/donetick/{token}/"
