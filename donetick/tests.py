import json
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock
from urllib.parse import urlparse, parse_qs

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from concierge.tests import FakeResponse
from tasks.models import Task

from . import engine
from .models import DonetickLink, DonetickOutbox


def noon(day):
    return datetime.combine(day, datetime.min.time().replace(hour=10), tzinfo=dt_timezone.utc).isoformat().replace("+00:00", "Z")


class FakeDonetick:
    """Just enough of Donetick's eAPI: chores, circle members, complete (moves recurring chores on), update, create."""

    def __init__(self, chores, members=None, token="dt-token"):
        self.chores = {c["id"]: {"isActive": True, "labelsV2": [], "assignees": [], "frequencyType": "once", **c} for c in chores}
        self.members = members or []
        self.token = token
        self.calls = []
        self.next_id = 1000

    def __call__(self, method, url, json=None, data=None, headers=None, timeout=None):
        parsed = urlparse(url)
        self.calls.append((method, parsed.path, parse_qs(parsed.query), json))
        if (headers or {}).get("secretkey") != self.token:
            return FakeResponse({"error": "unauthorized"}, status=401)
        path = parsed.path
        if method == "GET" and path == "/eapi/v1/chore":
            return FakeResponse([c for c in self.chores.values() if c["isActive"]])
        if method == "GET" and path == "/eapi/v1/circle/members":
            return FakeResponse(self.members)
        if method == "POST" and path.endswith("/complete"):
            chore = self.chores[int(path.split("/")[-2])]
            if chore["frequencyType"] == "once":
                chore["isActive"] = False
            else:
                due = datetime.fromisoformat(chore["nextDueDate"].replace("Z", "+00:00"))
                chore["nextDueDate"] = (due + timedelta(days=7)).isoformat().replace("+00:00", "Z")
            return FakeResponse(chore)
        if method == "PUT":
            chore = self.chores[int(path.split("/")[-1])]
            if json.get("name"):
                chore["name"] = json["name"]
            if "description" in json:
                chore["description"] = json["description"]
            if json.get("dueDate"):
                chore["nextDueDate"] = json["dueDate"]
            return FakeResponse(chore)
        if method == "POST" and path == "/eapi/v1/chore":
            self.next_id += 1
            chore = {"id": self.next_id, "name": json["name"], "description": json.get("description"), "nextDueDate": json.get("dueDate"),
                     "isActive": True, "labelsV2": [], "assignees": [], "frequencyType": "once"}
            self.chores[chore["id"]] = chore
            return FakeResponse(chore, status=201)
        return FakeResponse({"error": "not found"}, status=404)


@override_settings(INTEGRATIONS_ALLOW_PRIVATE_URLS=True, API_PUBLIC_URL="https://api.example")
class DonetickTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "marta@example.com", "password": "s3cure-Passw0rd", "full_name": "Marta"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="marta@example.com")
        self.ws = self.client.get("/api/workspaces/").data[0]["id"]  # Personal
        self.home = self.client.post("/api/workspaces/", {"name": "Home"}).data["id"]
        self.garden = self.client.post(f"/api/workspaces/{self.home}/projects/", {"name": "Garden"}, format="json").data["id"]
        self.today = timezone.localdate()
        self.dt = FakeDonetick([
            {"id": 1, "name": "Water the plants", "labelsV2": [{"id": 1, "name": "garden"}], "frequencyType": "weekly",
             "nextDueDate": noon(self.today), "assignedTo": 7, "priority": 1},
            {"id": 2, "name": "Take out the trash", "nextDueDate": noon(self.today + timedelta(days=1)), "assignedTo": 8,
             "description": "<p>Blue bin <b>and</b> glass</p>"},
            {"id": 3, "name": "Fix the bike", "labelsV2": [{"id": 2, "name": "errands"}], "assignedTo": 7},
        ], members=[{"userId": 7, "username": "marta", "displayName": "Marta"}, {"userId": 8, "username": "luca", "displayName": "Luca"}])

    def http(self):
        return mock.patch("integrations.http.requests.request", self.dt)

    def add_integration(self, **config):
        res = self.client.post(f"/api/workspaces/{self.home}/integrations/", {
            "kind": "donetick", "name": "Donetick", "api_key": "dt-token",
            "config": {"base_url": "https://chores.example.org/", "label_projects": {"Garden": self.garden}, **config},
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def sync(self, integration_id):
        with self.http():
            return self.client.post(f"/api/workspaces/{self.home}/integrations/{integration_id}/test/").data

    def work(self):
        with self.http():
            DonetickOutbox.objects.update(next_attempt_at=timezone.now())
            engine.send_outbox()

    def test_workspace_integration_two_way(self):
        integration = self.add_integration()
        self.assertEqual(integration["config"]["base_url"], "https://chores.example.org")
        self.assertEqual(integration["info"]["webhook_url"], f"https://api.example/api/hooks/donetick/{integration['token']}/")
        res = self.sync(integration["id"])
        self.assertEqual(res["status"], "success", res)
        self.assertIn("Synced 3 chore(s)", res["response_body"])
        tasks = {t.title: t for t in Task.objects.filter(workspace_id=self.home)}
        self.assertEqual(set(tasks), {"Water the plants", "Take out the trash", "Fix the bike"})
        water = tasks["Water the plants"]
        self.assertEqual(water.project_id, self.garden)  # label → project
        self.assertEqual(water.planned_date, self.today)
        self.assertEqual(water.assignee, self.user)  # Donetick "marta" ↔ marta@example.com
        self.assertEqual(water.priority.name, water.workspace.tags.filter(group__kind="priority").order_by("position").first().name)
        self.assertEqual(tasks["Take out the trash"].description, "Blue bin and glass")
        self.assertIsNone(tasks["Take out the trash"].project_id)
        self.assertIsNone(tasks["Take out the trash"].assignee)  # Luca isn't in the workspace
        self.assertIsNone(tasks["Fix the bike"].planned_date)
        info = self.client.get(f"/api/workspaces/{self.home}/integrations/{integration['id']}/").data["info"]
        self.assertEqual(info["labels"], ["errands", "garden"])
        self.assertEqual(len(info["members"]), 2)

        # A second sync changes nothing.
        self.sync(integration["id"])
        self.assertEqual(Task.objects.filter(workspace_id=self.home).count(), 3)

        # Done in Flowly → completed in Donetick (as Marta) → next week's occurrence appears.
        done = water.workspace.tags.get(group__kind="status", is_done=True)
        self.client.patch(f"/api/workspaces/{self.home}/tasks/{water.id}/", {"status": done.id}, format="json")
        self.work()
        complete = [c for c in self.dt.calls if c[1] == "/eapi/v1/chore/1/complete"]
        self.assertEqual(complete[0][2], {"completedBy": ["7"]})
        nxt = Task.objects.get(workspace_id=self.home, title="Water the plants", status__is_done=False)
        self.assertEqual(nxt.planned_date, self.today + timedelta(days=7))
        self.assertEqual(nxt.project_id, self.garden)

        # Renamed and rescheduled in Flowly → updated in Donetick (no echo on the next sync).
        trash = tasks["Take out the trash"]
        self.client.patch(f"/api/workspaces/{self.home}/tasks/{trash.id}/", {"title": "Trash & glass", "planned_date": str(self.today + timedelta(days=2))}, format="json")
        self.work()
        put = [c for c in self.dt.calls if c[0] == "PUT"][-1]
        self.assertEqual(put[3]["name"], "Trash & glass")
        self.assertTrue(put[3]["dueDate"].startswith(str(self.today + timedelta(days=2)) + "T12:00"))
        self.sync(integration["id"])
        trash.refresh_from_db()
        self.assertEqual(trash.title, "Trash & glass")
        self.assertEqual(trash.planned_date, self.today + timedelta(days=2))

        # Renamed in Donetick → renamed in Flowly; a Flowly edit to the notes isn't overwritten meanwhile.
        self.dt.chores[3]["name"] = "Fix the bike brakes"
        bike = tasks["Fix the bike"]
        self.client.patch(f"/api/workspaces/{self.home}/tasks/{bike.id}/", {"description": "Buy pads"}, format="json")
        self.work()
        self.sync(integration["id"])
        bike.refresh_from_db()
        self.assertEqual((bike.title, bike.description), ("Fix the bike brakes", "Buy pads"))

        # Completed in Donetick (webhook) → done in Flowly, then the poll brings the next occurrence.
        self.client.credentials()
        self.dt.chores[1]["nextDueDate"] = noon(self.today + timedelta(days=7))
        payload = {"type": "task.completed", "data": {"chore": dict(self.dt.chores[1]), "display_name": "Luca"}}
        res = self.client.post(f"/api/hooks/donetick/{integration['token']}/", payload, format="json")
        self.assertEqual(res.data["handled"], "task.completed")
        nxt.refresh_from_db()
        self.assertTrue(nxt.status.is_done)
        self.assertIn("by Luca", nxt.comments.get().body)
        self.dt.chores[1]["nextDueDate"] = noon(self.today + timedelta(days=14))
        with self.http():
            engine.poll(engine.Source(integration=nxt.workspace.integrations.get()))
        self.assertTrue(Task.objects.filter(workspace_id=self.home, title="Water the plants", planned_date=self.today + timedelta(days=14)).exists())
        self.assertEqual(self.client.post("/api/hooks/donetick/nope/", payload, format="json").status_code, 404)

        # A one-off chore completed in Donetick leaves the list: the task is ticked.
        self.dt.chores[3]["isActive"] = False
        with self.http():
            engine.poll(engine.Source(integration=nxt.workspace.integrations.get()))
        bike.refresh_from_db()
        self.assertTrue(bike.status.is_done)

    def test_recurring_done_elsewhere_skip_and_deleted_task(self):
        integration = self.add_integration(labels=["garden"])
        self.sync(integration["id"])
        self.assertEqual(list(Task.objects.filter(workspace_id=self.home).values_list("title", flat=True)), ["Water the plants"])  # label filter
        src = engine.Source(integration=Task.objects.get(workspace_id=self.home).workspace.integrations.get())
        # Skipped in Donetick: moved, not ticked.
        self.client.credentials()
        self.client.post(f"/api/hooks/donetick/{integration['token']}/", {"type": "task.skipped", "data": {"chore": self.dt.chores[1]}}, format="json")
        self.dt.chores[1]["nextDueDate"] = noon(self.today + timedelta(days=7))
        with self.http():
            engine.poll(src)
        task = Task.objects.get(workspace_id=self.home)
        self.assertFalse(task.status.is_done)
        self.assertEqual(task.planned_date, self.today + timedelta(days=7))
        # Done in Donetick without a webhook (poll only), once its day has come: ticked + next occurrence.
        with self.http():
            reconcile_today = self.today + timedelta(days=7)
            self.dt.chores[1]["nextDueDate"] = noon(self.today + timedelta(days=14))
            for chore in [self.dt.chores[1]]:
                engine.reconcile_chore(src, chore, today=reconcile_today)
        task.refresh_from_db()
        self.assertTrue(task.status.is_done)
        new = Task.objects.get(workspace_id=self.home, status__is_done=False)
        # Deleted in Flowly: not brought back.
        new.delete()
        with self.http():
            engine.poll(src)
        self.assertFalse(Task.objects.filter(workspace_id=self.home, status__is_done=False).exists())
        self.assertTrue(DonetickLink.objects.filter(task__isnull=True).exists())

    def test_create_chores_from_new_tasks(self):
        integration = self.add_integration(create_chores=True)
        res = self.client.post(f"/api/workspaces/{self.home}/tasks/", {"title": "Buy light bulbs", "planned_date": str(self.today)}, format="json")
        self.work()
        created = [c for c in self.dt.calls if c[0] == "POST" and c[1] == "/eapi/v1/chore"]
        self.assertEqual(created[0][3]["name"], "Buy light bulbs")
        link = DonetickLink.objects.get(task_id=res.data["id"])
        self.assertEqual(link.integration_id, integration["id"])
        self.sync(integration["id"])
        self.assertEqual(Task.objects.filter(title="Buy light bulbs").count(), 1)  # not duplicated by the poll

    def test_personal_connection_routes_by_label(self):
        mensa = self.client.post("/api/workspaces/", {"name": "Mensa"}).data["id"]
        kitchen = self.client.post(f"/api/workspaces/{mensa}/projects/", {"name": "Kitchen"}, format="json").data["id"]
        stranger = User.objects.create_user(email="x@example.com", password="s3cure-Passw0rd")
        from workspaces.models import Membership, Workspace

        foreign = Workspace.objects.create(name="Not mine")
        Membership.objects.create(workspace=foreign, user=stranger, role="owner")
        bad = self.client.post("/api/my/donetick/", {"base_url": "https://chores.example.org", "token": "dt-token",
                                                    "routes": [{"label": "x", "workspace": foreign.id}]}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.post("/api/my/donetick/", {"base_url": "https://chores.example.org"}, format="json").status_code, 400)

        self.dt.chores[4] = {"id": 4, "name": "Order vegetables", "labelsV2": [{"id": 3, "name": "Mensa"}], "assignedTo": 7,
                             "assignees": [{"userId": 7}], "isActive": True, "frequencyType": "once", "nextDueDate": noon(self.today)}
        with self.http():
            res = self.client.post("/api/my/donetick/", {
                "base_url": "https://chores.example.org", "token": "dt-token", "donetick_user_id": 7, "only_mine": True,
                "routes": [{"label": "mensa", "workspace": mensa, "project": kitchen}, {"label": "garden", "workspace": self.home}],
            }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertNotIn("dt-token", res.content.decode())
        titles = {t.title: t for t in Task.objects.all()}
        self.assertEqual(set(titles), {"Order vegetables", "Water the plants"})  # Fix the bike: no route; trash: Luca's
        veg = titles["Order vegetables"]
        self.assertEqual((veg.workspace_id, veg.project_id, veg.assignee, veg.visibility), (mensa, kitchen, self.user, "private"))
        self.assertEqual(titles["Water the plants"].workspace_id, self.home)
        self.assertEqual(res.data["info"]["labels"], ["errands", "garden", "Mensa"])

        # A default workspace catches chores without a matching label.
        with self.http():
            res = self.client.patch(f"/api/my/donetick/{res.data['id']}/", {"default_workspace": self.ws}, format="json")
            self.client.post(f"/api/my/donetick/{res.data['id']}/sync/")
        self.assertEqual(Task.objects.get(title="Fix the bike").workspace_id, self.ws)

        # Complete as myself in Donetick.
        done = veg.workspace.tags.get(group__kind="status", is_done=True)
        self.client.patch(f"/api/workspaces/{mensa}/tasks/{veg.id}/", {"status": done.id}, format="json")
        self.work()
        self.assertIn(("POST", "/eapi/v1/chore/4/complete", {"completedBy": ["7"]}, None), self.dt.calls)

    def test_personal_connection_leaves_workspaces_with_their_own_integration(self):
        self.add_integration()
        with self.http():
            self.client.post("/api/my/donetick/", {"base_url": "https://chores.example.org", "token": "dt-token",
                                                  "only_mine": False, "default_workspace": self.home}, format="json")
        # Only the workspace integration's tasks (it hasn't synced yet): none from the personal one.
        self.assertFalse(Task.objects.filter(workspace_id=self.home).exists())

    def test_bad_token_is_reported(self):
        self.dt.token = "other"
        integration = self.add_integration()
        res = self.sync(integration["id"])
        self.assertEqual(res["status"], "failed")
        info = self.client.get(f"/api/workspaces/{self.home}/integrations/{integration['id']}/").data["info"]
        self.assertIn("HTTP 401", info["error"])

    def test_created_webhook_waits_for_the_worker_poll(self):
        integration = self.add_integration()
        self.client.credentials()
        res = self.client.post(f"/api/hooks/donetick/{integration['token']}/", {"type": "task.created", "data": {"chore": self.dt.chores[2]}}, format="json")
        self.assertEqual(res.data["handled"], "task.created")
        self.assertFalse(Task.objects.filter(workspace_id=self.home).exists())
        from .models import DonetickSync

        self.assertLessEqual(DonetickSync.objects.get(integration_id=integration["id"]).next_poll_at, timezone.now())
        engine._last_poll_check = 0
        with self.http():
            engine.tick()
        self.assertEqual(Task.objects.filter(workspace_id=self.home).count(), 3)
