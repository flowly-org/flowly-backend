"""Your Flowly tasks as an iCal feed (a private URL) for any calendar app: planned tasks on their day, due dates too."""

import secrets
from datetime import datetime, timedelta, timezone as dt_timezone

import icalendar
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from instance import conf

PAST, AHEAD = timedelta(days=30), timedelta(days=365)


def feed_url(user) -> str | None:
    return f"{conf.get('API_PUBLIC_URL')}/api/ical/{user.ical_token}.ics" if user.ical_token else None


class FeedSettingsView(APIView):
    """GET → {"url"}; POST → (re)create the secret link; DELETE → switch the feed off."""

    def get(self, request):
        return Response({"url": feed_url(request.user)})

    def post(self, request):
        request.user.ical_token = secrets.token_urlsafe(24)
        request.user.save(update_fields=["ical_token"])
        return Response({"url": feed_url(request.user)})

    def delete(self, request):
        request.user.ical_token = ""
        request.user.save(update_fields=["ical_token"])
        return Response({"url": None})


def build(user) -> bytes:
    from django.db.models import Q

    from tasks.access import member_visible_q
    from tasks.models import Task

    today = timezone.localdate()
    tasks = (Task.objects.filter(workspace__memberships__user=user).filter(member_visible_q(user))
             .filter(Q(assignee=user) | Q(assignee__isnull=True, created_by=user))
             .filter(Q(planned_date__range=(today - PAST, today + AHEAD)) | Q(due_date__range=(today - PAST, today + AHEAD)))
             .select_related("status", "workspace", "project").distinct()[:2000])
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Flowly//Tasks//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Flowly")
    cal.add("refresh-interval;value=duration", "PT1H")
    base = conf.get("PUBLIC_URL") or ""
    stamp = datetime.now(dt_timezone.utc)
    for task in tasks:
        where = " · ".join(filter(None, [task.workspace.name, task.project.name if task.project_id else ""]))
        done = task.status.is_done
        for kind, day in (("plan", task.planned_date), ("due", task.due_date if task.due_date != task.planned_date else None)):
            if not day:
                continue
            ev = icalendar.Event()
            ev.add("uid", f"{task.ref}-{kind}@flowly")
            ev.add("dtstamp", stamp)
            ev.add("dtstart", day)
            ev.add("dtend", day + timedelta(days=1))
            mark = "✓ " if done else ("⚑ " if kind == "due" else "")
            ev.add("summary", f"{mark}{'Due: ' if kind == 'due' else ''}{task.title}")
            ev.add("description", "\n".join(filter(None, [where, task.description[:1000], f"{base}/t/{task.ref}" if base else ""])))
            if base:
                ev.add("url", f"{base}/t/{task.ref}")
            ev.add("transp", "TRANSPARENT")  # tasks don't make you look busy
            cal.add_component(ev)
    return cal.to_ical()


class FeedView(APIView):
    """GET /api/ical/<token>.ics — public (the token is the secret)."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request, token):
        from accounts.models import User

        user = User.objects.filter(ical_token=token, is_active=True).first() if len(token) >= 20 else None
        if user is None:
            return HttpResponse("Not found", status=404, content_type="text/plain")
        res = HttpResponse(build(user), content_type="text/calendar; charset=utf-8")
        res["Content-Disposition"] = 'inline; filename="flowly.ics"'
        res["Cache-Control"] = "private, max-age=900"
        return res
