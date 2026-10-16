import base64
import hashlib
import hmac
import io
import json
import tempfile
import zipfile
from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from integrations.models import Integration
from stairway_tasks import services
from stairway_tasks.models import StairwayRun
from tasks.models import Attachment, Attribute, Comment, Task
from workspaces.models import Membership


def zipped(files: dict) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return base64.b64encode(buf.getvalue()).decode()


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeStairway:
    """Answers tools/call like Stairway: records calls; deliverables come from `files`."""

    def __init__(self):
        self.calls = []
        self.files = {"report.md": "# Findings\nAll good.", "data/table.csv": "a,b\n1,2"}
        self.fail = None

    def __call__(self, method, url, json=None, **kwargs):
        name, args = json["params"].get("name"), json["params"].get("arguments", {})
        self.calls.append((url, name, args))
        if self.fail == name:
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Error: an experiment named 'x' already exists"}], "isError": True}})
        text = {
            "create_experiment": "Created /srv/x. Started.",
            "experiment_info": __import__("json").dumps({"state": "running", "session_url": "https://claude.ai/code/session_1"}),
            "experiment_files": __import__("json").dumps({"files": [{"path": p, "size": len(d)} for p, d in self.files.items()],
                                                          "skipped": ["huge.bin"], "zip_base64": zipped(self.files) if self.files else None}),
        }.get(name, "ok")
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": False}})


@override_settings(API_PUBLIC_URL="https://api.flowly.test", PUBLIC_URL="https://app.flowly.test")
class StairwayTests(APITestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        storage = override_settings(ATTACHMENTS_DIR=self.tmp.name)
        storage.enable()
        self.addCleanup(storage.disable)
        self.fake = FakeStairway()
        patch = mock.patch("stairway_tasks.client.send", self.fake)
        patch.start()
        self.addCleanup(patch.stop)
        self.me = User.objects.create_user(email="me@example.com", password="s3cure-Passw0rd", full_name="Ada")
        self.client.force_authenticate(self.me)
        self.ws = self.client.post("/api/workspaces/", {"name": "Studio"}).data["id"]
        self.base = f"/api/workspaces/{self.ws}"
        self.site = self.client.post(f"{self.base}/projects/", {"name": "Site"}).data["id"]
        Attribute.objects.create(workspace_id=self.ws, key="tone", value="Formal, Italian.")
        self.task = Task.objects.get(pk=self.client.post(f"{self.base}/tasks/", {
            "title": "Competitor analysis", "project": self.site, "description": "Three competitors, in our {{tone}}",
            "checklist": [{"id": "a", "text": "Pricing", "done": False}]}, format="json").data["id"])

    def connect(self, project=None, **config):
        res = self.client.post(f"{self.base}/integrations/", {"kind": "stairway", "name": "Stairway", "project": project, "api_key": "s3cret-token",
                                                              "config": {"url": "https://stairway.example.org/mcp", **config}}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def test_config_is_checked(self):
        for bad in ({"url": "http://stairway.example.org"}, {"url": "https://s.example.org", "agent": "gpt"},
                    {"url": "https://s.example.org", "permission_mode": "yolo"}):
            res = self.client.post(f"{self.base}/integrations/", {"kind": "stairway", "name": "S", "api_key": "t", "config": bad}, format="json")
            self.assertEqual(res.status_code, 400, bad)
        hook = self.connect()
        self.assertEqual(hook["config"]["url"], "https://stairway.example.org")  # /mcp trimmed
        self.assertNotIn("s3cret-token", json.dumps(hook))

    def test_hand_over_reply_and_collect(self):
        self.assertFalse(self.client.get(f"{self.base}/tasks/{self.task.id}/stairway/").data["available"])
        self.connect(agent="opencode", model="deepseek/deepseek-v4.1-flash", instructions="Write in Markdown.")
        res = self.client.post(f"{self.base}/tasks/{self.task.id}/stairway/", {"instructions": "Focus on Italy."}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        run = StairwayRun.objects.get()
        url, tool, args = self.fake.calls[0]
        self.assertEqual((url, tool), ("https://stairway.example.org/mcp/s3cret-token", "create_experiment"))
        self.assertEqual(args["name"], f"flowly-{self.task.ref}")
        self.assertEqual((args["agent"], args["model"], args["permission_mode"]), ("opencode", "deepseek/deepseek-v4.1-flash", "acceptEdits"))
        self.assertEqual(args["callback_url"], f"https://api.flowly.test/api/stairway/callback/{run.token}/")
        for bit in ("Task: Competitor analysis", "Three competitors, in our Formal, Italian.", "- [ ] Pricing",
                    "Write in Markdown.", "Focus on Italy.", "./deliverables/"):
            self.assertIn(bit, args["prompt"])
        self.assertEqual(res.data["session_url"], "https://claude.ai/code/session_1")
        # A second hand-over while it works is refused.
        self.assertEqual(self.client.post(f"{self.base}/tasks/{self.task.id}/stairway/", {}, format="json").status_code, 400)

        # Stairway calls back: unsigned or wrongly signed calls are ignored.
        body = json.dumps({"experiment": run.experiment, "event": "reply", "text": "Done: report in ./deliverables. Decide on pricing.",
                           "url": "https://claude.ai/code/session_1"}).encode()
        self.client.force_authenticate(None)
        cb = f"/api/stairway/callback/{run.token}/"
        self.assertEqual(self.client.post(cb, body, content_type="application/json", HTTP_X_STAIRWAY_SIGNATURE="sha256=nope").status_code, 404)
        sig = "sha256=" + hmac.new(run.secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertEqual(self.client.post(cb, body, content_type="application/json", HTTP_X_STAIRWAY_SIGNATURE=sig).status_code, 204)
        run.refresh_from_db()
        self.assertEqual((run.replies, run.collect), (1, True))
        self.assertTrue(Comment.objects.filter(task=self.task, source="stairway", body__startswith="Done: report").exists())

        services.collect_due()
        run.refresh_from_db()
        self.assertEqual(run.status, "review")
        names = sorted(Attachment.objects.filter(task=self.task, kind="file").values_list("name", flat=True))
        self.assertEqual(names, ["deliverables/data/table.csv", "deliverables/report.md"])
        self.assertTrue(Comment.objects.filter(task=self.task, body__contains="Too large to attach: huge.bin").exists())
        from concierge.models import Notification

        self.assertTrue(Notification.objects.filter(user=self.me, title="Stairway finished “Competitor analysis”").exists())
        # The files download through a signed, expiring link.
        self.client.force_authenticate(self.me)
        att = Attachment.objects.get(name="deliverables/report.md")
        link = self.client.get(f"{self.base}/tasks/{self.task.id}/attachments/{att.id}/download/").data["url"]
        self.assertTrue(link.startswith("https://api.flowly.test/api/files/"))
        self.client.force_authenticate(None)
        got = self.client.get(link.removeprefix("https://api.flowly.test"))
        self.assertEqual(b"".join(got.streaming_content), b"# Findings\nAll good.")
        self.assertEqual(self.client.get("/api/files/forged/").status_code, 404)

        # A follow-up, a new reply: the deliverables are replaced, not added again.
        self.client.force_authenticate(self.me)
        res = self.client.post(f"{self.base}/tasks/{self.task.id}/stairway/{run.id}/prompt/", {"text": "Add Spain."}, format="json")
        self.assertEqual((res.status_code, res.data["status"]), (200, "working"))
        self.assertEqual(self.fake.calls[-1][1:], ("send_prompt", {"name": run.experiment, "prompt": "Add Spain."}))
        self.fake.files = {"report.md": "# Findings v2"}
        services.receive(run, {"experiment": run.experiment, "event": "reply", "text": "Updated."})
        services.collect_due()
        self.assertEqual(list(Attachment.objects.filter(task=self.task).values_list("name", flat=True)), ["deliverables/report.md"])
        res = self.client.post(f"{self.base}/tasks/{self.task.id}/stairway/{run.id}/stop/", {}, format="json")
        self.assertEqual(res.data["status"], "stopped")

    def test_project_integration_wins_and_failures_are_reported(self):
        self.connect()
        self.connect(project=self.site, url="https://site-stairway.example.org")
        self.assertEqual(services.integration_for(self.task).config["url"], "https://site-stairway.example.org")
        self.fake.fail = "create_experiment"
        res = self.client.post(f"{self.base}/tasks/{self.task.id}/stairway/", {}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("already exists", res.data["detail"])
        self.assertEqual(StairwayRun.objects.get().status, "failed")
        # Members without access to a private task can't hand it over.
        other = User.objects.create_user(email="o@example.com", password="s3cure-Passw0rd")
        Membership.objects.create(workspace_id=self.ws, user=other)
        Task.objects.filter(pk=self.task.pk).update(visibility="private")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(f"{self.base}/tasks/{self.task.id}/stairway/").status_code, 404)


    def test_waiting_for_permission_and_answers(self):
        self.connect()
        self.client.post(f"{self.base}/tasks/{self.task.id}/stairway/", {}, format="json")
        run = StairwayRun.objects.get()
        self.assertIn("file tools (Write/Edit), not shell commands", self.fake.calls[0][2]["prompt"])
        body = json.dumps({"experiment": run.experiment, "event": "waiting", "text": "Bash command\n  mkdir deliverables\nDo you want to proceed?"}).encode()
        sig = "sha256=" + hmac.new(run.secret.encode(), body, hashlib.sha256).hexdigest()
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(f"/api/stairway/callback/{run.token}/", body, content_type="application/json", HTTP_X_STAIRWAY_SIGNATURE=sig).status_code, 204)
        run.refresh_from_db()
        self.assertEqual(run.status, "waiting")
        self.assertEqual(run.waiting_for, "Bash command\n  mkdir deliverables")
        from concierge.models import Notification

        self.assertTrue(Notification.objects.filter(user=self.me, title="Stairway needs your OK on “Competitor analysis”").exists())
        self.client.force_authenticate(self.me)
        res = self.client.post(f"{self.base}/tasks/{self.task.id}/stairway/{run.id}/approve/", {}, format="json")
        self.assertEqual((res.status_code, res.data["status"], res.data["waiting_for"]), (200, "working", ""))
        self.assertEqual(self.fake.calls[-1][1:], ("answer_permission", {"name": run.experiment, "allow": True}))
        self.assertTrue(Comment.objects.filter(task=self.task, body__startswith="Ada allowed what Stairway asked").exists())
        self.client.post(f"{self.base}/tasks/{self.task.id}/stairway/{run.id}/deny/", {}, format="json")
        self.assertEqual(self.fake.calls[-1][2]["allow"], False)
