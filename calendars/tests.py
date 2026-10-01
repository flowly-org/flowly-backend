import base64
import json
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from concierge.models import ConciergeProfile
from concierge.tests import FakeResponse, tool_call
from tasks.models import Task

from . import prep, sync
from .models import Calendar, CalendarAccount, CalendarEvent


class ClassifyTests(TestCase):
    HOME = "Bologna, Italy"

    def test_reasons(self):
        c = lambda title, loc="", days=1: prep.classify(title, loc, "", self.HOME, days)
        self.assertEqual(c("Team standup", "Sala verde"), "")
        self.assertEqual(c("Muse concert", "Stadio Dall'Ara, Bologna"), "tickets")
        self.assertEqual(c("Volo per Londra"), "travel")
        self.assertEqual(c("Matrimonio di Luca"), "event")
        self.assertEqual(c("Client workshop", "Hauptstraße 1, 10115 Berlin, Germany"), "away")
        self.assertEqual(c("Client workshop", "Via Rizzoli 1, 40125 Bologna BO, Italy"), "")
        self.assertEqual(c("Client workshop", "https://meet.google.com/abc"), "")
        self.assertEqual(c("Client workshop", "Office"), "")  # no address: somewhere nearby
        self.assertEqual(c("Holidays", "Sardinia", days=7), "travel")
        self.assertEqual(prep.classify("Workshop", "Rue X, Paris, France", "", "", 1), "")  # home unknown
        self.assertEqual(prep.home_places("Bologna, Italy; Imola"), ["bologna", "imola"])
        street = "Via Donato Creti 32, 40128 Bologna BO, Italy"
        self.assertEqual(prep.home_places(street), ["via donato creti", "bologna"])
        self.assertEqual(prep.classify("Dentist", "Via Rizzoli 3, 40125 Bologna BO, Italy", "", street, 1), "")
        self.assertEqual(prep.classify("Workshop", "Corso Como 1, 20154 Milano MI, Italy", "", street, 1), "away")


def id_token(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"x.{payload}.y"


class FakeGoogle:
    def __init__(self, email="alice@gmail.com", events=None):
        self.email = email
        self.events = events if events is not None else {}
        self.calls = []
        self.refresh_fails = False

    def __call__(self, method, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append((method, url))
        if url.startswith("https://oauth2.googleapis.com/token"):
            if data.get("grant_type") == "refresh_token":
                if self.refresh_fails:
                    return FakeResponse({"error": "invalid_grant"}, status=400)
                return FakeResponse({"access_token": "at-2", "expires_in": 3600})
            return FakeResponse({"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600, "id_token": id_token(self.email)})
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            return FakeResponse({})
        if "/users/me/calendarList" in url:
            return FakeResponse({"items": [
                {"id": "primary-id", "summary": self.email, "primary": True, "accessRole": "owner", "backgroundColor": "#4285f4"},
                {"id": "holidays@group", "summary": "Holidays in Italy", "accessRole": "reader"},
            ]})
        if "/calendars/" in url:
            cal = url.split("/calendars/")[1].split("/")[0]
            return FakeResponse({"items": self.events.get(cal, [])})
        raise AssertionError(f"unexpected {method} {url}")


def timed(eid, title, day, hour=9, hours=1, location="", **extra):
    start = timezone.now().replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=day)
    return {"id": eid, "summary": title, "location": location, "htmlLink": f"https://calendar.google.com/e/{eid}",
            "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + timedelta(hours=hours)).isoformat()}, **extra}


def all_day(eid, title, day, days=1, location=""):
    first = timezone.localdate() + timedelta(days=day)
    return {"id": eid, "summary": title, "location": location,
            "start": {"date": first.isoformat()}, "end": {"date": (first + timedelta(days=days)).isoformat()}}


@override_settings(GOOGLE_CLIENT_ID="cid", GOOGLE_CLIENT_SECRET="secret", PUBLIC_URL="https://app.example", API_PUBLIC_URL="https://api.example")
class CalendarApiTests(APITestCase):
    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "alice@example.com", "password": "s3cure-Passw0rd", "full_name": "Alice"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res['access']}")
        self.user = User.objects.get(email="alice@example.com")
        self.client.patch("/api/auth/me/", {"home_location": "Bologna, Italy"}, format="json")
        ConciergeProfile.objects.create(user=self.user, timezone="UTC")
        self.ws = self.user.memberships.first().workspace
        self.google = FakeGoogle(events={"primary-id": [
            timed("standup", "Team standup", 0, location="Sala verde"),
            timed("concert", "Muse concert", 5, hour=20, location="Unipol Arena, Casalecchio di Reno"),
            timed("berlin", "Client workshop", 9, location="Hauptstraße 1, 10115 Berlin, Germany"),
            timed("declined", "Boring sync", 1, attendees=[{"self": True, "responseStatus": "declined"}]),
            {"id": "gone", "status": "cancelled"},
            all_day("trip", "Sardinia", 14, days=5, location="Cagliari"),
        ]})

    def connect(self, google=None):
        google = google or self.google
        with mock.patch("integrations.http.requests.request", google):
            url = self.client.post("/api/calendar/google/connect/").data["url"]
            q = parse_qs(urlparse(url).query)
            self.assertEqual(q["redirect_uri"], ["https://api.example/api/calendar/google/callback/"])
            self.assertIn("calendar.readonly", q["scope"][0])
            self.assertEqual(q["access_type"], ["offline"])
            res = self.client.get("/api/calendar/google/callback/", {"code": "abc", "state": q["state"][0]})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res["Location"], "https://app.example/settings?calendar=connected#calendars")
        return res

    def events(self, **params):
        return {e["title"]: e for e in self.client.get("/api/calendar/events/", params).data}

    def test_connect_sync_and_classify(self):
        self.connect()
        overview = self.client.get("/api/calendar/").data
        self.assertTrue(overview["available"])
        self.assertEqual(overview["home_location"], "Bologna, Italy")
        [account] = overview["accounts"]
        self.assertEqual(account["email"], "alice@gmail.com")
        self.assertEqual({c["name"]: c["selected"] for c in account["calendars"]}, {"alice@gmail.com": True, "Holidays in Italy": False})
        stored = CalendarAccount.objects.get()
        self.assertTrue(stored.refresh_token.startswith("enc:"))
        self.assertNotIn("holidays", "".join(u for _, u in self.google.calls))  # unselected calendars aren't fetched

        today = timezone.localdate()
        span = self.events(**{"from": today.isoformat(), "to": (today + timedelta(days=30)).isoformat()})
        self.assertEqual(set(span), {"Team standup", "Muse concert", "Client workshop", "Sardinia"})
        self.assertEqual({k: v["reason"] for k, v in span.items()},
                         {"Team standup": "", "Muse concert": "tickets", "Client workshop": "away", "Sardinia": "travel"})
        self.assertEqual(span["Sardinia"]["end_date"], (today + timedelta(days=18)))
        self.assertEqual(list(self.events()), ["Team standup"])  # default: today only

        # Home elsewhere: the Berlin workshop isn't a trip any more.
        self.client.patch("/api/auth/me/", {"home_location": "Berlin, Germany"}, format="json")
        self.assertEqual(CalendarEvent.objects.get(external_id="berlin").auto_reason, "")

    def test_several_google_accounts(self):
        self.connect()
        self.connect(FakeGoogle(email="alice@work.example", events={"primary-id": [timed("w1", "Board meeting", 0, location="Room 4")]}))
        emails = [a["email"] for a in self.client.get("/api/calendar/").data["accounts"]]
        self.assertEqual(emails, ["alice@gmail.com", "alice@work.example"])
        self.assertEqual(set(self.events()), {"Team standup", "Board meeting"})
        # Reconnecting the same Google account updates it instead of adding a third.
        self.connect()
        self.assertEqual(CalendarAccount.objects.count(), 2)

    def test_decisions_tasks_and_resync(self):
        self.connect()
        concert = CalendarEvent.objects.get(external_id="concert")
        workshop = CalendarEvent.objects.get(external_id="berlin")
        # A matching task shows up as a suggestion; create one from the event.
        self.client.post(f"/api/workspaces/{self.ws.id}/tasks/", {"title": "Ask Marco about Muse"}, format="json")
        prep_list = {e["title"]: e for e in self.client.get("/api/calendar/events/", {"prep": 1}).data}
        self.assertEqual(set(prep_list), {"Muse concert", "Client workshop", "Sardinia"})
        self.assertEqual([t["title"] for t in prep_list["Muse concert"]["suggested"]], ["Ask Marco about Muse"])
        res = self.client.post(f"/api/calendar/events/{concert.id}/tasks/", {"title": "Buy Muse tickets"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        [task] = res.data["tasks"]
        self.assertEqual(task["title"], "Buy Muse tickets")
        stored = Task.objects.get(pk=task["id"])
        self.assertEqual(stored.due_date, concert.start_date)
        self.assertEqual(stored.assignee, self.user)
        self.assertIn("Unipol Arena", stored.description)

        # Link an existing task; mark the trip as background; confirm the workshop.
        other = Task.objects.create(workspace=self.ws, title="Book hotel", status=stored.status, created_by=self.user)
        self.client.post(f"/api/calendar/events/{workshop.id}/tasks/", {"task": other.id}, format="json")
        sardinia = CalendarEvent.objects.get(external_id="trip")
        res = self.client.patch(f"/api/calendar/events/{sardinia.id}/", {"prep": "background"}, format="json")
        self.assertFalse(res.data["needs_prep"])
        res = self.client.patch(f"/api/calendar/events/{workshop.id}/", {"confirmed": True}, format="json")
        self.assertTrue(res.data["confirmed"])

        # Someone else's event / task are out of reach.
        res = self.client.post(f"/api/calendar/events/{workshop.id}/tasks/", {"task": 999999}, format="json")
        self.assertEqual(res.status_code, 400)

        # Re-sync: decisions and links survive, deleted events go.
        self.google.events["primary-id"] = [e for e in self.google.events["primary-id"] if e["id"] != "standup"]
        with mock.patch("integrations.http.requests.request", self.google):
            self.client.post(f"/api/calendar/accounts/{CalendarAccount.objects.get().id}/")
        self.assertFalse(CalendarEvent.objects.filter(external_id="standup").exists())
        self.assertEqual(CalendarEvent.objects.get(external_id="trip").prep, "background")
        self.assertEqual(list(CalendarEvent.objects.get(external_id="berlin").tasks.all()), [other])

        # Unselecting a calendar drops its events.
        cal = Calendar.objects.get(primary=True)
        self.client.patch(f"/api/calendar/calendars/{cal.id}/", {"selected": False}, format="json")
        self.assertFalse(CalendarEvent.objects.exists())

    def test_revoked_access_and_disconnect(self):
        self.connect()
        account = CalendarAccount.objects.get()
        account.token_expires_at = timezone.now() - timedelta(minutes=1)
        account.save()
        self.google.refresh_fails = True
        with mock.patch("integrations.http.requests.request", self.google):
            sync.sync_account(account)
        account.refresh_from_db()
        self.assertEqual(account.status, "error")
        self.assertIn("reconnect", account.last_error)
        with mock.patch("integrations.http.requests.request", self.google):
            self.assertEqual(self.client.delete(f"/api/calendar/accounts/{account.id}/").status_code, 204)
        self.assertFalse(CalendarEvent.objects.exists())

    def test_bad_state_is_rejected(self):
        res = self.client.get("/api/calendar/google/callback/", {"code": "abc", "state": "forged"})
        self.assertEqual(res["Location"], "https://app.example/settings?calendar=error#calendars")
        self.assertFalse(CalendarAccount.objects.exists())

    @override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model")
    def test_concierge_brief_and_tools(self):
        from concierge import brain

        self.connect()
        self.user.plan = "pro"
        self.user.save()
        concert = CalendarEvent.objects.get(external_id="concert")
        prompts = []

        def fake(method, url, json=None, data=None, headers=None, timeout=None):
            prompts.append(json)
            return FakeResponse({"choices": [{"message": replies.pop(0)}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

        replies = [{"role": "assistant", "content": "Morning! Muse in 5 days: tickets?"}]
        with mock.patch("integrations.http.requests.request", fake):
            text = brain.brief(self.user)
        self.assertIn("Muse", text)
        prompt = prompts[0]["messages"][-1]["content"]
        self.assertIn("Calendar today (background, not tasks):", prompt)
        self.assertIn("Team standup", prompt)
        self.assertIn(f"E{concert.id}", prompt)
        self.assertIn("may need: tickets or a booking", prompt)
        self.assertIn("away from home", prompt)
        self.assertNotIn("Boring sync", prompt)

        replies = [
            tool_call("create_event_task", {"event_id": concert.id, "title": "Buy Muse tickets", "day": "today"}),
            tool_call("mark_event", {"event_id": CalendarEvent.objects.get(external_id="berlin").id, "decision": "confirmed"}, "c2"),
            {"role": "assistant", "content": "Done: task added and Berlin confirmed."},
        ]
        with mock.patch("integrations.http.requests.request", fake):
            answer = brain.reply(self.user, "yes, add a task for the tickets; Berlin is still on")
        self.assertIn("Done", answer)
        self.assertEqual([t.title for t in concert.tasks.all()], ["Buy Muse tickets"])
        self.assertIsNotNone(CalendarEvent.objects.get(external_id="berlin").confirmed_at)

        # Without an LLM the plain brief still lists what needs preparation.
        plain = brain.plain_brief(self.user, brain.local_now(self.user))
        self.assertIn("Client workshop", plain)

    def test_mcp_tool_is_personal_only(self):
        from mcpserver.tools import run_tool

        self.connect()
        text, err = run_tool(self.user, "list_calendar_events", {"days": 30, "prep_only": True})
        self.assertFalse(err)
        self.assertIn("Muse concert", text)
        text, err = run_tool(self.user, "list_calendar_events", {}, scope=self.ws.id)
        self.assertTrue(err)
