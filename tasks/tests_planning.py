from datetime import date, timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from concierge.models import Notification
from tasks import recurrence
from tasks.models import Reminder, Task


class RecurrenceRuleTests(APITestCase):
    def test_next_dates(self):
        mon = date(2026, 10, 5)  # a Monday
        self.assertEqual(recurrence.next_date({"freq": "daily", "interval": 2}, mon), date(2026, 10, 7))
        self.assertEqual(recurrence.next_date({"freq": "weekly", "interval": 1}, mon), date(2026, 10, 12))
        weekdays = {"freq": "weekly", "interval": 1, "weekdays": [0, 1, 2, 3, 4]}
        self.assertEqual(recurrence.next_date(weekdays, date(2026, 10, 9)), date(2026, 10, 12))  # Fri → Mon
        self.assertEqual(recurrence.next_date({"freq": "weekly", "interval": 2, "weekdays": [0, 3]}, mon), date(2026, 10, 8))
        self.assertEqual(recurrence.next_date({"freq": "weekly", "interval": 2, "weekdays": [0, 3]}, date(2026, 10, 8)), date(2026, 10, 19))
        self.assertEqual(recurrence.next_date({"freq": "monthly", "interval": 1}, date(2026, 1, 31)), date(2026, 2, 28))
        self.assertEqual(recurrence.next_date({"freq": "yearly", "interval": 1}, date(2028, 2, 29)), date(2029, 2, 28))
        self.assertEqual(recurrence.describe({"freq": "weekly", "interval": 2, "weekdays": [0, 3]}), "every 2 weeks on Mon, Thu")
        with self.assertRaises(ValueError):
            recurrence.clean({"freq": "hourly"})


class PlanningTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "p@example.com", "password": "s3cure-Passw0rd", "full_name": "P"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="p@example.com")
        self.ws = self.client.get("/api/workspaces/").data[0]["id"]
        self.today = timezone.localdate()
        statuses = self.client.get(f"/api/workspaces/{self.ws}/tags/").data
        self.done = next(t["id"] for t in statuses if t.get("is_done"))
        self.todo = next(t["id"] for t in statuses if t.get("group_kind", "status") and not t.get("is_done") and t.get("group") is not None
                         and Task._meta.get_field("status").related_model.objects.get(pk=t["id"]).group.kind == "status")

    def url(self, task_id=None):
        return f"/api/workspaces/{self.ws}/tasks/" + (f"{task_id}/" if task_id else "")

    def test_recurring_task_spawns_and_undo(self):
        res = self.client.post(self.url(), {"title": "Water plants", "planned_date": str(self.today), "estimate_minutes": 10,
                                            "checklist": [{"text": "Balcony", "done": True}, "Kitchen"],
                                            "recurrence": {"freq": "weekly", "interval": 1}}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        task = res.data
        self.assertEqual(task["recurrence"], {"freq": "weekly", "interval": 1})
        self.assertEqual([i["text"] for i in task["checklist"]], ["Balcony", "Kitchen"])
        due = timezone.now() + timedelta(hours=3)
        self.client.post(self.url(task["id"]) + "reminders/", {"remind_at": due.isoformat()}, format="json")

        done = self.client.patch(self.url(task["id"]), {"status": self.done}, format="json").data
        self.assertIsNone(done["recurrence"])
        nxt = Task.objects.get(pk=done["next_occurrence"]["id"])
        self.assertEqual(nxt.planned_date, self.today + timedelta(days=7))
        self.assertEqual(nxt.recurrence, {"freq": "weekly", "interval": 1})
        self.assertEqual([i["done"] for i in nxt.checklist], [False, False])
        self.assertEqual(nxt.estimate_minutes, 10)
        self.assertEqual(nxt.series, Task.objects.get(pk=task["id"]).series)
        self.assertEqual(Reminder.objects.get(task=nxt).remind_at, Reminder.objects.get(task_id=task["id"]).remind_at + timedelta(days=7))

        # Unticked by mistake: the untouched next occurrence goes away and the rule comes back.
        back = self.client.patch(self.url(task["id"]), {"status": self.todo}, format="json").data
        self.assertEqual(back["recurrence"], {"freq": "weekly", "interval": 1})
        self.assertFalse(Task.objects.filter(pk=nxt.pk).exists())

    def test_overdue_recurring_catches_up_and_until_stops(self):
        old = self.client.post(self.url(), {"title": "Standup notes", "planned_date": str(self.today - timedelta(days=10)),
                                            "recurrence": {"freq": "daily", "interval": 1}}, format="json").data
        done = self.client.patch(self.url(old["id"]), {"status": self.done}, format="json").data
        self.assertEqual(Task.objects.get(pk=done["next_occurrence"]["id"]).planned_date, self.today)
        last = self.client.post(self.url(), {"title": "Last one", "planned_date": str(self.today),
                                             "recurrence": {"freq": "daily", "interval": 1, "until": str(self.today)}}, format="json").data
        done = self.client.patch(self.url(last["id"]), {"status": self.done}, format="json").data
        self.assertNotIn("next_occurrence", done)

    def test_reminders_fire_once(self):
        from concierge import jobs

        task = self.client.post(self.url(), {"title": "Call the dentist"}, format="json").data
        past = self.client.post(self.url(task["id"]) + "reminders/", {"remind_at": "2020-01-01T10:00"}, format="json")
        self.assertEqual(past.status_code, 400)
        res = self.client.post(self.url(task["id"]) + "reminders/", {"remind_at": (timezone.now() + timedelta(minutes=5)).isoformat()}, format="json")
        self.assertEqual(len(res.data), 1)
        Reminder.objects.update(remind_at=timezone.now() - timedelta(seconds=5))
        jobs.send_reminders()
        jobs.send_reminders()
        n = Notification.objects.get(kind="reminder")
        self.assertEqual(n.title, "⏰ Call the dentist")
        rid = res.data[0]["id"]
        self.assertEqual(self.client.delete(self.url(task["id"]) + f"reminders/{rid}/").data, [])

    def test_day_load_and_flo_tools(self):
        from concierge.tools import Toolbox

        self.client.post(self.url(), {"title": "Report", "planned_date": str(self.today), "estimate_minutes": 120, "assignee": self.user.id}, format="json")
        self.client.post(self.url(), {"title": "Mail", "planned_date": str(self.today), "assignee": self.user.id}, format="json")
        load = self.client.get(f"/api/my/day-load/?date={self.today}").data
        self.assertEqual((load["estimated_minutes"], load["unestimated"], load["capacity_minutes"]), (120, 1, 480))

        box = Toolbox(self.user, None, self.today)
        out = box.run("create_task", '{"title": "Pay rent", "repeat": {"freq": "monthly"}, "estimate_minutes": 5}')
        self.assertIn("repeats every month", out)
        self.assertIn("estimate=5min", out)
        rent = Task.objects.get(title="Pay rent")
        when = (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT09:00")
        self.assertIn("I'll remind you", box.run("set_reminder", f'{{"task_id": {rent.id}, "at": "{when}"}}'))
        self.assertIn("Error", box.run("create_task", '{"title": "x", "repeat": {"freq": "hourly"}}'))
        self.assertIn("checklist 0/2", box.run("add_steps", f'{{"task_id": {rent.id}, "steps": ["Open bank app", "Transfer"]}}') + box.run("list_tasks", "{}"))


class TemplatesAndSearchTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "tpl@example.com", "password": "s3cure-Passw0rd", "full_name": "T"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="tpl@example.com")
        self.ws = self.client.get("/api/workspaces/").data[0]["id"]
        self.clients = self.client.post(f"/api/workspaces/{self.ws}/projects/", {"name": "Clients"}, format="json").data["id"]
        self.start = date(2026, 11, 2)

    def test_parser_estimates_offsets_checklists(self):
        from tasks.bulk import parse, parse_estimate
        from workspaces.models import Workspace

        self.assertEqual([parse_estimate(x) for x in ["~30m", "~1h", "~1h30", "~1.5h", "~90", "~x"]], [30, 60, 90, 90, 90, None])
        drafts = parse("- Kickoff call ~1h @+0\n  [ ] Send agenda\n  [x] Book room\n  bring slides\n- Report @+1w", Workspace.objects.get(pk=self.ws), self.start)
        self.assertEqual(drafts[0]["estimate"], 60)
        self.assertEqual(drafts[0]["checklist"], [{"text": "Send agenda", "done": False}, {"text": "Book room", "done": True}])
        self.assertEqual(drafts[0]["notes"], "bring slides")
        self.assertEqual(drafts[1]["date"], self.start + timedelta(days=7))

    def test_template_crud_apply_and_from_tasks(self):
        url = f"/api/workspaces/{self.ws}/templates/"
        res = self.client.post(url, {"name": "Client onboarding", "project": self.clients,
                                     "body": "- Kickoff call ~1h\n  [ ] Send agenda\n- Send contract @+1 ~15m\n- Follow-up @+7"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["count"], 3)
        created = self.client.post(f"{url}{res.data['id']}/apply/", {"start": str(self.start)}, format="json").data
        self.assertEqual([(t["title"], t["planned_date"], t["project"], t["estimate_minutes"]) for t in created], [
            ("Kickoff call", "2026-11-02", self.clients, 60), ("Send contract", "2026-11-03", self.clients, 15),
            ("Follow-up", "2026-11-09", self.clients, None)])
        self.assertEqual(created[0]["checklist"][0]["text"], "Send agenda")
        self.assertEqual(created[0]["assignee"], self.user.id)
        # Save existing tasks as a template (dates relative to the first).
        tpl = self.client.post(f"{url}from-tasks/", {"name": "Copy", "tasks": [t["id"] for t in created]}, format="json").data
        self.assertIn("- Send contract +Clients @+1 ~15m", tpl["body"])
        self.assertIn("  [ ] Send agenda", tpl["body"])

        from concierge.tools import Toolbox

        out = Toolbox(self.user, None, self.start).run("use_template", '{"name": "onboarding", "start": "2026-12-01"}')
        self.assertIn("Created 3 tasks", out)

    def test_search(self):
        a = self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": "Renew the domain", "description": "flowly.net at Cloudflare"}, format="json").data
        b = self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": "Budget", "checklist": ["Ask about domain costs"]}, format="json").data
        c = self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": "Call Anna"}, format="json").data
        self.client.post(f"/api/workspaces/{self.ws}/tasks/{c['id']}/comments/", {"body": "She manages the domain transfer"}, format="json")
        other = User.objects.create_user(email="o@example.com", password="s3cure-Passw0rd")
        from workspaces.models import Membership

        Membership.objects.create(workspace_id=self.ws, user=other)
        Task.objects.create(workspace_id=self.ws, title="Private domain note", visibility="private", created_by=other,
                            status=Task.objects.get(pk=a["id"]).status)
        res = self.client.get("/api/my/search/?q=domain").data
        self.assertEqual([r["id"] for r in res if r["type"] == "task"], [a["id"], b["id"], c["id"]])
        self.assertIn("transfer", res[2]["snippet"])
        self.assertTrue(any(r["type"] == "project" for r in self.client.get("/api/my/search/?q=clie").data))
        self.assertEqual(self.client.get("/api/my/search/?q=d").data, [])
