"""Sprints (rollover, burndown, review), the async daily standup, and posting summaries to the team chat."""

import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from tasks.models import Task

from .models import AgileSetup, Sprint, StandupEntry, StandupRound

log = logging.getLogger("flowly.agile")
SUMMARY_AFTER = timedelta(hours=2)


def tz_of(setup):
    try:
        return ZoneInfo(setup.timezone)
    except Exception:
        return ZoneInfo("UTC")


def local_now(setup, now=None):
    return (now or timezone.now()).astimezone(tz_of(setup))


def scope_tasks(setup):
    qs = Task.objects.filter(workspace=setup.workspace)
    return qs.filter(project=setup.project) if setup.project_id else qs


def members(setup):
    from accounts.models import User

    return User.objects.filter(memberships__workspace=setup.workspace, is_bot=False, is_active=True).distinct()


# --- sprints --------------------------------------------------------------------------


def _first_start(setup, today):
    return today - timedelta(days=(today.weekday() - setup.sprint_start) % 7)


def new_sprint(setup, start, status=Sprint.Status.PLANNED):
    number = (setup.sprints.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    return Sprint.objects.create(setup=setup, number=number, start=start, end=start + timedelta(days=7 * setup.sprint_weeks - 1), status=status)


def current(setup, today=None):
    today = today or local_now(setup).date()
    return setup.sprints.filter(status=Sprint.Status.ACTIVE).first() or ensure(setup, today)


def ensure(setup, today):
    """An active sprint covering today (the first one starts on the last sprint-start weekday)."""
    active = setup.sprints.filter(status=Sprint.Status.ACTIVE).first()
    if active:
        return active
    planned = setup.sprints.filter(status=Sprint.Status.PLANNED, start__lte=today).order_by("start").first()
    if planned:
        planned.status = Sprint.Status.ACTIVE
        planned.save(update_fields=["status"])
        return planned
    last = setup.sprints.order_by("-end").first()
    start = last.end + timedelta(days=1) if last and last.end >= today - timedelta(days=7 * setup.sprint_weeks) else _first_start(setup, today)
    while start + timedelta(days=7 * setup.sprint_weeks) <= today:  # skip sprints missed while switched off
        start += timedelta(days=7 * setup.sprint_weeks)
    return new_sprint(setup, start, Sprint.Status.ACTIVE)


def next_sprint(sprint):
    nxt = sprint.setup.sprints.filter(start__gt=sprint.end).order_by("start").first()
    return nxt or new_sprint(sprint.setup, sprint.end + timedelta(days=1))


def numbers(sprint):
    tasks = list(sprint.tasks.select_related("status"))
    done = [t for t in tasks if t.status.is_done]
    est = lambda items: sum(t.estimate_minutes or 0 for t in items)  # noqa: E731
    return {"committed": len(tasks), "done": len(done), "open": len(tasks) - len(done),
            "committed_minutes": est(tasks), "done_minutes": est(done)}


@transaction.atomic
def close(sprint, now=None):
    """Sprint review: freeze the numbers, carry unfinished work over, start the next sprint, tell the team."""
    if sprint.status == Sprint.Status.CLOSED:
        return sprint
    setup = sprint.setup
    review = numbers(sprint)
    nxt = next_sprint(sprint)
    open_tasks = sprint.tasks.filter(status__is_done=False)
    review["carried_over"] = open_tasks.count()
    review["carried_titles"] = list(open_tasks.values_list("title", flat=True)[:20])
    review["done_titles"] = list(sprint.tasks.filter(status__is_done=True).values_list("title", flat=True)[:30])
    if setup.carry_over:
        open_tasks.update(sprint=nxt)
    previous = list(setup.sprints.filter(status=Sprint.Status.CLOSED).order_by("-end")[:3])
    velocities = [p.review.get("done_minutes", 0) for p in previous] + [review["done_minutes"]]
    review["velocity_minutes"] = round(sum(velocities) / len(velocities))
    sprint.review, sprint.status, sprint.closed_at = review, Sprint.Status.CLOSED, now or timezone.now()
    sprint.save()
    if nxt.status == Sprint.Status.PLANNED and nxt.start <= local_now(setup, now).date():
        nxt.status = Sprint.Status.ACTIVE
        nxt.save(update_fields=["status"])
    transaction.on_commit(lambda: announce_review(sprint))
    return sprint


def review_text(sprint) -> str:
    r = sprint.review
    lines = [f"🏁 {sprint.setup.label} · {sprint.name} review ({sprint.start:%d %b} – {sprint.end:%d %b})",
             f"Done {r['done']}/{r['committed']} tasks" + (f" ({r['done_minutes'] // 60}h of {r['committed_minutes'] // 60}h estimated)" if r["committed_minutes"] else "") + "."]
    if sprint.goal:
        lines.append(f"Goal: {sprint.goal}")
    if r.get("done_titles"):
        lines.append("Shipped: " + "; ".join(r["done_titles"][:8]) + ("…" if len(r["done_titles"]) > 8 else ""))
    if r.get("carried_over"):
        lines.append(f"Carried over: {r['carried_over']} ({'; '.join(r['carried_titles'][:5])})")
    if r.get("velocity_minutes"):
        lines.append(f"Velocity (last sprints): about {r['velocity_minutes'] // 60}h of estimated work per sprint.")
    if sprint.setup.retro:
        lines.append("Retrospective: add what went well and what to improve on the sprint page.")
    return "\n".join(lines)


def announce_review(sprint):
    from concierge import notify
    from concierge.models import Notification

    text = review_text(sprint)
    for user in members(sprint.setup):
        notify.create(user, Notification.Kind.BRIEF, f"{sprint.setup.label}: {sprint.name} review", text,
                      workspace=sprint.setup.workspace, data={"url": f"/w/{sprint.setup.workspace_id}/sprints"},
                      telegram=Notification.Telegram.NONE)
    post_to_team(sprint.setup, text)


def burndown(sprint):
    """Remaining work per day: estimated minutes (or task count without estimates), and the ideal line."""
    tasks = list(sprint.tasks.select_related("status"))
    use_minutes = any(t.estimate_minutes for t in tasks)
    weight = (lambda t: t.estimate_minutes or 30) if use_minutes else (lambda t: 1)  # noqa: E731
    total = sum(weight(t) for t in tasks)
    tz = tz_of(sprint.setup)
    today = local_now(sprint.setup).date()
    days = (sprint.end - sprint.start).days + 1
    out = []
    for i in range(days):
        day = sprint.start + timedelta(days=i)
        ideal = round(total * (1 - (i + 1) / days), 1)
        remaining = None
        if day <= today:
            end = datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=tz)
            remaining = total - sum(weight(t) for t in tasks if t.status.is_done and t.completed_at and t.completed_at < end)
        out.append({"date": day.isoformat(), "remaining": remaining, "ideal": ideal})
    return {"unit": "minutes" if use_minutes else "tasks", "total": total, "days": out}


def capacity(sprint):
    """Team capacity in the sprint: working days × each member's daily capacity × 0.7 (meetings, interruptions)."""
    from accounts.focus import options

    workdays = sum(1 for i in range((sprint.end - sprint.start).days + 1) if (sprint.start + timedelta(days=i)).weekday() < 5)
    people = list(members(sprint.setup))
    total = sum(options(u)["capacity_minutes"] for u in people) * workdays * 0.7
    return {"minutes": round(total), "people": len(people), "workdays": workdays}


def tick(now=None):
    """Worker: roll sprints over, ask for and summarise standups."""
    now = now or timezone.now()
    for setup in AgileSetup.objects.filter(enabled=True).select_related("workspace", "project"):
        try:
            today = local_now(setup, now).date()
            active = setup.sprints.filter(status=Sprint.Status.ACTIVE).first()
            if active and active.end < today:
                close(active, now)
            ensure(setup, today)
            if setup.standup:
                standup_round(setup, now)
        except Exception:
            log.exception("Agile tick failed for setup %s", setup.pk)


# --- standup --------------------------------------------------------------------------


def draft(setup, user, today):
    """Yesterday / today / blockers, from the person's activity in the scope."""
    tz = tz_of(setup)
    last_workday = today - timedelta(days=3 if today.weekday() == 0 else 1)
    since = datetime.combine(last_workday, dtime.min, tzinfo=tz)
    until = datetime.combine(today, dtime.min, tzinfo=tz)
    mine = scope_tasks(setup).filter(Q(assignee=user))
    done = mine.filter(completed_at__gte=since, completed_at__lt=until, status__is_done=True).values_list("title", flat=True)[:10]
    plan = mine.filter(status__is_done=False).filter(Q(planned_date__lte=today)).values_list("title", flat=True)[:10]
    blocked = [t.title for t in mine.filter(status__is_done=False, blocked_by_links__depends_on__status__is_done=False).distinct()[:5]]
    return {"yesterday": "\n".join(f"- {t}" for t in done), "today": "\n".join(f"- {t}" for t in plan),
            "blockers": "\n".join(f"- waiting: {t}" for t in blocked)}


def standup_round(setup, now):
    from concierge import notify, telegram
    from concierge.models import ConciergeProfile, Notification

    local = local_now(setup, now)
    today = local.date()
    if today.weekday() not in (setup.standup_days or []):
        return
    at = datetime.combine(today, setup.standup_time, tzinfo=local.tzinfo)
    if local < at:
        return
    rnd, _ = StandupRound.objects.get_or_create(setup=setup, date=today)
    sprint = current(setup, today)
    if rnd.asked_at is None:
        if local - at > timedelta(hours=3):  # the worker was down: skip today's
            rnd.asked_at = rnd.summarised_at = now
            rnd.save()
            return
        rnd.asked_at = now
        rnd.save(update_fields=["asked_at"])
        url = f"/w/{setup.workspace_id}/sprints"
        for user in members(setup):
            d = draft(setup, user, today)
            body = "\n".join(filter(None, [f"Yesterday:\n{d['yesterday']}" if d["yesterday"] else "", f"Today:\n{d['today']}" if d["today"] else "",
                                           f"Blockers:\n{d['blockers']}" if d["blockers"] else ""])) or "Nothing from your tasks yet."
            notify.create(user, Notification.Kind.BRIEF, f"🧍 Standup · {setup.label}", body, workspace=setup.workspace,
                          data={"url": url}, telegram=Notification.Telegram.NONE)
            profile = ConciergeProfile.objects.filter(user=user, telegram_chat_id__isnull=False).first()
            if profile and user.has_pro:
                try:
                    telegram.send_message(profile.telegram_chat_id, f"🧍 Standup for {setup.label}. My draft from your tasks:\n\n{body}\n\n"
                                                                     "Reply with your update (or “ok” to keep the draft) and I'll post it.")
                except Exception as exc:
                    log.info("Standup DM failed for %s: %s", user.pk, exc)
        return
    if rnd.summarised_at is None and (now - rnd.asked_at >= SUMMARY_AFTER or answered_all(setup, sprint, today)):
        rnd.summarised_at = now
        rnd.save(update_fields=["summarised_at"])
        post_to_team(setup, summary(setup, sprint, today))


def answered_all(setup, sprint, today):
    ids = set(members(setup).values_list("id", flat=True))
    return bool(ids) and ids <= set(StandupEntry.objects.filter(sprint=sprint, date=today).values_list("user_id", flat=True))


def summary(setup, sprint, today) -> str:
    entries = {e.user_id: e for e in StandupEntry.objects.filter(sprint=sprint, date=today)}
    lines = [f"🧍 Standup · {setup.label} · {today:%a %d %b}" + (f" · {sprint.name}" if sprint else "")]
    for user in members(setup):
        name = user.full_name or user.email.split("@")[0]
        e = entries.get(user.id)
        if e is None:
            d = draft(setup, user, today)
            if not any(d.values()):
                continue
            e = StandupEntry(yesterday=d["yesterday"], today=d["today"], blockers=d["blockers"])
            name += " (from their tasks)"
        lines.append(f"\n{name}")
        for label, value in (("✓", e.yesterday), ("→", e.today), ("⚠", e.blockers)):
            for line in value.splitlines():
                line = line.strip().removeprefix("- ").removeprefix("• ").strip()
                if line:
                    lines.append(f"{label} {line}")
    return "\n".join(lines)


def post_to_team(setup, text):
    """The workspace's Telegram group / Slack channel (Enterprise team bots), when one is linked."""
    from concierge import telegram
    from integrations.connectors.teamchat import SlackBot
    from integrations.models import Integration

    for integ in Integration.objects.filter(workspace=setup.workspace, enabled=True, kind__in=["telegram_group", "slack_bot"]):
        if integ.project_id and integ.project_id != setup.project_id:
            continue
        try:
            if integ.kind == "telegram_group" and integ.config.get("chat_id"):
                telegram.send_message(integ.config["chat_id"], text)
            elif integ.kind == "slack_bot" and integ.config.get("channel"):
                SlackBot.api(integ, "chat.postMessage", channel=integ.config["channel"], text=text)
        except Exception as exc:
            log.info("Posting to %s failed: %s", integ.pk, exc)


def save_standup(setup, user, today, yesterday="", today_text="", blockers=""):
    sprint = current(setup, today)
    entry, _ = StandupEntry.objects.update_or_create(sprint=sprint, user=user, date=today, defaults={
        "yesterday": yesterday.strip()[:4000], "today": today_text.strip()[:4000], "blockers": blockers.strip()[:4000]})
    return entry
