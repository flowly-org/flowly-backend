"""The weekly review: a person's week in numbers, what's slipping, next week's load, and Flo's written review."""

import logging
from datetime import datetime, time, timedelta

from django.db.models import Q, Sum
from django.utils import timezone

from tasks.access import member_visible_q
from tasks.models import Task

from .models import WeeklyReview
from .stats import compute, monday

log = logging.getLogger("flowly.insights")
DAYS = {"fri": 4, "sun": 6}


def week_numbers(user, week_start, tz, today):
    week_end = week_start + timedelta(days=6)
    data = compute(user, week_start, week_end, tz, today, user=user)
    mine = (Task.objects.filter(workspace__memberships__user=user, assignee=user).filter(member_visible_q(user))
            .select_related("workspace", "project", "status").distinct())
    planned = mine.filter(planned_date__range=(week_start, week_end))
    slipping = (mine.filter(status__is_done=False).filter(Q(planned_date__lte=week_end) | Q(due_date__lte=week_end))
                .order_by("planned_date", "due_date")[:15])
    nxt_lo, nxt_hi = week_end + timedelta(days=1), week_end + timedelta(days=7)
    upcoming = mine.filter(status__is_done=False).filter(Q(planned_date__range=(nxt_lo, nxt_hi)) | Q(due_date__range=(nxt_lo, nxt_hi)))
    from accounts.focus import options

    data.update({
        "week_start": week_start.isoformat(),
        "planned": {"total": planned.count(), "done": planned.filter(status__is_done=True).count()},
        "slipping": [{"id": t.id, "workspace": t.workspace_id, "title": t.title, "planned_date": t.planned_date.isoformat() if t.planned_date else None,
                      "due_date": t.due_date.isoformat() if t.due_date else None, "where": " · ".join(filter(None, [t.workspace.name, t.project.name if t.project_id else ""]))}
                     for t in slipping],
        "next_week": {"tasks": upcoming.count(), "estimated_minutes": upcoming.aggregate(m=Sum("estimate_minutes"))["m"] or 0,
                      "capacity_minutes": options(user)["capacity_minutes"] * 5},
    })
    return data


def plain_text(user, s) -> str:
    t = s["totals"]
    lines = [f"Your week: {t['completed']} tasks done, {t['minutes'] // 60}h{t['minutes'] % 60:02d} tracked."]
    if s["planned"]["total"]:
        lines.append(f"Planned this week: {s['planned']['done']}/{s['planned']['total']} done.")
    if s["projects"]:
        lines.append("Most of it: " + ", ".join(p["name"] for p in s["projects"][:3]) + ".")
    if s["slipping"]:
        lines.append(f"Still open from this week or earlier: {len(s['slipping'])} (e.g. {s['slipping'][0]['title']}).")
    if t["estimate_ratio"]:
        lines.append(f"Tasks took {t['estimate_ratio']:.1f}× their estimates.")
    nw = s["next_week"]
    lines.append(f"Next week: {nw['tasks']} tasks, about {nw['estimated_minutes'] // 60}h estimated.")
    return "\n".join(lines)


def ai_text(user, s) -> str:
    from concierge import brain

    now = brain.local_now(user)
    slip = "\n".join(f"- #{x['id']} {x['title']} (planned {x['planned_date'] or '—'}, due {x['due_date'] or '—'}; {x['where']})" for x in s["slipping"]) or "none"
    t = s["totals"]
    prompt = (
        "Write my weekly review for the chat. Structure: a one-line verdict; what went well (with the numbers); "
        "patterns worth noticing (what keeps slipping, estimates vs real time, where the time went); then NEXT WEEK: "
        "three priorities picked from the open list, and what to move to later or drop. Be honest and kind, no guilt. "
        "Max ~15 short lines, '•' bullets, no headings.\n\n"
        f"Done: {t['completed']} tasks, {t['minutes']} minutes tracked. Planned this week: {s['planned']['done']}/{s['planned']['total']} done.\n"
        f"Time and completions by project: {', '.join(f'{p['name']} {p['completed']} done/{p['minutes']} min' for p in s['projects'][:6]) or 'none'}.\n"
        f"Overdue now: {t['overdue']}. Estimate ratio (real/estimated): {t['estimate_ratio'] or 'n/a'} over {t['estimate_samples']} tasks. "
        f"On-time rate: {t['on_time_rate'] if t['on_time_rate'] is not None else 'n/a'}%.\n"
        f"Next week: {s['next_week']['tasks']} tasks planned or due, {s['next_week']['estimated_minutes']} min estimated, "
        f"capacity about {s['next_week']['capacity_minutes']} min.\nStill open from this week or earlier:\n{slip}"
    )
    messages = [{"role": "system", "content": brain.system_prompt(user, now)}, {"role": "user", "content": prompt}]
    return brain.run(user, messages, tools=False, max_tokens=2500)


def generate(user, week_start, *, write=True) -> WeeklyReview:
    from calendars.sync import user_tz
    from concierge import brain

    tz = user_tz(user)
    today = timezone.now().astimezone(tz).date()
    s = week_numbers(user, week_start, tz, today)
    text = ""
    if write and not brain.availability(user):
        try:
            text = ai_text(user, s)
        except Exception as exc:  # the numbers are still worth sending
            log.warning("Weekly review text failed for %s: %s", user.pk, exc)
    review, _ = WeeklyReview.objects.update_or_create(user=user, week_start=week_start,
                                                      defaults={"stats": s, "text": text or plain_text(user, s)})
    return review


def due_reviews(now=None):
    """(profile, week_start) whose review should go out now."""
    from concierge.models import ConciergeProfile

    from calendars.sync import user_tz

    now = now or timezone.now()
    for profile in ConciergeProfile.objects.filter(enabled=True).exclude(review_day="off").select_related("user"):
        local = now.astimezone(user_tz(profile.user))
        if local.weekday() != DAYS.get(profile.review_day, 4) or local.time() < profile.review_time:
            continue
        week = monday(local.date())
        if WeeklyReview.objects.filter(user=profile.user, week_start=week).exists():
            continue
        if local - datetime.combine(local.date(), profile.review_time, tzinfo=local.tzinfo) > timedelta(hours=6):
            continue  # worker was down: skip rather than send at night
        yield profile, week


def send_due():
    from concierge import notify, telegram
    from concierge.models import Notification

    for profile, week in due_reviews():
        user = profile.user
        review = generate(user, week)
        notify.create(user, Notification.Kind.BRIEF, f"Your weekly review ({week:%d %b})", review.text,
                      data={"url": "/review"}, telegram=Notification.Telegram.NONE)
        if profile.telegram_chat_id and user.has_pro:
            try:
                telegram.send_message(profile.telegram_chat_id, f"📊 {review.text}")
            except Exception as exc:
                log.warning("Weekly review to Telegram failed for %s: %s", user.pk, exc)
