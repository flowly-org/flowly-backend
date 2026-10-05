from datetime import date, timedelta

from django.core import signing
from django.shortcuts import get_object_or_404, redirect
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from instance import conf
from integrations.http import DeliveryError

from . import google, microsoft, prep, providers, services, sync
from .models import Calendar, CalendarAccount, CalendarEvent

STATE_SALT = "flowly.calendar.google"


def account_data(account):
    return {
        "id": account.id, "provider": account.provider, "email": account.email, "status": account.status,
        "label": account.email or account.username or account.server_url,
        "last_error": account.last_error, "last_synced_at": account.last_synced_at,
        "calendars": [
            {"id": c.id, "name": c.name, "color": c.color, "primary": c.primary, "selected": c.selected}
            for c in account.calendars.all()
        ],
    }


def task_data(task):
    return {
        "id": task.id, "ref": task.ref, "title": task.title, "workspace": task.workspace_id,
        "workspace_name": task.workspace.name, "planned_date": task.planned_date, "done": task.status.is_done,
    }


def event_data(event, user, related=False):
    data = {
        "id": event.id, "calendar": event.calendar_id, "color": event.calendar.color, "title": event.title,
        "location": event.location, "html_link": event.html_link, "all_day": event.all_day,
        "start": event.start, "end": event.end, "start_date": event.start_date, "end_date": event.end_date,
        "prep": event.prep, "needs_prep": event.needs_prep, "reason": event.reason,
        "confirmed": event.confirmed_at is not None,
        "tasks": [task_data(t) for t in prep.visible_tasks(user).filter(calendar_events=event)],
    }
    if related:
        data["suggested"] = [task_data(t) for t in prep.related_tasks(event, user)[1]]
    return data


class CalendarOverviewView(APIView):
    """Connected accounts with their calendars, and whether Google sign-in is configured."""

    def get(self, request):
        accounts = request.user.calendar_accounts.prefetch_related("calendars").order_by("created_at")
        return Response({
            "available": google.enabled(),
            "providers": {"google": google.enabled(), "microsoft": microsoft.enabled(), "caldav": True, "ics": True},
            "home_location": request.user.home_location,
            "accounts": [account_data(a) for a in accounts],
        })


class GoogleConnectView(APIView):
    """POST → {"url": Google consent URL}. Each sign-in adds an account; connect as many as you like."""

    def post(self, request):
        if not google.enabled():
            return Response({"detail": "Google Calendar isn't configured on this server."}, status=400)
        state = signing.dumps({"u": request.user.id}, salt=STATE_SALT)
        return Response({"url": google.auth_url(state)})


class GoogleCallbackView(APIView):
    """Google redirects the browser here; we store the tokens and send the user back to Settings."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        back = f"{conf.get('PUBLIC_URL')}/settings"
        if request.query_params.get("error"):
            return redirect(f"{back}?calendar=cancelled#calendars")
        try:
            state = signing.loads(request.query_params.get("state", ""), salt=STATE_SALT, max_age=900)
            tokens = google.exchange_code(request.query_params.get("code", ""))
        except (signing.BadSignature, DeliveryError, KeyError, ValueError):
            return redirect(f"{back}?calendar=error#calendars")
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(pk=state["u"], is_active=True).first()
        if user is None:
            return redirect(f"{back}?calendar=error#calendars")
        account, _ = CalendarAccount.objects.get_or_create(
            user=user, provider=CalendarAccount.Provider.GOOGLE, email=tokens.get("email", "")
        )
        google.store_tokens(account, tokens)
        account.status, account.last_error = CalendarAccount.Status.OK, ""
        account.save()
        sync.sync_account(account)
        return redirect(f"{back}?calendar=connected#calendars")


MS_SALT = "flowly.calendar.microsoft"


class MicrosoftConnectView(APIView):
    """POST → {"url": Microsoft sign-in URL} (Outlook.com and Microsoft 365 accounts)."""

    def post(self, request):
        if not microsoft.enabled():
            return Response({"detail": "Microsoft calendars aren't configured on this server."}, status=400)
        return Response({"url": microsoft.auth_url(signing.dumps({"u": request.user.id}, salt=MS_SALT))})


class MicrosoftCallbackView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        back = f"{conf.get('PUBLIC_URL')}/settings"
        if request.query_params.get("error"):
            return redirect(f"{back}?calendar=cancelled#calendars")
        try:
            state = signing.loads(request.query_params.get("state", ""), salt=MS_SALT, max_age=900)
            tokens = microsoft.exchange_code(request.query_params.get("code", ""))
        except (signing.BadSignature, DeliveryError, KeyError, ValueError):
            return redirect(f"{back}?calendar=error#calendars")
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(pk=state["u"], is_active=True).first()
        if user is None:
            return redirect(f"{back}?calendar=error#calendars")
        account, _ = CalendarAccount.objects.get_or_create(user=user, provider=CalendarAccount.Provider.MICROSOFT, email=tokens.get("email", ""))
        microsoft.store_tokens(account, tokens)
        account.status, account.last_error = CalendarAccount.Status.OK, ""
        account.save()
        sync.sync_account(account)
        return redirect(f"{back}?calendar=connected#calendars")


class CalDavAddView(APIView):
    """POST {"server_url", "username", "password"} → checks the login, finds the calendars, connects them."""

    def post(self, request):
        from integrations.crypto import encrypt

        from .caldav import Client

        url = str(request.data.get("server_url") or "").strip()
        username = str(request.data.get("username") or "").strip()
        password = str(request.data.get("password") or "")
        if not url.startswith(("https://", "http://")) or not username or not password:
            return Response({"detail": "Enter the server address, your username and an (app) password."}, status=400)
        try:
            found = Client(url, username, password).discover()
        except DeliveryError as exc:
            return Response({"detail": str(exc)}, status=400)
        account, _ = CalendarAccount.objects.get_or_create(user=request.user, provider=CalendarAccount.Provider.CALDAV,
                                                           server_url=url[:500], username=username[:200], email="")
        account.refresh_token = encrypt(password)
        account.status, account.last_error = CalendarAccount.Status.OK, ""
        account.save()
        sync.sync_account(account)
        account.refresh_from_db()
        return Response({**account_data(account), "found": len(found)}, status=201)


class IcsAddView(APIView):
    """POST {"url", "name"} → subscribe to an iCal (.ics / webcal://) link, read-only."""

    def post(self, request):
        url = str(request.data.get("url") or "").strip().replace("webcal://", "https://", 1)
        name = str(request.data.get("name") or "").strip()[:200]
        if not url.startswith(("https://", "http://")):
            return Response({"detail": "Paste the calendar's iCal address (https://… or webcal://…)."}, status=400)
        try:
            providers.fetch_feed(url)
        except DeliveryError as exc:
            return Response({"detail": str(exc)}, status=400)
        account, _ = CalendarAccount.objects.get_or_create(user=request.user, provider=CalendarAccount.Provider.ICS,
                                                           server_url=url[:500], email="", defaults={"username": name or "iCal"})
        if name:
            account.username = name
        account.status, account.last_error = CalendarAccount.Status.OK, ""
        account.save()
        sync.sync_account(account)
        account.refresh_from_db()
        return Response(account_data(account), status=201)


class AccountView(APIView):
    def post(self, request, pk):
        """Sync now."""
        account = get_object_or_404(CalendarAccount, pk=pk, user=request.user)
        account.status = CalendarAccount.Status.OK
        sync.sync_account(account)
        account.refresh_from_db()
        return Response(account_data(account))

    def delete(self, request, pk):
        account = get_object_or_404(CalendarAccount, pk=pk, user=request.user)
        if account.provider == "google":
            google.revoke(account)
        account.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CalendarView(APIView):
    def patch(self, request, pk):
        cal = get_object_or_404(Calendar, pk=pk, account__user=request.user)
        if "selected" in request.data:
            cal.selected = serializers.BooleanField().to_internal_value(request.data["selected"])
            cal.save(update_fields=["selected"])
            if cal.selected:
                sync.sync_account(cal.account)
            else:
                cal.events.all().delete()
        return Response({"id": cal.id, "selected": cal.selected})


def _day(value, default):
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        raise serializers.ValidationError({"detail": f"Bad date: {value}"})


class EventListView(APIView):
    """GET ?from=YYYY-MM-DD&to=YYYY-MM-DD (inclusive; default today) or ?prep=1 for the next 3 weeks' prep list."""

    def get(self, request):
        from concierge.notify import local_today

        today = local_today(request.user)
        if request.query_params.get("prep"):
            events = prep.upcoming(request.user, today)
            return Response([event_data(e, request.user, related=True) for e in events])
        lo = _day(request.query_params.get("from"), today)
        hi = _day(request.query_params.get("to"), lo)
        if (hi - lo).days > 62:
            hi = lo + timedelta(days=62)
        events = CalendarEvent.objects.filter(
            user=request.user, calendar__selected=True, start_date__lte=hi, end_date__gte=lo
        ).select_related("calendar")
        return Response([event_data(e, request.user) for e in events])


class EventView(APIView):
    def get(self, request, pk):
        event = get_object_or_404(CalendarEvent.objects.select_related("calendar"), pk=pk, user=request.user)
        return Response(event_data(event, request.user, related=True))

    def patch(self, request, pk):
        """{"prep": "auto"|"needed"|"background", "confirmed": bool}"""
        event = get_object_or_404(CalendarEvent, pk=pk, user=request.user)
        services.decide(event, prep=request.data.get("prep"), confirmed=request.data.get("confirmed"))
        return Response(event_data(event, request.user, related=True))


class EventTasksView(APIView):
    """POST {"task": id} links an existing task; POST {"workspace", "title", ...task fields} creates one and links it."""

    def post(self, request, pk):
        event = get_object_or_404(CalendarEvent, pk=pk, user=request.user)
        if request.data.get("task"):
            services.link(event, request.user, request.data["task"])
        else:
            services.create_task(event, request.user, request.data)
        return Response(event_data(event, request.user, related=True), status=201)

    def delete(self, request, pk, task_id):
        event = get_object_or_404(CalendarEvent, pk=pk, user=request.user)
        event.tasks.remove(task_id)
        return Response(event_data(event, request.user, related=True))
