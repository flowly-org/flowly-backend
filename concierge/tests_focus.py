from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from concierge import brain, focus
from concierge.models import ConciergeProfile, Notification
from concierge.tests import FakeWorld
from timetracking.models import TimeEntry


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model")
class AdhdModeTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "a@example.com", "password": "s3cure-Passw0rd", "full_name": "Ada"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="a@example.com")
        self.ws = self.client.get("/api/workspaces/").data[0]["id"]

    def test_opt_in_and_options(self):
        me = self.client.get("/api/auth/me/").data
        self.assertFalse(me["adhd_mode"])
        self.assertEqual(me["focus"]["today_limit"], 3)
        res = self.client.patch("/api/auth/me/", {"adhd_mode": True, "focus": {"check_in_minutes": 20}}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["adhd_mode"])
        self.assertEqual(res.data["focus"]["check_in_minutes"], 20)
        self.assertEqual(res.data["focus"]["hyperfocus_minutes"], 90)
        res = self.client.patch("/api/auth/me/", {"focus": {"celebrate": False}}, format="json")
        self.assertEqual(res.data["focus"]["check_in_minutes"], 20)  # merged, not replaced
        self.assertFalse(res.data["focus"]["celebrate"])
        self.assertEqual(self.client.patch("/api/auth/me/", {"focus": {"today_limit": 99}}, format="json").status_code, 400)

    def test_prompt_and_brief_change_in_adhd_mode(self):
        now = brain.local_now(self.user)
        self.assertNotIn("ADHD MODE", brain.system_prompt(self.user, now))
        self.user.adhd_mode = True
        self.user.save()
        self.assertIn("ADHD MODE", brain.system_prompt(self.user, now))
        self.user.plan = "pro"
        self.user.save()
        ConciergeProfile.objects.get_or_create(user=self.user)
        world = FakeWorld(llm_replies=[{"role": "assistant", "content": "Today's 3: …"}])
        with mock.patch("integrations.http.requests.request", world):
            brain.brief(self.user)
        self.assertIn("Today's 3", world.llm_calls[0]["json"]["messages"][1]["content"])

    def test_check_in_break_and_heads_up_nudges(self):
        from calendars.models import Calendar, CalendarAccount, CalendarEvent

        self.user.adhd_mode = True
        self.user.save()
        task = self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": "Write the report"}, format="json").data
        now = timezone.now()
        entry = TimeEntry.objects.create(workspace_id=self.ws, user=self.user, task_id=task["id"], started_at=now - timedelta(minutes=30))
        self.assertEqual(focus.nudges(self.user, now), 1)
        self.assertEqual(focus.nudges(self.user, now), 0)  # once
        n = Notification.objects.get(kind="focus")
        self.assertIn("30 min on “Write the report”", n.title)
        # Hyperfocus: repeated every 90 minutes.
        self.assertEqual(focus.nudges(self.user, now + timedelta(minutes=70)), 1)
        self.assertEqual(focus.nudges(self.user, now + timedelta(minutes=80)), 0)
        self.assertEqual(focus.nudges(self.user, now + timedelta(minutes=160)), 1)
        self.assertIn("1h40", Notification.objects.filter(kind="focus").order_by("-id")[1].title)
        entry.ended_at = now
        entry.save()

        account = CalendarAccount.objects.create(user=self.user, email="a@gmail.com")
        cal = Calendar.objects.create(account=account, external_id="primary", name="Main")
        start = now + timedelta(minutes=8)
        CalendarEvent.objects.create(calendar=cal, user=self.user, external_id="e1", title="Team call", start=start,
                                     end=start + timedelta(hours=1), start_date=start.date(), end_date=start.date())
        self.assertEqual(focus.nudges(self.user, now), 1)
        self.assertIn("In 8 min: Team call", Notification.objects.filter(kind="focus").latest("id").title)
        # Off when the option is 0.
        self.user.focus = {"heads_up_minutes": 0}
        self.user.save()
        Notification.objects.all().delete()
        from concierge.models import FocusNudge

        FocusNudge.objects.all().delete()
        self.assertEqual(focus.nudges(self.user, now), 0)

    def test_add_steps(self):
        from concierge.tools import Toolbox

        task = self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": "Do taxes", "description": "2025"}, format="json").data
        box = Toolbox(self.user, None, timezone.localdate())
        out = box.run("add_steps", f'{{"task_id": {task["id"]}, "steps": ["Find last year\'s PDF", "List receipts"]}}')
        self.assertIn("Added 2 steps", out)
        data = self.client.get(f"/api/workspaces/{self.ws}/tasks/{task['id']}/").data
        self.assertEqual(data["description"], "2025")
        self.assertEqual([(i["text"], i["done"]) for i in data["checklist"]], [("Find last year's PDF", False), ("List receipts", False)])
