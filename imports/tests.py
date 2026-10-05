import io
import json
from datetime import date
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from accounts.models import User
from concierge.tests import FakeResponse
from tasks.models import Comment, Task
from workspaces.models import Membership

TRELLO = {
    "id": "b1", "name": "Website relaunch",
    "lists": [{"id": "l1", "name": "To do", "closed": False}, {"id": "l2", "name": "Done", "closed": False}],
    "members": [{"id": "m1", "fullName": "Luca Rossi", "username": "luca"}, {"id": "m2", "fullName": "Somebody Else", "username": "x"}],
    "cards": [
        {"id": "c1", "name": "Write the copy", "desc": "Homepage and pricing", "idList": "l1", "due": "2030-02-01T10:00:00.000Z",
         "labels": [{"name": "Content", "color": "green"}], "idMembers": ["m1"]},
        {"id": "c2", "name": "Pick a font", "desc": "", "idList": "l2", "labels": [], "idMembers": ["m2"]},
        {"id": "c3", "name": "Old idea", "idList": "l1", "closed": True, "labels": [], "idMembers": []},
    ],
    "checklists": [{"id": "k1", "idCard": "c1", "checkItems": [{"name": "Hero", "state": "complete", "pos": 1}, {"name": "FAQ", "state": "incomplete", "pos": 2}]}],
    "actions": [{"type": "commentCard", "idMemberCreator": "m1", "memberCreator": {"fullName": "Luca Rossi"}, "date": "2030-01-01",
                 "data": {"text": "Draft in the shared drive", "card": {"id": "c1"}}}],
}

ASANA_CSV = """Task ID,Created At,Completed At,Last Modified,Name,Section/Column,Assignee,Assignee Email,Start Date,Due Date,Tags,Notes,Projects,Parent task
111,2030-01-01,,2030-01-02,Plan the event,Planning,Luca Rossi,luca@example.com,2030-03-01,2030-03-10,"urgent, venue",Budget 5k,Spring event,
112,2030-01-01,2030-01-03,2030-01-03,Book the room,Planning,,,,,,,Spring event,Plan the event
113,2030-01-01,2030-01-05,2030-01-05,Send invites,Done,,,,,,,Spring event,
"""


class ImportTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "imp@example.com", "password": "s3cure-Passw0rd", "full_name": "Imp"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="imp@example.com")
        self.ws = self.client.get("/api/workspaces/").data[0]["id"]
        self.luca = User.objects.create_user(email="luca@example.com", password="s3cure-Passw0rd", full_name="Luca Rossi")
        Membership.objects.create(workspace_id=self.ws, user=self.luca)
        self.url = f"/api/workspaces/{self.ws}/import/"

    def upload(self, source, name, content):
        return self.client.post(self.url, {"source": source, "file": SimpleUploadedFile(name, content.encode())}, format="multipart")

    def test_trello_board(self):
        res = self.upload("trello", "board.json", json.dumps(TRELLO))
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data["created"], res.data["project_name"]), (2, "Website relaunch"))
        copy = Task.objects.get(title="Write the copy")
        self.assertEqual((copy.assignee, copy.due_date, copy.project.name), (self.luca, date(2030, 2, 1), "Website relaunch"))
        self.assertEqual([(i["text"], i["done"]) for i in copy.checklist], [("Hero", True), ("FAQ", False)])
        self.assertEqual(sorted(t.name for t in copy.tags.all()), ["Content", "To do"])
        self.assertEqual(Comment.objects.get(task=copy).external_author, "Luca Rossi")
        font = Task.objects.get(title="Pick a font")
        self.assertTrue(font.status.is_done)  # in the "Done" list
        self.assertIn("(Assigned to Somebody Else in Trello.)", font.description)
        again = self.upload("trello", "board.json", json.dumps(TRELLO)).data
        self.assertEqual((again["created"], again["skipped"]), (0, 2))
        self.assertEqual(self.upload("trello", "x.json", "{not json").status_code, 400)
        self.assertEqual(self.upload("trello", "x.json", json.dumps({"a": 1})).status_code, 400)

    def test_asana_csv(self):
        res = self.upload("asana_csv", "spring.csv", ASANA_CSV)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data["created"], res.data["project_name"]), (2, "Spring event"))
        plan = Task.objects.get(title="Plan the event")
        self.assertEqual((plan.assignee, plan.planned_date, plan.due_date), (self.luca, date(2030, 3, 1), date(2030, 3, 10)))
        self.assertEqual(plan.checklist[0]["text"], "Book the room")
        self.assertTrue(plan.checklist[0]["done"])
        self.assertEqual(sorted(t.name for t in plan.tags.all()), ["Planning", "urgent", "venue"])
        self.assertTrue(Task.objects.get(title="Send invites").status.is_done)

    def test_asana_api(self):
        def fake(method, url, json=None, data=None, headers=None, timeout=None):
            assert headers["Authorization"] == "Bearer pat-1"
            if "/projects/999?" in url:
                return FakeResponse({"data": {"name": "Ops board"}})
            if "/projects/999/tasks" in url:
                return FakeResponse({"data": [{"gid": "t1", "name": "Renew certificates", "notes": "", "completed": False, "due_on": "2030-04-01",
                                               "assignee": {"name": "Imp", "email": "imp@example.com"}, "num_subtasks": 1, "tags": [],
                                               "memberships": [{"project": {"gid": "999"}, "section": {"name": "This week"}}]}], "next_page": None})
            if "/subtasks" in url:
                return FakeResponse({"data": [{"name": "api.example.com", "completed": False}]})
            if "/stories" in url:
                return FakeResponse({"data": [{"type": "comment", "text": "Expires on the 5th", "created_by": {"name": "Luca Rossi"}},
                                              {"type": "system", "text": "added to project"}]})
            raise AssertionError(url)

        with mock.patch("integrations.http.requests.request", side_effect=fake):
            res = self.client.post(self.url, {"source": "asana_api", "token": "pat-1", "project": "https://app.asana.com/0/999/list"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        task = Task.objects.get(title="Renew certificates")
        self.assertEqual((task.assignee, task.project.name, task.checklist[0]["text"]), (self.user, "Ops board", "api.example.com"))
        self.assertEqual(list(task.comments.values_list("body", flat=True)), ["Expires on the 5th"])
        with mock.patch("integrations.http.requests.request", return_value=FakeResponse({"errors": []}, status=401)):
            bad = self.client.post(self.url, {"source": "asana_api", "token": "x", "project": "999"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("personal access token", bad.data["detail"])
