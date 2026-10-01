import hashlib
import hmac
import json
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import override_settings
from rest_framework.test import APITestCase

from integrations.models import Delivery, ExternalLink, GitHubApp, Integration
from integrations.worker import process_due
from workspaces.models import Membership


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data) if data is not None else ""
        self.content = self.text.encode()

    def json(self):
        return self._data


class FakeHttp:
    """Records calls to requests.request and answers from a list of (method, url-substring, response)."""

    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls = []

    def __call__(self, method, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "data": data, "headers": headers or {}})
        for m, fragment, response in self.routes:
            if m == method and fragment in url:
                return response
        return FakeResponse(200, {})


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@override_settings(INTEGRATIONS_ALLOW_PRIVATE_URLS=True, PUBLIC_URL="https://flowly.test")
class IntegrationTestCase(APITestCase):
    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "alice@example.com", "password": "s3cure-Passw0rd", "full_name": "Alice"})
        self.alice = res.data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.alice['access']}")
        self.ws = self.client.post("/api/workspaces/", {"name": "Team"}).data
        self.base = f"/api/workspaces/{self.ws['id']}"
        groups = self.client.get(f"{self.base}/tag-groups/").data
        self.status = {t["name"]: t["id"] for g in groups if g["kind"] == "status" for t in g["tags"]}
        self.project = self.client.post(f"{self.base}/projects/", {"name": "Site"}).data

    def integration(self, kind, config=None, **extra):
        res = self.client.post(f"{self.base}/integrations/", {"kind": kind, "name": kind, "config": config or {}, **extra}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def task(self, **data):
        return self.client.post(f"{self.base}/tasks/", {"title": "Task", **data}, format="json").data


class OutgoingWebhookTests(IntegrationTestCase):
    def test_status_change_is_delivered_signed(self):
        hook = self.integration("outgoing_webhook", {"url": "https://example.com/hook", "events": ["task.status_changed", "task.completed"]})
        task = self.task(project=self.project["id"])
        self.client.patch(f"{self.base}/tasks/{task['id']}/", {"status": self.status["Done"]}, format="json")
        self.assertEqual(sorted(Delivery.objects.values_list("event", flat=True)), ["task.completed", "task.status_changed"])

        http = FakeHttp()
        with mock.patch("integrations.http.requests.request", http):
            self.assertEqual(process_due(), 2)
        call = http.calls[0]
        self.assertEqual(call["headers"]["X-Flowly-Signature"], sign(hook["secret"], call["data"]))
        body = json.loads(call["data"])
        self.assertEqual(body["task"]["status"]["name"], "Done")
        self.assertEqual(body["changes"]["status_id"]["from_value"]["name"], "To do")
        self.assertEqual(set(Delivery.objects.values_list("status", flat=True)), {"success"})

    def test_failures_retry_then_give_up_and_4xx_stops(self):
        self.integration("outgoing_webhook", {"url": "https://example.com/hook", "events": ["task.created"]})
        self.task()
        with mock.patch("integrations.http.requests.request", FakeHttp([("POST", "example.com", FakeResponse(500))])):
            process_due()
        d = Delivery.objects.get()
        self.assertEqual((d.status, d.attempts, d.response_status), ("pending", 1, 500))
        self.assertIsNotNone(d.next_attempt_at)

        Delivery.objects.update(next_attempt_at=d.created_at)
        with mock.patch("integrations.http.requests.request", FakeHttp([("POST", "example.com", FakeResponse(410))])):
            process_due()
        d.refresh_from_db()
        self.assertEqual(d.status, "failed")

    def test_project_scope(self):
        self.integration("outgoing_webhook", {"url": "https://example.com/hook", "events": ["task.created"]}, project=self.project["id"])
        self.task()  # no project → out of scope
        self.task(project=self.project["id"])
        self.assertEqual(Delivery.objects.count(), 1)

    @override_settings(INTEGRATIONS_ALLOW_PRIVATE_URLS=False)
    def test_private_urls_are_refused(self):
        self.integration("outgoing_webhook", {"url": "http://127.0.0.1:5432/", "events": ["task.created"]})
        self.task()
        process_due()
        d = Delivery.objects.get()
        self.assertEqual(d.status, "failed")
        self.assertIn("private address", d.last_error)

    def test_only_managers_manage_integrations(self):
        bob = self.client.post("/api/auth/register/", {"email": "bob@example.com", "password": "s3cure-Passw0rd"}).data
        Membership.objects.create(workspace_id=self.ws["id"], user_id=bob["user"]["id"], role="member")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {bob['access']}")
        self.assertEqual(self.client.get(f"{self.base}/integrations/").status_code, 403)


class IncomingWebhookTests(IntegrationTestCase):
    def test_create_then_update_by_external_id(self):
        hook = self.integration("incoming_webhook", {"schedule": "today"}, project=self.project["id"])
        self.client.post(f"{self.base}/tags/", {"name": "bug"})
        url = f"/api/hooks/{hook['token']}/"
        self.client.credentials()
        res = self.client.post(url, {"external_id": "T-1", "title": "Login broken", "tags": ["BUG", "unknown"], "assignee": "alice@example.com", "priority": "High"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        res2 = self.client.post(url, {"external_id": "T-1", "status": "done"}, format="json")
        self.assertEqual((res2.status_code, res2.data["task"]), (200, res.data["task"]))
        self.assertEqual(self.client.post(url, {"external_id": "T-1", "status": "nope"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(url, {"description": "no title"}, format="json").status_code, 400)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.alice['access']}")
        task = self.client.get(f"{self.base}/tasks/{res.data['task']}/").data
        self.assertEqual(task["status"], self.status["Done"])
        self.assertEqual(task["project"], self.project["id"])
        self.assertEqual(task["assignee"], self.alice["user"]["id"])
        self.assertIsNotNone(task["planned_date"])
        self.assertEqual(len(task["tags"]), 1)

    def test_signature_can_be_required(self):
        hook = self.integration("incoming_webhook", {"require_signature": True})
        body = json.dumps({"title": "Signed"}).encode()
        self.client.credentials()
        url = f"/api/hooks/{hook['token']}/"
        self.assertEqual(self.client.post(url, body, content_type="application/json").status_code, 401)
        res = self.client.post(url, body, content_type="application/json", HTTP_X_FLOWLY_SIGNATURE=sign(hook["secret"], body))
        self.assertEqual(res.status_code, 201)


@override_settings(EMAIL_INBOUND_DOMAIN="in.flowly.test")
class EmailTests(IntegrationTestCase):
    def test_member_email_creates_task_once(self):
        inbox = self.integration("email")
        self.assertEqual(inbox["info"]["address"], f"{inbox['token']}@in.flowly.test")
        self.client.credentials()
        mail = {"to": f"Flowly <x+{inbox['token']}@in.flowly.test>", "from": "Alice <ALICE@example.com>", "subject": "Buy milk", "text": "2 litres", "message_id": "<m1@x>"}
        self.assertEqual(self.client.post("/api/integrations/email/inbound/", mail, format="json").status_code, 200)
        self.client.post("/api/integrations/email/inbound/", mail, format="json")  # provider retry
        self.client.post("/api/integrations/email/inbound/", {**mail, "from": "spam@evil.test", "message_id": "<m2@x>"}, format="json")
        self.assertEqual(list(Integration.objects.get().deliveries.values_list("status", flat=True).order_by("id")), ["success", "ignored", "ignored"])
        from tasks.models import Task

        task = Task.objects.get()
        self.assertEqual((task.title, task.assignee.email), ("Buy milk", "alice@example.com"))


class SlackTests(IntegrationTestCase):
    def test_completed_task_posts_message(self):
        self.integration("slack", {"webhook_url": "https://hooks.slack.com/services/X"})
        task = self.task(project=self.project["id"])
        self.client.patch(f"{self.base}/tasks/{task['id']}/", {"status": self.status["Done"]}, format="json")
        http = FakeHttp()
        with mock.patch("integrations.http.requests.request", http):
            process_due()
        texts = [c["json"]["text"] for c in http.calls]
        self.assertTrue(any(t.startswith("✅ completed") for t in texts), texts)


def rsa_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


class GitHubTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        from integrations import github

        github._tokens.clear()
        self.app = GitHubApp.objects.create(
            app_id=1, slug="flowly-test", name="Flowly", html_url="https://github.com/apps/flowly-test",
            client_id="c", client_secret="s", webhook_secret="whsecret", private_key=rsa_pem(),
        )
        self.gh = self.integration("github", project=self.project["id"])
        Integration.objects.filter(pk=self.gh["id"]).update(config={**self.gh["config"], "installation_id": 77, "repo": "acme/site"})
        self.client.patch("/api/auth/me/", {"github_login": "@alice-gh"}, format="json")
        self.client.post(f"{self.base}/tags/", {"name": "bug"})

    def webhook(self, event, payload):
        body = json.dumps(payload).encode()
        return self.client.post(
            "/api/integrations/github/webhook/", body, content_type="application/json",
            HTTP_X_GITHUB_EVENT=event, HTTP_X_HUB_SIGNATURE_256=sign("whsecret", body),
        )

    def issue_payload(self, action, state="open", **issue):
        return {
            "action": action,
            "installation": {"id": 77},
            "repository": {"full_name": "Acme/Site"},
            "issue": {
                "number": 5, "title": "Crash on save", "body": "Steps…", "state": state,
                "html_url": "https://github.com/acme/site/issues/5",
                "assignees": [{"login": "alice-gh"}], "labels": [{"name": "bug"}, {"name": "flowly: Done"}], **issue,
            },
        }

    def test_issue_lifecycle_and_status_sync_back(self):
        self.assertEqual(self.webhook("issues", self.issue_payload("opened")).status_code, 200)
        link = ExternalLink.objects.get()
        task = link.task
        self.assertEqual((task.title, task.project_id, task.assignee.email), ("Crash on save", self.project["id"], "alice@example.com"))
        self.assertEqual([t.name for t in task.tags.all()], ["bug"])
        self.assertFalse(Delivery.objects.filter(direction="out").exists(), "incoming change must not echo back")

        self.webhook("issues", self.issue_payload("closed", state="closed"))
        task.refresh_from_db()
        self.assertTrue(task.status.is_done)

        # Reopen from Flowly → GitHub issue is reopened via the installation token.
        self.client.patch(f"{self.base}/tasks/{task.id}/", {"status": self.status["In progress"]}, format="json")
        http = FakeHttp([("POST", "/access_tokens", FakeResponse(201, {"token": "inst-token"}))])
        with mock.patch("integrations.http.requests.request", http):
            process_due()
        patch = next(c for c in http.calls if c["method"] == "PATCH")
        self.assertTrue(patch["url"].endswith("/repos/acme/site/issues/5"))
        self.assertEqual(patch["json"], {"state": "open"})
        self.assertEqual(patch["headers"]["Authorization"], "token inst-token")
        self.assertEqual(Delivery.objects.get(direction="out", event="task.status_changed").status, "success")

    def test_bad_signature_and_other_repos_are_ignored(self):
        body = json.dumps(self.issue_payload("opened")).encode()
        res = self.client.post("/api/integrations/github/webhook/", body, content_type="application/json", HTTP_X_GITHUB_EVENT="issues", HTTP_X_HUB_SIGNATURE_256="sha256=bad")
        self.assertEqual(res.status_code, 401)
        payload = self.issue_payload("opened")
        payload["repository"]["full_name"] = "acme/other"
        self.assertEqual(self.webhook("issues", payload).data, {"handled": 0})

    def test_flowly_created_issue_links_back_without_duplicate(self):
        Integration.objects.filter(pk=self.gh["id"]).update(config={**Integration.objects.get(pk=self.gh["id"]).config, "create_issues": True})
        task = self.task(project=self.project["id"], title="From Flowly")
        http = FakeHttp([
            ("POST", "/access_tokens", FakeResponse(201, {"token": "t"})),
            ("POST", "/repos/acme/site/issues", FakeResponse(201, {"number": 9, "html_url": "https://github.com/acme/site/issues/9"})),
        ])
        with mock.patch("integrations.http.requests.request", http):
            process_due()
        create = next(c for c in http.calls if c["url"].endswith("/repos/acme/site/issues"))
        self.assertIn(f"flowly-task:{task['id']}", create["json"]["body"])
        # GitHub's "opened" webhook for that issue updates the same task.
        self.webhook("issues", self.issue_payload("opened", number=9, title="From Flowly", body=create["json"]["body"]))
        from tasks.models import Task

        self.assertEqual(Task.objects.count(), 1)

    def test_comments_sync_both_ways(self):
        self.webhook("issues", self.issue_payload("opened"))
        task = ExternalLink.objects.get().task
        base = f"{self.base}/tasks/{task.id}/comments/"
        # GitHub → Flowly, matched to the member by GitHub login, not echoed back.
        gh_comment = {"id": 501, "body": "Repro attached", "user": {"login": "alice-gh"}, "html_url": "https://github.com/acme/site/issues/5#c501"}
        self.webhook("issue_comment", {**self.issue_payload("created"), "comment": gh_comment})
        comments = self.client.get(base).data
        self.assertEqual((comments[0]["source"], comments[0]["author"]["email"]), ("github", "alice@example.com"))
        self.assertFalse(Delivery.objects.filter(direction="out").exists())
        self.webhook("issue_comment", {**self.issue_payload("edited"), "comment": {**gh_comment, "body": "Repro v2"}})
        self.assertEqual(self.client.get(base).data[0]["body"], "Repro v2")

        # Flowly → GitHub
        mine = self.client.post(base, {"body": "Fixed in main"}, format="json").data
        http = FakeHttp([
            ("POST", "/access_tokens", FakeResponse(201, {"token": "t"})),
            ("POST", "/issues/5/comments", FakeResponse(201, {"id": 777, "html_url": "https://github.com/acme/site/issues/5#c777"})),
        ])
        with mock.patch("integrations.http.requests.request", http):
            process_due()
        posted = next(c for c in http.calls if c["url"].endswith("/issues/5/comments"))
        self.assertIn("Fixed in main", posted["json"]["body"])
        self.assertIn(f"flowly-comment:{mine['id']}", posted["json"]["body"])
        # GitHub's webhook for our own comment is ignored.
        self.webhook("issue_comment", {**self.issue_payload("created"), "comment": {"id": 777, "body": posted["json"]["body"], "user": {"login": "flowly[bot]"}}})
        self.assertEqual(len(self.client.get(base).data), 2)

    def test_install_flow_stores_installation(self):
        Integration.objects.filter(pk=self.gh["id"]).update(config={})
        res = self.client.post(f"{self.base}/integrations/{self.gh['id']}/github/install/")
        state = res.data["url"].split("state=")[1]
        with mock.patch("integrations.http.requests.request", FakeHttp([("GET", "/app/installations/99", FakeResponse(200, {"id": 99}))])):
            res = self.client.post("/api/integrations/github/installed/", {"installation_id": 99, "state": state}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(Integration.objects.get(pk=self.gh["id"]).config["installation_id"], 99)
