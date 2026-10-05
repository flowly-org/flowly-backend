from datetime import date, timedelta

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from tasks.models import Project
from workspaces.models import Workspace

from . import review as reviews
from .models import WeeklyReview
from .stats import compute, monday


def _tz_today(user):
    from calendars.sync import user_tz

    tz = user_tz(user)
    return tz, timezone.now().astimezone(tz).date()


class ReviewView(APIView):
    """GET ?week=YYYY-MM-DD (any day of it; default this week) → numbers (+ Flo's text if written).
    POST {week} → (re)write it now (Flo's text for Pro, a plain summary otherwise)."""

    def get(self, request):
        tz, today = _tz_today(request.user)
        week = self._week(request.query_params.get("week"), today)
        saved = WeeklyReview.objects.filter(user=request.user, week_start=week).first()
        stats = reviews.week_numbers(request.user, week, tz, today)
        return Response({"week_start": week, "stats": stats, "text": saved.text if saved else "",
                         "generated_at": saved.updated_at if saved else None})

    def post(self, request):
        _, today = _tz_today(request.user)
        week = self._week(request.data.get("week"), today)
        r = reviews.generate(request.user, week)
        return Response({"week_start": week, "stats": r.stats, "text": r.text, "generated_at": r.updated_at})

    @staticmethod
    def _week(value, today):
        try:
            day = date.fromisoformat(str(value)[:10]) if value else today
        except ValueError:
            day = today
        return monday(day)


class InsightsView(APIView):
    """GET ?scope=me|workspace|project&id=&weeks=12 → numbers for that scope."""

    def get(self, request):
        tz, today = _tz_today(request.user)
        weeks = max(1, min(int(request.query_params.get("weeks") or 12), 52))
        lo = monday(today) - timedelta(weeks=weeks - 1)
        scope = request.query_params.get("scope", "me")
        kwargs = {}
        if scope == "workspace":
            kwargs["workspace"] = get_object_or_404(Workspace, pk=request.query_params.get("id"), memberships__user=request.user)
        elif scope == "project":
            kwargs["project"] = get_object_or_404(Project, pk=request.query_params.get("id"), workspace__memberships__user=request.user)
        else:
            kwargs["user"] = request.user
        return Response(compute(request.user, lo, today, tz, today, **kwargs))
