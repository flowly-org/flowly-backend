from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from agile import services
from agile.models import AgileSetup, Sprint, StandupEntry, StandupRound
from concierge.models import Notification
from tasks.models import Task
from workspaces.models import Membership

ROME = ZoneInfo("Europe/Rome")


class AgileTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "lead@example.com", "password": "s3cure-Passw0rd", "full_name": "Lea"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.lead = User.objects.get(email="lead@example.com")
        self.ws = self.client.post("/api/workspaces/", {"name": "Product"}).data["id"]
        self.app = self.client.post(f"/api/workspaces/{self.ws}/projects/", {"name": "App"}, format="json").data["id"]
        self.dev = User.objects.create_user(email="dev@example.com", password="s3cure-Passw0rd", full_name="Dario")
        Membership.objects.create(workspace_id=self.ws, user=self.dev, role="member")
        groups = self.client.get(f"/api/workspaces/{self.ws}/tag-groups/").data
        self.done = next(t["id"] for g in groups if g["kind"] == "status" for t in g["tags"] if t["is_done"])
        self.base = f"/api/workspaces/{self.ws}"

    def task(self, title, **extra):
        return self.client.post(f"{self.base}/tasks/", {"title": title, "project": self.app, **extra}, format="json").data

    def test_opt_in_planning_burndown_close(self):
        self.assertFalse(self.client.get("/api/workspaces/").data[1]["agile"] if len(self.client.get("/api/workspaces/").data) > 1 else False)
        setup = self.client.post(f"{self.base}/agile/", {"project": self.app, "sprint_weeks": 1, "sprint_start": 0}, format="json")
        self.assertEqual(setup.status_code, 201, setup.data)
        sprint = setup.data["sprint"]
        self.assertEqual((sprint["number"], sprint["status"]), (1, "active"))
        self.assertTrue(any(w["agile"] for w in self.client.get("/api/workspaces/").data))
        # Members can't change the setup.
        self.client.force_authenticate(self.dev)
        self.assertEqual(self.client.post(f"{self.base}/agile/", {}, format="json").status_code, 403)
        self.client.force_authenticate(self.lead)

        a = self.task("Login screen", estimate_minutes=120, assignee=self.dev.id)
        b = self.task("API auth", estimate_minutes=60)
        c = self.task("Docs")
        self.client.post(f"{self.base}/sprints/{sprint['id']}/tasks/", {"add": [a["id"], b["id"], c["id"]]}, format="json")
        page = self.client.get(f"{self.base}/sprints/{sprint['id']}/").data
        self.assertEqual(len(page["tasks"]), 3)
        self.assertEqual(page["burndown"]["unit"], "minutes")
        self.assertEqual(page["burndown"]["total"], 210)  # 120 + 60 + 30 for the one without estimate
        self.assertEqual(page["capacity"]["people"], 2)
        self.client.patch(f"{self.base}/sprints/{sprint['id']}/", {"goal": "Sign-in works"}, format="json")
        self.client.patch(f"{self.base}/tasks/{a['id']}/", {"status": self.done}, format="json")

        with self.captureOnCommitCallbacks(execute=True):
            closed = self.client.post(f"{self.base}/sprints/{sprint['id']}/close/").data
        r = closed["review"]
        self.assertEqual((r["committed"], r["done"], r["carried_over"], r["done_minutes"]), (3, 1, 2, 120))
        nxt = Sprint.objects.get(setup_id=setup.data["id"], number=2)
        self.assertEqual(nxt.status, "active")  # closed early: the next sprint starts now
        self.assertEqual(Sprint.objects.filter(setup_id=setup.data["id"]).count(), 2)
        self.assertEqual(set(nxt.tasks.values_list("title", flat=True)), {"API auth", "Docs"})
        self.assertTrue(Notification.objects.filter(user=self.dev, title__contains="Sprint 1 review").exists())
        self.assertIn("Goal: Sign-in works", services.review_text(Sprint.objects.get(pk=sprint["id"])))

    def test_rollover_by_the_worker(self):
        setup = AgileSetup.objects.create(workspace_id=self.ws, sprint_weeks=1, sprint_start=0, standup=False)
        now = datetime(2030, 3, 6, 12, tzinfo=ROME)  # a Wednesday
        services.tick(now)
        first = setup.sprints.get()
        self.assertEqual((str(first.start), str(first.end), first.status), ("2030-03-04", "2030-03-10", "active"))
        services.tick(now + timedelta(days=7))
        self.assertEqual(list(setup.sprints.order_by("number").values_list("number", "status")), [(1, "closed"), (2, "active")])

    def test_standup_round_and_retro(self):
        setup = AgileSetup.objects.create(workspace_id=self.ws, project_id=self.app, standup=True, standup_days=[0, 1, 2, 3, 4, 5, 6])
        a = self.task("Login screen", assignee=self.dev.id, planned_date=str(timezone.now().astimezone(ROME).date()))
        now = timezone.now().astimezone(ROME).replace(hour=9, minute=40)
        services.tick(now)
        self.assertEqual(Notification.objects.filter(title__startswith="🧍 Standup").count(), 2)
        dev_note = Notification.objects.get(user=self.dev, title__startswith="🧍 Standup")
        self.assertIn("Login screen", dev_note.body)
        # The lead answers on the page; Dario keeps his draft through Flo.
        self.client.post(f"{self.base}/agile/{setup.id}/standup/", {"yesterday": "Reviewed PRs", "today": "Release notes", "blockers": ""}, format="json")
        from concierge.tools import Toolbox

        out = Toolbox(self.dev, None, now.date()).run("post_standup", '{"keep_draft": true}')
        self.assertIn("Standup posted", out)
        with mock.patch("agile.services.post_to_team") as post:
            services.tick(now + timedelta(minutes=1))  # everyone answered: summary goes out
        text = post.call_args[0][1]
        self.assertIn("Lea", text)
        self.assertIn("→ Release notes", text)
        self.assertIn("→ Login screen", text)
        self.assertIsNotNone(StandupRound.objects.get(setup=setup).summarised_at)

        sprint = services.current(setup)
        self.client.post(f"{self.base}/sprints/{sprint.id}/retro/", {"kind": "improve", "text": "Smaller PRs"}, format="json")
        item = self.client.get(f"{self.base}/sprints/{sprint.id}/").data["retro"][0]
        self.assertEqual(self.client.patch(f"{self.base}/retro/{item['id']}/", {"vote": True}, format="json").data["votes"], 1)
        action = self.client.post(f"{self.base}/retro/{item['id']}/", {}, format="json").data
        task = Task.objects.get(pk=action["task"])
        self.assertEqual((task.title, task.sprint.number), ("Smaller PRs", 2))
        self.assertEqual(self.client.post(f"{self.base}/sprints/{sprint.id}/retro/", {"kind": "nope", "text": "x"}, format="json").status_code, 400)
