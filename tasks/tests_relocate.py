from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from integrations.models import ExternalLink, Integration
from timetracking.models import Invoice, TimeEntry
from workspaces.models import Membership, Workspace

from .models import Project, Tag, TagGroup, Task, TaskDependency
from .tests import ApiTestCase

User = get_user_model()


class RelocateTests(ApiTestCase):
    def setUp(self):
        self.alice_auth = self.register("alice@example.com")
        self.alice = User.objects.get(email="alice@example.com")
        self.register("bob@example.com")
        self.bob = User.objects.get(email="bob@example.com")
        self.login_as(self.alice_auth)
        self.team = Workspace.objects.get(pk=self.client.post("/api/workspaces/", {"name": "Team"}).data["id"])
        Membership.objects.create(workspace=self.team, user=self.bob, role="member")
        self.personal = Workspace.objects.get(memberships__user=self.alice, name="Personal")
        self.project = Project.objects.create(workspace=self.team, name="Website", color="#ef4444")
        statuses = Tag.objects.filter(workspace=self.team, group__kind="status")
        # A custom status the target doesn't have, a custom tag group, a priority.
        self.review = Tag.objects.create(workspace=self.team, group=statuses.first().group, name="Review", position=5)
        context = TagGroup.objects.create(workspace=self.team, name="Context", position=3)
        self.phone = Tag.objects.create(workspace=self.team, group=context, name="Phone")
        high = Tag.objects.get(workspace=self.team, group__kind="priority", name="High")
        self.a = Task.objects.create(workspace=self.team, project=self.project, title="Design", status=statuses.get(name="In progress"),
                                     priority=high, assignee=self.bob, created_by=self.alice)
        self.a.tags.add(self.phone)
        self.b = Task.objects.create(workspace=self.team, project=self.project, title="Copy", status=self.review, created_by=self.alice)
        self.outside = Task.objects.create(workspace=self.team, title="Elsewhere", status=statuses.first(), created_by=self.alice)
        TaskDependency.objects.create(task=self.a, depends_on=self.outside)
        TaskDependency.objects.create(task=self.b, depends_on=self.a)
        github = Integration.objects.create(workspace=self.team, kind="github", name="Repo")
        ExternalLink.objects.create(integration=github, task=self.a, external_id="1")
        self.hook = Integration.objects.create(workspace=self.team, project=self.project, kind="outgoing_webhook", name="Hook")
        ExternalLink.objects.create(integration=self.hook, task=self.b, external_id="2")
        start = timezone.now() - timedelta(hours=2)
        self.entry = TimeEntry.objects.create(workspace=self.team, user=self.bob, task=self.a, project=self.project,
                                              started_at=start, ended_at=start + timedelta(hours=1), duration_seconds=3600)
        today = timezone.localdate()
        self.invoice = Invoice.objects.create(workspace=self.team, project=self.project, number="INV-1", issue_date=today,
                                              due_date=today, period_start=today, period_end=today, currency="EUR")
        Project.objects.create(workspace=self.personal, name="Website")
        self.url = f"/api/workspaces/{self.team.pk}/projects/{self.project.pk}/"

    def test_dry_run_reports_without_changing(self):
        res = self.client.post(self.url + "move/", {"workspace": self.personal.pk, "dry_run": True}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data["tasks"], res.data["unassigned"], res.data["dropped_links"], res.data["dropped_dependencies"],
                          res.data["invoices_staying"], res.data["time_entries"]), (2, 1, 1, 1, 1, 1))
        self.project.refresh_from_db()
        self.assertEqual(self.project.workspace, self.team)

    def test_move_maps_everything_onto_the_target(self):
        res = self.client.post(self.url + "move/", {"workspace": self.personal.pk}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.project.refresh_from_db()
        self.assertEqual((self.project.workspace, self.project.name), (self.personal, "Website (2)"))
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual({self.a.workspace, self.b.workspace}, {self.personal})
        # Same-named status kept; the unknown "Review" maps to an open status of the target, not a new column.
        self.assertEqual((self.a.status.workspace, self.a.status.name), (self.personal, "In progress"))
        self.assertEqual(self.b.status.workspace, self.personal)
        self.assertFalse(self.b.status.is_done)
        self.assertFalse(Tag.objects.filter(workspace=self.personal, name="Review").exists())
        self.assertEqual((self.a.priority.workspace, self.a.priority.name), (self.personal, "High"))
        # Custom tags are recreated in the target; bob isn't a member there.
        self.assertEqual([(t.workspace_id, t.group.name, t.name) for t in self.a.tags.all()], [(self.personal.pk, "Context", "Phone")])
        self.assertIsNone(self.a.assignee)
        # The workspace-wide GitHub link is dropped; the project's own integration moves with its link.
        self.assertEqual(list(self.a.external_links.all()), [])
        self.hook.refresh_from_db()
        self.assertEqual(self.hook.workspace, self.personal)
        self.assertEqual(self.b.external_links.count(), 1)
        self.assertFalse(TaskDependency.objects.filter(depends_on=self.outside).exists())
        self.assertTrue(TaskDependency.objects.filter(task=self.b, depends_on=self.a).exists())
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.workspace, self.personal)
        self.invoice.refresh_from_db()
        self.assertEqual((self.invoice.workspace, self.invoice.project), (self.team, None))
        self.outside.refresh_from_db()
        self.assertEqual(self.outside.workspace, self.team)

    def test_promote_creates_a_workspace_with_the_people_involved(self):
        dry = self.client.post(self.url + "promote/", {"dry_run": True}, format="json")
        self.assertEqual(dry.data["people_by_choice"], {"involved": 1, "all": 1, "me": 0})
        self.assertEqual(dry.data["unassigned_by_choice"], {"involved": 0, "all": 0, "me": 1})
        res = self.client.post(self.url + "promote/", {"name": "Website Co", "members": "involved"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        ws = Workspace.objects.get(pk=res.data["workspace"])
        self.assertEqual((ws.name, ws.color), ("Website Co", "#ef4444"))
        roles = dict(ws.memberships.values_list("user__email", "role"))
        self.assertEqual(roles, {"alice@example.com": "owner", "bob@example.com": "member"})
        self.b.refresh_from_db()
        self.a.refresh_from_db()
        # The source's statuses are copied, so the custom one survives; bob stays assigned.
        self.assertEqual((self.b.status.workspace, self.b.status.name), (ws, "Review"))
        self.assertEqual(self.a.assignee, self.bob)
        self.assertEqual(Project.objects.get(pk=self.project.pk).workspace, ws)

    def test_promote_only_me(self):
        res = self.client.post(self.url + "promote/", {"members": "me"}, format="json")
        ws = Workspace.objects.get(pk=res.data["workspace"])
        self.assertEqual(ws.name, "Website")
        self.assertEqual(ws.memberships.count(), 1)
        self.a.refresh_from_db()
        self.assertIsNone(self.a.assignee)

    def test_only_managers_and_own_workspaces(self):
        stranger = Workspace.objects.create(name="Other")
        res = self.client.post(self.url + "move/", {"workspace": stranger.pk}, format="json")
        self.assertEqual(res.status_code, 400)
        self.login_as(self.client.post("/api/auth/login/", {"email": "bob@example.com", "password": "s3cure-Passw0rd"}).data)
        bob_personal = Workspace.objects.get(memberships__user=self.bob, name="Personal")
        self.assertEqual(self.client.post(self.url + "move/", {"workspace": bob_personal.pk}, format="json").status_code, 403)
        self.assertEqual(self.client.post(self.url + "promote/", {}, format="json").status_code, 403)
