import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from concierge.tests import tool_call
from delegation.tests import FakeLLM
from tasks.models import Task

from .models import WikiPage


def make_repo(files: dict) -> str:
    root = tempfile.mkdtemp(prefix="wiki-src-")
    for name, text in files.items():
        path = Path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
           "HOME": "/tmp", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    for cmd in (["init", "-q", "-b", "master"], ["add", "."], ["commit", "-qm", "wiki"]):
        subprocess.run(["git", *cmd], cwd=root, env=env, check=True)
    return root


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model", DELEGATION_MODEL="test/agent",
                   DELEGATION_TRIAGE_MODEL="test/triage")
class WikiTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "w@example.com", "password": "s3cure-Passw0rd", "full_name": "W"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="w@example.com")
        self.ws = self.client.get("/api/workspaces/").data[0]["id"]
        self.project = self.client.post(f"/api/workspaces/{self.ws}/projects/", {"name": "Events"}, format="json").data["id"]
        self.repo = make_repo({
            "Home.md": "Welcome to the Acme handbook. We write in British English and sign emails as 'The Acme team'.",
            "Expense-policy.md": "Expenses: submit receipts within 30 days in the Finance sheet. Hotels up to 140 EUR a night.",
            "Event-checklist.md": "Before an event: book the room, order coffee from Caffè Nero, send the agenda 3 days ahead.",
            "_Sidebar.md": "[[Home]]",
            "logo.png": "not text",
        })

    def add_wiki(self, **config):
        return self.client.post(f"/api/workspaces/{self.ws}/integrations/", {
            "kind": "wiki", "name": "Handbook", "config": {"source": "github_wiki", "repo": "acme/handbook", **config},
        }, format="json")

    def sync(self, integration_id):
        with mock.patch("wiki.sync.clone_url", return_value=self.repo), mock.patch("wiki.sync.ensure_public_url"):
            return self.client.post(f"/api/workspaces/{self.ws}/integrations/{integration_id}/test/")

    def test_config_is_validated_and_normalised(self):
        res = self.add_wiki(repo="https://github.com/acme/handbook/wiki")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["config"], {"source": "github_wiki", "repo": "acme/handbook"})
        self.assertEqual(res.data["info"]["web_url"], "https://github.com/acme/handbook/wiki")
        self.assertEqual(self.add_wiki(repo="not a repo").status_code, 400)
        bad = self.client.post(f"/api/workspaces/{self.ws}/integrations/", {
            "kind": "wiki", "name": "Docs", "config": {"source": "github_repo", "repo": "acme/site", "path": "../etc"}}, format="json")
        self.assertEqual(bad.status_code, 400)
        git = self.client.post(f"/api/workspaces/{self.ws}/integrations/", {
            "kind": "wiki", "name": "Docs", "config": {"source": "git", "url": "http://example.org/x.git"}}, format="json")
        self.assertEqual(git.status_code, 400)

    def test_sync_search_and_flo_tools(self):
        wiki = self.add_wiki().data
        res = self.sync(wiki["id"])
        self.assertEqual(res.data["status"], "success", res.data)
        self.assertIn("Synced 3 page(s)", res.data["response_body"])
        titles = set(WikiPage.objects.values_list("title", flat=True))
        self.assertEqual(titles, {"Home", "Expense policy", "Event checklist"})
        page = WikiPage.objects.get(title="Expense policy")
        self.assertEqual(page.url, "https://github.com/acme/handbook/wiki/Expense-policy")
        info = self.client.get(f"/api/workspaces/{self.ws}/integrations/{wiki['id']}/").data["info"]
        self.assertEqual(info["pages"], 3)
        self.assertEqual(info["error"], "")

        from concierge.tools import Toolbox
        from django.utils import timezone

        box = Toolbox(self.user, None, timezone.localdate())
        found = box.run("search_wiki", '{"query": "hotel expenses limit"}')
        self.assertIn(f"[W{page.id}] Expense policy", found.splitlines()[0])
        self.assertIn("140 EUR", box.run("read_wiki_page", f'{{"page_id": "W{page.id}"}}'))
        # Read-only team chat visitors may search too.
        self.assertIn("Expense", Toolbox(self.user, None, timezone.localdate(), read_only=True).run("search_wiki", '{"query": "expenses"}'))

        # A removed page disappears on the next sync; a failing sync is recorded.
        Path(self.repo, "Event-checklist.md").unlink()
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qam", "rm"], cwd=self.repo, check=True,
                       env={"HOME": "/tmp", "PATH": "/usr/bin:/bin:/usr/local/bin"})
        self.sync(wiki["id"])
        self.assertFalse(WikiPage.objects.filter(title="Event checklist").exists())
        with mock.patch("wiki.sync.clone_url", return_value="/nonexistent/repo"), mock.patch("wiki.sync.ensure_public_url"):
            failed = self.client.post(f"/api/workspaces/{self.ws}/integrations/{wiki['id']}/test/").data
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Couldn't fetch the wiki", self.client.get(f"/api/workspaces/{self.ws}/integrations/{wiki['id']}/").data["info"]["error"])

    def test_wiki_is_context_for_delegation(self):
        from delegation import engine

        wiki = self.add_wiki().data
        self.sync(wiki["id"])
        other = self.client.post(f"/api/workspaces/{self.ws}/projects/", {"name": "Other"}, format="json").data["id"]
        # A project wiki only applies to that project's tasks.
        project_wiki = self.client.post(f"/api/workspaces/{self.ws}/integrations/", {
            "kind": "wiki", "name": "Other docs", "project": other, "config": {"source": "github_wiki", "repo": "acme/other"}}, format="json").data
        WikiPage.objects.create(integration_id=project_wiki["id"], path="Secret.md", title="Other project notes", content="hotel secrets")

        task = Task.objects.get(pk=self.client.post(f"/api/workspaces/{self.ws}/tasks/", {
            "title": "Book a hotel for the Milan event", "project": self.project}, format="json").data["id"])
        text = engine.describe_task(task, wiki_budget=7000)
        self.assertIn("Team wiki", text)
        self.assertIn("British English", text)  # the Home page
        self.assertIn("140 EUR", text)  # the matching page
        self.assertNotIn("Other project notes", text)
        self.assertNotIn("Team wiki", engine.describe_task(task))

        self.user.plan = "pro_plus"
        self.user.save()
        engine.request(task, self.user, run=True)
        page = WikiPage.objects.get(title="Event checklist")
        llm = FakeLLM([
            '{"verdict": "partial", "plan": "Shortlist hotels within policy", "reason": "Booking is up to you"}',
            tool_call("search_wiki", {"query": "event checklist"}),
            tool_call("read_wiki_page", {"page_id": f"W{page.id}"}, "c2"),
            tool_call("finish", {"summary": "Shortlist within the 140 EUR policy", "outcome": "partial"}, "c3"),
        ])
        with mock.patch("integrations.http.requests.request", llm):
            engine.process()
        self.assertIn("Team wiki", llm.calls[0]["messages"][1]["content"])  # triage sees it too (shorter)
        agent_tools = [m for m in llm.calls[-1]["messages"] if m["role"] == "tool"]
        self.assertIn("Event checklist", agent_tools[0]["content"])
        self.assertIn("Caffè Nero", agent_tools[1]["content"])
        self.assertIn("conventions", llm.calls[1]["messages"][0]["content"])
