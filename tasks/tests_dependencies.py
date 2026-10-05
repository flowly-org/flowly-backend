from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from concierge.models import Notification
from tasks.models import Task
from workspaces.models import Membership


class DependencyTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "d@example.com", "password": "s3cure-Passw0rd", "full_name": "D"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.ws = self.client.post("/api/workspaces/", {"name": "Build"}).data["id"]
        self.bob = User.objects.create_user(email="bob@example.com", password="s3cure-Passw0rd", full_name="Bob")
        Membership.objects.create(workspace_id=self.ws, user=self.bob)
        groups = self.client.get(f"/api/workspaces/{self.ws}/tag-groups/").data
        self.done = next(t["id"] for g in groups if g["kind"] == "status" for t in g["tags"] if t["is_done"])

    def task(self, title, **extra):
        return self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": title, **extra}, format="json").data

    def url(self, t):
        return f"/api/workspaces/{self.ws}/tasks/{t['id']}/"

    def test_blocked_by_loops_and_unblock(self):
        design = self.task("Design")
        api = self.task("Build the API")
        ui = self.task("Build the UI", blocked_by=[design["id"], api["id"]], assignee=self.bob.id)
        self.assertIn("blocked_by", ui, ui)
        self.assertEqual(sorted(ui["blocked_by"]), sorted([design["id"], api["id"]]))
        self.assertTrue(ui["blocked"])
        self.assertEqual(self.client.get(self.url(design)).data["blocking"], [ui["id"]])
        changed = self.client.patch(self.url(ui), {"blocked_by": [design["id"]]}, format="json").data
        self.assertEqual((changed["blocked_by"], changed["blocked_by_info"][0]["title"]), ([design["id"]], "Design"))
        self.client.patch(self.url(ui), {"blocked_by": [design["id"], api["id"]]}, format="json")
        # Loops and self-links are refused.
        self.assertEqual(self.client.patch(self.url(design), {"blocked_by": [ui["id"]]}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(self.url(ui), {"blocked_by": [ui["id"]]}, format="json").status_code, 400)
        # Another workspace's task can't be a blocker.
        other = self.client.post("/api/workspaces/", {"name": "Other"}).data["id"]
        foreign = self.client.post(f"/api/workspaces/{other}/tasks/", {"title": "x"}, format="json").data
        self.assertEqual(self.client.patch(self.url(ui), {"blocked_by": [foreign["id"]]}, format="json").status_code, 400)

        self.client.patch(self.url(design), {"status": self.done}, format="json")
        self.assertFalse(Notification.objects.filter(user=self.bob, title__startswith="🔓").exists())  # API still open
        self.client.patch(self.url(api), {"status": self.done}, format="json")
        self.assertTrue(Notification.objects.filter(user=self.bob, title="🔓 Unblocked: Build the UI").exists())
        self.assertFalse(self.client.get(self.url(ui)).data["blocked"])

    def test_timeline_filter_and_flo(self):
        from datetime import date, timedelta

        from concierge.tools import Toolbox

        start = date(2030, 3, 2)
        a = self.task("Spec", planned_date=str(start), due_date=str(start + timedelta(days=4)))
        b = self.task("Ship", due_date=str(start + timedelta(days=20)))
        self.task("Elsewhere", planned_date="2031-01-01")
        rows = self.client.get(f"/api/workspaces/{self.ws}/tasks/?span_from=2030-03-01&span_to=2030-03-31").data
        self.assertEqual(sorted(t["title"] for t in rows), ["Ship", "Spec"])
        me = User.objects.get(email="d@example.com")
        box = Toolbox(me, None, timezone.localdate())
        self.assertIn("now waits for", box.run("set_dependency", f'{{"task_id": {b["id"]}, "depends_on": {a["id"]}}}'))
        self.assertIn(f"blocked by #{a['id']}", box.run("list_tasks", '{"day": "backlog", "everyone": true}') + box.run("list_tasks", '{"day": "2030-03-22", "everyone": true}'))
