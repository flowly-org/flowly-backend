"""Turns task changes into notifications for the people they affect (no LLM involved)."""

from datetime import date
from zoneinfo import ZoneInfo

from django.utils import timezone

from .models import ConciergeProfile, Notification


def light(task):
    return {
        "assignee_id": task.assignee_id,
        "priority_id": task.priority_id,
        "planned_date": task.planned_date,
        "status_id": task.status_id,
    }


def local_today(user) -> date:
    profile = ConciergeProfile.objects.filter(user=user).first()
    if profile:
        try:
            return timezone.now().astimezone(ZoneInfo(profile.timezone)).date()
        except Exception:
            pass
    return timezone.localdate()


def is_urgent(task) -> bool:
    """Urgent = carries the highest-ranked tag of its workspace's priority group."""
    if not task.priority_id:
        return False
    top = task.priority.group.tags.order_by("-position", "-id").first()
    return top is not None and top.pk == task.priority_id


def telegram_state(user):
    profile = ConciergeProfile.objects.filter(user=user).first()
    eligible = bool(profile and profile.enabled and profile.alerts and profile.telegram_chat_id and user.has_pro)
    return Notification.Telegram.PENDING if eligible else Notification.Telegram.NONE


def create(user, kind, title, body="", task=None, workspace=None, data=None, telegram=None):
    return Notification.objects.create(
        user=user, kind=kind, title=title[:255], body=body, task=task,
        workspace=workspace or (task.workspace if task else None), data=data or {},
        telegram=telegram if telegram is not None else telegram_state(user),
    )


def _where(task):
    return " / ".join(filter(None, [task.workspace.name, task.project.name if task.project_id else None]))


def task_changed(before: dict | None, task, *, actor=None, source: str | None = None):
    if task is None or not task.assignee_id:
        return None
    if actor is not None and actor.id == task.assignee_id:
        return None  # your own changes don't need telling
    who = (actor.full_name or actor.email) if actor else (source or "Someone")
    created = before is None
    assignee_changed = created or before["assignee_id"] != task.assignee_id
    assignee = task.assignee
    if is_urgent(task) and task.planned_date == local_today(assignee):
        became = created or assignee_changed or before["priority_id"] != task.priority_id or before["planned_date"] != task.planned_date
        if became:
            verb = "added" if created else ("assigned you" if assignee_changed else "flagged")
            return create(
                assignee, Notification.Kind.URGENT, f"Urgent today: {task.title}",
                f"{who} {verb} it ({_where(task)}).", task=task,
            )
    if created and lands_today(task, assignee):
        return new_task(assignee, task, who)
    if assignee_changed:
        when = f" for {task.planned_date:%a %d %b}" if task.planned_date else ""
        return create(
            assignee, Notification.Kind.ASSIGNED, f"{who} assigned you “{task.title}”",
            f"{_where(task)}{when}.", task=task,
        )
    return None


def comment_added(comment):
    task = comment.task
    if not task.assignee_id or task.assignee_id == comment.author_id:
        return None
    who = (comment.author.full_name or comment.author.email) if comment.author else f"@{comment.external_author}"
    excerpt = comment.body.strip().replace("\n", " ")
    return create(
        task.assignee, Notification.Kind.COMMENT, f"{who} commented on “{task.title}”",
        excerpt[:280] + ("…" if len(excerpt) > 280 else ""), task=task,
    )


def lands_today(task, user) -> bool:
    """Planned for today (or already overdue), or due today: it's on today's board."""
    today = local_today(user)
    return bool((task.planned_date and task.planned_date <= today) or task.due_date == today)


def working_now(user) -> bool:
    """Inside the user's working hours (Settings → Calendars), in their time zone."""
    from datetime import datetime

    from accounts.focus import options

    profile = ConciergeProfile.objects.filter(user=user).first()
    try:
        tz = ZoneInfo(profile.timezone) if profile else timezone.get_current_timezone()
    except Exception:
        tz = timezone.get_current_timezone()
    now = timezone.now().astimezone(tz).time()
    o = options(user)
    start, end = (datetime.strptime(o[k], "%H:%M").time() for k in ("day_start", "day_end"))
    return start <= now < end


def new_task(user, task, who):
    """A task that arrived on today's board. Forwarded on Telegram in batches during working hours (unless switched
    off); outside them, the morning brief will bring it up."""
    profile = ConciergeProfile.objects.filter(user=user).first()
    forward = bool(profile and profile.forward_new_tasks and profile.enabled and profile.telegram_chat_id and user.has_pro
                   and working_now(user))
    return create(
        user, Notification.Kind.NEW_TASK, f"New for today: {task.title}", f"{who} added it ({_where(task)}).", task=task,
        data={"who": who}, telegram=Notification.Telegram.PENDING if forward else Notification.Telegram.NONE,
    )
