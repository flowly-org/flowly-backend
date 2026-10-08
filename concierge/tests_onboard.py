import json
from datetime import timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from concierge.models import ConciergeMessage, ConciergeProfile
from tasks.models import Project, Task

ROME = ZoneInfo("Europe/Rome")


def answer(data):
    content = data if isinstance(data, str) else json.dumps(data)
    return {"message": {"content": content}, "usage": {}}


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model")
class SetupTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "new@example.com", "password": "s3cure-Passw0rd", "full_name": "Nina"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="new@example.com")
        ConciergeProfile.objects.create(user=self.user, timezone="Europe/Rome")
        self.today = timezone.now().astimezone(ROME).date()
        self.personal = self.user.memberships.get().workspace

    def plan(self):
        d = lambda n: str(self.today + timedelta(days=n))  # noqa: E731
        return {
            "message": "8 things in 2 projects: start with the invoice.",
            "projects": ["ACME", "Home", "Unused"],
            "tasks": [
                {"title": "Send the ACME invoice", "project": "ACME", "date": d(0), "minutes": 20, "priority": "high", "notes": "due Friday"},
                {"title": "Call the plumber", "project": "home", "date": d(1), "minutes": 10, "priority": None, "notes": ""},
                {"title": "  Learn   Italian ", "project": None, "date": None, "minutes": 9999, "priority": "low"},
                {"title": "Past thing", "project": "Nowhere", "date": "2001-01-01", "minutes": "x"},
                {"title": ""},
            ],
        }

    def test_propose_then_apply(self):
        with mock.patch("concierge.onboard.chat", return_value=answer("```json\n" + json.dumps(self.plan()) + "\n```")) as llm:
            res = self.client.post("/api/concierge/setup/", {"text": "invoice for ACME by friday, plumber, someday italian"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn(self.today.strftime("%A"), llm.call_args.args[1][0]["content"])
        plan = res.data
        self.assertEqual(plan["workspace"], self.personal.pk)
        self.assertEqual([p["name"] for p in plan["projects"]], ["ACME", "Home"])  # unused ones dropped
        titles = [t["title"] for t in plan["tasks"]]
        self.assertEqual(titles, ["Send the ACME invoice", "Call the plumber", "Learn Italian", "Past thing"])
        italian, past = plan["tasks"][2], plan["tasks"][3]
        self.assertEqual((italian["minutes"], italian["priority"], italian["date"]), (None, None, None))
        self.assertEqual((past["date"], past["project"]), (None, None))  # past date and unknown project dropped
        self.assertEqual(Task.objects.filter(workspace=self.personal).count(), 0)  # nothing created yet

        plan["tasks"] = plan["tasks"][:3]  # the person removed one
        res = self.client.post("/api/concierge/setup/", {"apply": True, "plan": plan}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data["tasks"], res.data["projects"]), (3, 2))
        invoice = Task.objects.get(title="Send the ACME invoice")
        self.assertEqual((invoice.project.name, invoice.planned_date, invoice.estimate_minutes, invoice.assignee), ("ACME", self.today, 20, self.user))
        self.assertEqual(invoice.priority.name, "High")
        self.assertEqual(invoice.description, "due Friday")
        self.assertIsNone(Task.objects.get(title="Learn Italian").planned_date)
        self.assertEqual(set(Project.objects.filter(workspace=self.personal).values_list("name", flat=True)), {"ACME", "Home"})
        # Flo remembers it: the setup is part of the conversation.
        self.assertEqual(list(ConciergeMessage.objects.filter(user=self.user, channel="setup").values_list("role", flat=True)), ["user", "assistant"])
        history = self.client.get("/api/concierge/chat/").data
        self.assertEqual([m["channel"] for m in history["messages"]], ["setup", "setup"])

    def test_free_users_can_set_up_but_not_too_often(self):
        self.assertFalse(self.user.has_pro)
        with mock.patch("concierge.onboard.chat", return_value=answer(self.plan())):
            for _ in range(5):
                self.assertEqual(self.client.post("/api/concierge/setup/", {"text": "stuff"}, format="json").status_code, 200)
            res = self.client.post("/api/concierge/setup/", {"text": "stuff"}, format="json")
        self.assertEqual(res.status_code, 429)

    def test_garbled_or_empty_answers(self):
        with mock.patch("concierge.onboard.chat", return_value=answer("Sure! Here's your plan.")):
            self.assertEqual(self.client.post("/api/concierge/setup/", {"text": "stuff"}, format="json").status_code, 502)
        with mock.patch("concierge.onboard.chat", return_value=answer({"message": "", "projects": [], "tasks": []})):
            self.assertEqual(self.client.post("/api/concierge/setup/", {"text": "stuff"}, format="json").status_code, 422)
        self.assertEqual(self.client.post("/api/concierge/setup/", {"text": "  "}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/concierge/setup/", {"text": "x", "workspace": 999999}, format="json").status_code, 404)

    def test_brief_of_today_and_chat_availability(self):
        res = self.client.get("/api/concierge/brief/")
        self.assertEqual((res.data["brief"], res.data["available"]), (None, False))  # free plan: no daily brief
        ConciergeMessage.objects.create(user=self.user, role="assistant", content="Good morning!", channel="brief")
        self.assertEqual(self.client.get("/api/concierge/brief/").data["brief"], "Good morning!")
        chat = self.client.get("/api/concierge/chat/").data
        self.assertFalse(chat["available"])
        self.assertTrue(chat["reason"])
        self.assertEqual(chat["messages"], [])  # briefs aren't part of the chat


class ActivityTests(APITestCase):
    def test_last_seen_and_admin_activation(self):
        auth = self.client.post("/api/auth/register/", {"email": "a@example.com", "password": "s3cure-Passw0rd", "full_name": "A"}).data
        user = User.objects.get(email="a@example.com")
        self.assertIsNone(user.last_seen_at)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.client.get("/api/workspaces/")
        user.refresh_from_db()
        first = user.last_seen_at
        self.assertIsNotNone(first)
        self.client.get("/api/workspaces/")  # within 5 minutes: no write
        user.refresh_from_db()
        self.assertEqual(user.last_seen_at, first)

        ws = self.client.get("/api/workspaces/").data[0]["id"]
        self.client.post(f"/api/workspaces/{ws}/tasks/", {"title": "One"}, format="json")
        user.is_staff = True
        user.save()
        row = next(u for u in self.client.get("/api/admin/users/").data if u["id"] == user.pk)
        self.assertEqual((row["tasks"], row["flo_messages"], row["telegram"], row["stage"]), (1, 0, False, "first_task,active"))
        funnel = self.client.get("/api/admin/overview/").data["activation"]
        self.assertEqual((funnel["signed_up"], funnel["first_task"], funnel["used_flo"], funnel["active_7d"]), (1, 1, 0, 1))


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model")
class StepLimitTests(APITestCase):
    def test_flo_answers_even_after_using_every_step(self):
        from concierge import brain

        user = User.objects.create_user(email="s@example.com", password="s3cure-Passw0rd", plan="pro_plus")
        ConciergeProfile.objects.create(user=user, timezone="Europe/Rome")
        call = {"id": "c1", "type": "function", "function": {"name": "list_tasks", "arguments": "{}"}}
        busy = {"message": {"content": "", "tool_calls": [call]}, "usage": {}}
        done = {"message": {"content": "Moved the gym to tomorrow."}, "usage": {}}
        with mock.patch("concierge.brain.chat", side_effect=[busy] * brain.MAX_STEPS + [done]) as llm:
            self.assertEqual(brain.run(user, [{"role": "user", "content": "move the gym"}]), "Moved the gym to tomorrow.")
        self.assertIsNone(llm.call_args.kwargs["tools"])  # the last call can only answer

    def test_flo_wraps_up_when_out_of_time(self):
        from concierge import brain

        user = User.objects.create_user(email="t@example.com", password="s3cure-Passw0rd", plan="pro_plus")
        ConciergeProfile.objects.create(user=user, timezone="Europe/Rome")
        call = {"id": "c1", "type": "function", "function": {"name": "list_tasks", "arguments": "{}"}}
        busy = {"message": {"content": "", "tool_calls": [call]}, "usage": {}}
        done = {"message": {"content": "Here's what I did."}, "usage": {}}
        ticks = iter([0, 10, 80])  # start, first step, second step: over budget
        with mock.patch("concierge.brain.clock", side_effect=lambda: next(ticks)), \
                mock.patch("concierge.brain.chat", side_effect=[busy, done]) as llm:
            self.assertEqual(brain.run(user, [{"role": "user", "content": "do lots"}]), "Here's what I did.")
        self.assertEqual(llm.call_count, 2)


class FindTaskTests(APITestCase):
    def test_search_by_name_spans_every_date(self):
        from concierge.tools import Toolbox
        from tasks.services import default_status

        user = User.objects.create_user(email="f@example.com", password="s3cure-Passw0rd", full_name="F")
        from workspaces.services import create_workspace

        ws = create_workspace("Personal", user)
        today = timezone.localdate()
        Task.objects.create(workspace=ws, title="Buy a birthday gift for Marta", status=default_status(ws), assignee=user,
                            created_by=user, planned_date=today + timedelta(days=2))
        Task.objects.create(workspace=ws, title="Call the bank", status=default_status(ws), assignee=user, created_by=user, planned_date=today)
        box = Toolbox(user, None, today)
        self.assertIn("gift for Marta", box.tool_list_tasks(query="gift Marta"))
        self.assertNotIn("gift", box.tool_list_tasks())  # no query: today only
        self.assertEqual(box.tool_list_tasks(query="gift", day="today").split("\n")[0], "No tasks.")

    def test_repeat_has_its_own_tool(self):
        from concierge.tools import Toolbox
        from tasks.services import default_status
        from workspaces.services import create_workspace

        user = User.objects.create_user(email="r@example.com", password="s3cure-Passw0rd", full_name="R")
        ws = create_workspace("Personal", user)
        today = timezone.localdate()
        task = Task.objects.create(workspace=ws, title="Gym", status=default_status(ws), assignee=user, created_by=user, planned_date=today)
        box = Toolbox(user, None, today)
        # A stray "repeat" on an update is ignored: moving a task never makes it recurring.
        box.tool_update_task(task.pk, day="tomorrow", repeat={"freq": "daily"})
        task.refresh_from_db()
        self.assertEqual((task.planned_date, task.recurrence), (today + timedelta(days=1), None))
        self.assertIn("Now repeats", box.tool_set_repeat(task.pk, {"freq": "weekly", "weekdays": [0]}))
        task.refresh_from_db()
        self.assertEqual(task.recurrence["freq"], "weekly")
        self.assertIn("Stopped repeating", box.tool_set_repeat(task.pk, {}))
        task.refresh_from_db()
        self.assertIsNone(task.recurrence)
