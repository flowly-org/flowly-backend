from datetime import date

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from workspaces.models import Membership, Workspace

User = get_user_model()


class ApiTestCase(APITestCase):
    def register(self, email, password="s3cure-Passw0rd"):
        res = self.client.post("/api/auth/register/", {"email": email, "password": password, "full_name": email.split("@")[0]})
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def login_as(self, auth):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")


class AuthAndWorkspaceTests(ApiTestCase):
    def test_register_creates_personal_workspace_and_login_works(self):
        auth = self.register("alice@example.com")
        self.login_as(auth)
        res = self.client.get("/api/workspaces/")
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]["my_role"], "owner")

        self.client.credentials()
        res = self.client.post("/api/auth/login/", {"email": "alice@example.com", "password": "s3cure-Passw0rd"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("access", res.data)

    def test_invitation_is_consumed_on_signup(self):
        alice = self.register("alice@example.com")
        self.login_as(alice)
        ws = self.client.post("/api/workspaces/", {"name": "Team"}).data
        res = self.client.post(f"/api/workspaces/{ws['id']}/members/", {"email": "bob@example.com"})
        self.assertEqual(res.status_code, 202)

        bob = self.register("bob@example.com")
        self.login_as(bob)
        names = sorted(w["name"] for w in self.client.get("/api/workspaces/").data)
        self.assertEqual(names, ["Personal", "Team"])

    def test_non_members_get_404(self):
        alice = self.register("alice@example.com")
        self.login_as(alice)
        ws = self.client.post("/api/workspaces/", {"name": "Secret"}).data
        mallory = self.register("mallory@example.com")
        self.login_as(mallory)
        self.assertEqual(self.client.get(f"/api/workspaces/{ws['id']}/tasks/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/workspaces/{ws['id']}/").status_code, 404)

    def test_member_cannot_manage_members_or_remove_owner(self):
        alice = self.register("alice@example.com")
        bob = self.register("bob@example.com")
        self.login_as(alice)
        ws = self.client.post("/api/workspaces/", {"name": "Team"}).data
        self.client.post(f"/api/workspaces/{ws['id']}/members/", {"email": "bob@example.com"})
        owner_membership = Membership.objects.get(workspace_id=ws["id"], role="owner")

        self.login_as(bob)
        res = self.client.post(f"/api/workspaces/{ws['id']}/members/", {"email": "carol@example.com"})
        self.assertEqual(res.status_code, 403)
        res = self.client.delete(f"/api/workspaces/{ws['id']}/members/{owner_membership.id}/")
        self.assertEqual(res.status_code, 403)


class TaskTests(ApiTestCase):
    def setUp(self):
        self.alice = self.register("alice@example.com")
        self.bob = self.register("bob@example.com")
        self.login_as(self.alice)
        self.ws = self.client.post("/api/workspaces/", {"name": "Team"}).data
        self.client.post(f"/api/workspaces/{self.ws['id']}/members/", {"email": "bob@example.com"})
        self.base = f"/api/workspaces/{self.ws['id']}"

    def test_task_crud_with_project_tags_and_assignee(self):
        project = self.client.post(f"{self.base}/projects/", {"name": "Website", "color": "#ff0000"}).data
        group = self.client.post(f"{self.base}/tag-groups/", {"name": "Context"}).data
        home = self.client.post(f"{self.base}/tags/", {"name": "@home", "group": group["id"]}).data
        urgent = self.client.post(f"{self.base}/tags/", {"name": "urgent"}).data

        res = self.client.post(
            f"{self.base}/tasks/",
            {
                "title": "Write copy",
                "project": project["id"],
                "tags": [home["id"], urgent["id"]],
                "assignee": self.bob["user"]["id"],
                "planned_date": "2026-10-01",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        task = res.data
        self.assertEqual(task["status"], "todo")

        res = self.client.patch(f"{self.base}/tasks/{task['id']}/", {"status": "done"}, format="json")
        self.assertIsNotNone(res.data["completed_at"])
        res = self.client.patch(f"{self.base}/tasks/{task['id']}/", {"status": "todo"}, format="json")
        self.assertIsNone(res.data["completed_at"])

        office = self.client.post(f"{self.base}/tags/", {"name": "@office", "group": group["id"]}).data
        self.assertEqual(office["position"], home["position"] + 1)
        groups = self.client.get(f"{self.base}/tag-groups/").data
        self.assertEqual([t["name"] for t in groups[0]["tags"]], ["@home", "@office"])

    def test_one_tag_per_group(self):
        group = self.client.post(f"{self.base}/tag-groups/", {"name": "Energy"}).data
        hi = self.client.post(f"{self.base}/tags/", {"name": "high", "group": group["id"]}).data
        lo = self.client.post(f"{self.base}/tags/", {"name": "low", "group": group["id"]}).data
        res = self.client.post(f"{self.base}/tasks/", {"title": "x", "tags": [hi["id"], lo["id"]]}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("tags", res.data)

    def test_cannot_reference_objects_from_other_workspaces(self):
        other_ws = Workspace.objects.get(memberships__user__email="bob@example.com", name="Personal")
        self.login_as(self.bob)
        foreign = self.client.post(f"/api/workspaces/{other_ws.id}/projects/", {"name": "Bob's"}).data
        self.login_as(self.alice)
        res = self.client.post(f"{self.base}/tasks/", {"title": "x", "project": foreign["id"]}, format="json")
        self.assertEqual(res.status_code, 400)
        # Non-member assignee is rejected too.
        carol = self.register("carol@example.com")
        self.login_as(self.alice)
        res = self.client.post(f"{self.base}/tasks/", {"title": "x", "assignee": carol["user"]["id"]}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_day_filter_with_overdue_and_assignee_me(self):
        mk = lambda **kw: self.client.post(f"{self.base}/tasks/", kw, format="json").data
        mk(title="today mine", planned_date="2026-10-01", assignee=self.alice["user"]["id"])
        mk(title="today bob", planned_date="2026-10-01", assignee=self.bob["user"]["id"])
        mk(title="late open", planned_date="2026-09-28", assignee=self.alice["user"]["id"])
        mk(title="late done", planned_date="2026-09-28", status="done", assignee=self.alice["user"]["id"])
        mk(title="backlog")

        titles = lambda q: sorted(t["title"] for t in self.client.get(f"{self.base}/tasks/?{q}").data)
        self.assertEqual(titles("day=2026-10-01"), ["today bob", "today mine"])
        self.assertEqual(titles("day=2026-10-01&assignee=me"), ["today mine"])
        self.assertEqual(titles("day=2026-10-01&assignee=me&include_overdue=true"), ["late open", "today mine"])
        self.assertEqual(titles("unscheduled=true"), ["backlog"])

    def test_removing_member_unassigns_their_tasks(self):
        task = self.client.post(
            f"{self.base}/tasks/", {"title": "x", "assignee": self.bob["user"]["id"]}, format="json"
        ).data
        membership = Membership.objects.get(workspace_id=self.ws["id"], user__email="bob@example.com")
        res = self.client.delete(f"{self.base}/members/{membership.id}/")
        self.assertEqual(res.status_code, 204)
        self.assertIsNone(self.client.get(f"{self.base}/tasks/{task['id']}/").data["assignee"])
