
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

    def test_invitation_link_lets_anyone_signed_in_join(self):
        alice = self.register("alice@example.com")
        self.login_as(alice)
        ws = self.client.post("/api/workspaces/", {"name": "Team"}).data
        inv = self.client.post(f"/api/workspaces/{ws['id']}/members/", {"email": "bob@example.com", "role": "admin"}).data
        token = inv["link"].rsplit("/", 1)[1]
        # Plain members don't see join links.
        carol = self.register("carol@example.com")
        self.login_as(carol)
        self.client.credentials()
        preview = self.client.get(f"/api/invitations/{token}/").data
        self.assertEqual((preview["workspace"]["name"], preview["role"], preview["workspace"]["id"]), ("Team", "admin", None))
        self.assertEqual(self.client.post(f"/api/invitations/{token}/accept/").status_code, 401)
        self.login_as(carol)
        res = self.client.post(f"/api/invitations/{token}/accept/")
        self.assertEqual(res.data["workspace"], ws["id"])
        self.assertEqual(self.client.get(f"/api/workspaces/{ws['id']}/").data["my_role"], "admin")
        self.assertEqual(self.client.get(f"/api/invitations/{token}/").status_code, 404)  # used up
        self.login_as(alice)
        self.assertEqual(self.client.get(f"/api/workspaces/{ws['id']}/invitations/").data, [])

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


class TeamWorkspaceTestCase(ApiTestCase):
    def setUp(self):
        self.alice = self.register("alice@example.com")
        self.bob = self.register("bob@example.com")
        self.login_as(self.alice)
        self.ws = self.client.post("/api/workspaces/", {"name": "Team"}).data
        self.client.post(f"/api/workspaces/{self.ws['id']}/members/", {"email": "bob@example.com"})
        self.base = f"/api/workspaces/{self.ws['id']}"

    def system_tags(self, kind):
        groups = self.client.get(f"{self.base}/tag-groups/").data
        return {t["name"]: t for g in groups if g["kind"] == kind for t in g["tags"]}


class TaskTests(TeamWorkspaceTestCase):
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
        statuses = self.system_tags("status")
        self.assertEqual(task["status"], statuses["To do"]["id"])
        self.assertIsNone(task["priority"])

        res = self.client.patch(f"{self.base}/tasks/{task['id']}/", {"status": statuses["Done"]["id"]}, format="json")
        self.assertIsNotNone(res.data["completed_at"])
        res = self.client.patch(f"{self.base}/tasks/{task['id']}/", {"status": statuses["To do"]["id"]}, format="json")
        self.assertIsNone(res.data["completed_at"])

        office = self.client.post(f"{self.base}/tags/", {"name": "@office", "group": group["id"]}).data
        self.assertEqual(office["position"], home["position"] + 1)
        groups = {g["name"]: g for g in self.client.get(f"{self.base}/tag-groups/").data}
        self.assertEqual([t["name"] for t in groups["Context"]["tags"]], ["@home", "@office"])

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
        done = self.system_tags("status")["Done"]["id"]
        mk(title="late done", planned_date="2026-09-28", status=done, assignee=self.alice["user"]["id"])
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


class StatusAndPriorityTagTests(TeamWorkspaceTestCase):
    def test_workspace_has_editable_system_groups(self):
        groups = self.client.get(f"{self.base}/tag-groups/").data
        kinds = {g["kind"]: g for g in groups}
        self.assertEqual([t["name"] for t in kinds["status"]["tags"]], ["To do", "In progress", "Done"])
        self.assertEqual([t["is_done"] for t in kinds["status"]["tags"]], [False, False, True])
        self.assertEqual([t["name"] for t in kinds["priority"]["tags"]], ["Low", "Medium", "High"])

        # Built-in groups can be renamed but not deleted.
        res = self.client.patch(f"{self.base}/tag-groups/{kinds['status']['id']}/", {"name": "Stage"}, format="json")
        self.assertEqual(res.data["name"], "Stage")
        self.assertEqual(self.client.delete(f"{self.base}/tag-groups/{kinds['status']['id']}/").status_code, 400)

        # Custom statuses and priorities.
        review = self.client.post(f"{self.base}/tags/", {"name": "Review", "group": kinds["status"]["id"]}).data
        wont = self.client.post(f"{self.base}/tags/", {"name": "Won't do", "group": kinds["status"]["id"], "is_done": True}).data
        urgent = self.client.post(f"{self.base}/tags/", {"name": "Urgent", "group": kinds["priority"]["id"]}).data
        task = self.client.post(f"{self.base}/tasks/", {"title": "t", "status": review["id"], "priority": urgent["id"]}, format="json").data
        self.assertEqual((task["status"], task["priority"]), (review["id"], urgent["id"]))
        res = self.client.patch(f"{self.base}/tasks/{task['id']}/", {"status": wont["id"]}, format="json")
        self.assertIsNotNone(res.data["completed_at"])

        # Deleting a status moves its tasks to another status with the same done-ness.
        self.client.delete(f"{self.base}/tags/{wont['id']}/")
        task = self.client.get(f"{self.base}/tasks/{task['id']}/").data
        self.assertEqual(task["status"], self.system_tags("status")["Done"]["id"])
        # Deleting a priority clears it.
        self.client.delete(f"{self.base}/tags/{urgent['id']}/")
        self.assertIsNone(self.client.get(f"{self.base}/tasks/{task['id']}/").data["priority"])

    def test_status_and_priority_must_come_from_their_groups(self):
        priorities = self.system_tags("priority")
        statuses = self.system_tags("status")
        free = self.client.post(f"{self.base}/tags/", {"name": "x"}).data
        bad = [
            {"title": "t", "status": priorities["High"]["id"]},
            {"title": "t", "priority": statuses["Done"]["id"]},
            {"title": "t", "status": free["id"]},
            {"title": "t", "tags": [statuses["Done"]["id"]]},
        ]
        for payload in bad:
            self.assertEqual(self.client.post(f"{self.base}/tasks/", payload, format="json").status_code, 400, payload)

    def test_workspace_with_tasks_can_be_deleted(self):
        self.client.post(f"{self.base}/tasks/", {"title": "t"}, format="json")
        self.assertEqual(self.client.delete(f"{self.base}/").status_code, 204)

    def test_last_status_cannot_be_deleted(self):
        statuses = list(self.system_tags("status").values())
        for tag in statuses[:-1]:
            self.assertEqual(self.client.delete(f"{self.base}/tags/{tag['id']}/").status_code, 204)
        self.assertEqual(self.client.delete(f"{self.base}/tags/{statuses[-1]['id']}/").status_code, 400)


class LocalizedDefaultsTests(ApiTestCase):
    def test_new_workspaces_use_the_users_language(self):
        res = self.client.post("/api/auth/register/", {"email": "it@example.com", "password": "s3cure-Passw0rd", "language": "it"})
        self.login_as(res.data)
        ws = self.client.get("/api/workspaces/").data[0]
        groups = {g["kind"]: g for g in self.client.get(f"/api/workspaces/{ws['id']}/tag-groups/").data}
        self.assertEqual(groups["status"]["name"], "Stato")
        self.assertEqual([t["name"] for t in groups["status"]["tags"]], ["Da fare", "In corso", "Fatto"])
        self.assertEqual(self.client.patch("/api/auth/me/", {"language": "xx"}, format="json").data["language"], "en")


class CrossWorkspaceTests(ApiTestCase):
    def test_my_tasks_and_context_span_only_my_workspaces(self):
        alice = self.register("alice@example.com")
        bob = self.register("bob@example.com")
        self.login_as(alice)
        team = self.client.post("/api/workspaces/", {"name": "Team"}).data
        personal = next(w for w in self.client.get("/api/workspaces/").data if w["name"] == "Personal")
        uid = alice["user"]["id"]
        for ws, title in ((team, "team task"), (personal, "personal task")):
            self.client.post(
                f"/api/workspaces/{ws['id']}/tasks/",
                {"title": title, "planned_date": "2026-10-01", "assignee": uid},
                format="json",
            )
        self.client.post(f"/api/workspaces/{team['id']}/projects/", {"name": "Site"})

        self.login_as(bob)  # bob's own task must not leak into alice's view
        bob_ws = self.client.get("/api/workspaces/").data[0]
        self.client.post(f"/api/workspaces/{bob_ws['id']}/tasks/", {"title": "bob task", "planned_date": "2026-10-01"}, format="json")

        self.login_as(alice)
        res = self.client.get("/api/my/tasks/?day=2026-10-01&assignee=me")
        self.assertEqual(sorted(t["title"] for t in res.data), ["personal task", "team task"])
        self.assertEqual({t["workspace"] for t in res.data}, {team["id"], personal["id"]})
        res = self.client.get(f"/api/my/tasks/?day=2026-10-01&workspace={team['id']}")
        self.assertEqual([t["title"] for t in res.data], ["team task"])

        ctx = self.client.get("/api/my/context/").data
        self.assertEqual([c["name"] for c in ctx], ["Personal", "Team"])
        self.assertEqual([p["name"] for p in ctx[1]["projects"]], ["Site"])
        self.assertEqual({g["kind"] for g in ctx[1]["tag_groups"]}, {"status", "priority"})
        self.assertEqual(ctx[1]["members"][0]["user"]["email"], "alice@example.com")



class QuickAddTests(APITestCase):
    def test_single_line_list_workspace_and_day(self):
        from datetime import timedelta

        from concierge.notify import local_today

        auth = self.client.post("/api/auth/register/", {"email": "qa@example.com", "password": "s3cure-Passw0rd", "full_name": "Q"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        personal = self.client.get("/api/workspaces/").data[0]
        other = self.client.post("/api/workspaces/", {"name": "Studio"}).data
        res = self.client.post("/api/my/tasks/quick/", {"text": "Call the plumber !high"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["workspace"]["id"], personal["id"])
        self.assertEqual(res.data["created"][0]["title"], "Call the plumber")
        from accounts.models import User
        from tasks.models import Task

        user = User.objects.get(email="qa@example.com")
        task = Task.objects.get(title="Call the plumber")
        self.assertEqual((task.assignee, task.priority.name, task.planned_date), (user, "High", local_today(user)))
        res = self.client.post("/api/my/tasks/quick/", {"workspace": other["id"], "day": "tomorrow",
                               "text": "- Draft offer\n  for ACME\n- [x] Send invoice\n- Plan Q4 @backlog"}, format="json")
        titles = {t["title"]: t for t in res.data["created"]}
        self.assertEqual(set(titles), {"Draft offer", "Send invoice", "Plan Q4"})
        self.assertEqual(str(titles["Draft offer"]["planned_date"]), str(local_today(user) + timedelta(days=1)))
        self.assertIsNone(titles["Plan Q4"]["planned_date"])
        self.assertTrue(titles["Send invoice"]["done"])
        self.assertEqual(Task.objects.get(title="Draft offer").description, "for ACME")
        self.assertEqual(self.client.post("/api/my/tasks/quick/", {"text": "  "}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/my/tasks/quick/", {"text": "x", "workspace": 999999}, format="json").status_code, 404)
