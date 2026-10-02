import json
from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from concierge.models import Notification
from concierge.tests import FakeResponse, tool_call
from tasks.models import Comment, Task

from . import engine
from .models import Delegation


class FakeLLM:
    """Scripted chat completions; records requests (model and messages)."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, method, url, json=None, data=None, headers=None, timeout=None):
        assert "/chat/completions" in url, url
        self.calls.append(json)
        message = self.replies.pop(0)
        if isinstance(message, str):
            message = {"role": "assistant", "content": message}
        return FakeResponse({"choices": [{"message": message}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}})


class FakePage:
    status_code = 200
    url = "https://example.org/hotels"
    headers = {"Content-Type": "text/html; charset=utf-8"}

    class raw:
        @staticmethod
        def read(n, decode_content=True):
            return b"<html><head><title>Hotels</title><script>x()</script></head><body><h1>Hotel Mitte</h1><p>From 120 EUR, 300 m from Friedrichstrasse.</p></body></html>"


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model", DELEGATION_MODEL="test/agent",
                   DELEGATION_TRIAGE_MODEL="test/triage", INTEGRATIONS_ALLOW_PRIVATE_URLS=True)
class DelegationTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "dd@example.com", "password": "s3cure-Passw0rd", "full_name": "Dee"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="dd@example.com")
        self.ws = self.client.get("/api/workspaces/").data[0]["id"]
        self.task = self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": "Find a hotel near Friedrichstraße for the Berlin workshop",
                                                                           "description": "2 nights, budget 150/night"}, format="json").data
        self.url = f"/api/workspaces/{self.ws}/tasks/{self.task['id']}/delegation/"

    def plus(self):
        self.user.plan = "pro_plus"
        self.user.save()

    def run_worker(self, llm):
        with mock.patch("integrations.http.requests.request", llm), mock.patch("delegation.web.requests.get", return_value=FakePage()):
            engine.process()

    def test_needs_pro_plus(self):
        self.user.plan = "pro"
        self.user.save()
        res = self.client.post(self.url, {"action": "run"}, format="json")
        self.assertEqual(res.status_code, 402)
        self.assertFalse(self.client.get(self.url).data["available"])

    def test_assess_only(self):
        self.plus()
        res = self.client.post(self.url, {"action": "assess"}, format="json")
        self.assertEqual(res.data["delegation"]["status"], "queued")
        llm = FakeLLM(['{"verdict": "full", "plan": "Compare 3 hotels with prices and links.", "reason": "Pure research."}'])
        self.run_worker(llm)
        d = self.client.get(self.url).data["delegation"]
        self.assertEqual((d["status"], d["verdict"]), ("assessed", "full"))
        self.assertEqual(llm.calls[0]["model"], "test/triage")
        self.assertEqual(self.client.get(self.url).data["quota"]["used"], 0)  # checks don't count

    def test_run_researches_writes_and_reports(self):
        self.plus()
        self.client.post(self.url, {"action": "run"}, format="json")
        llm = FakeLLM([
            '{"verdict": "partial", "plan": "Shortlist hotels; booking stays with you.", "reason": "Cannot book."}',
            tool_call("web_search", {"query": "hotels near Friedrichstraße Berlin"}),
            "Hotel Mitte (https://example.org/hotels) from 120 EUR.",
            tool_call("fetch_page", {"url": "https://example.org/hotels"}, "c2"),
            tool_call("write_document", {"title": "Hotel shortlist", "content": "| Hotel | Price |\n|---|---|\n| Mitte | 120 € |"}, "c3"),
            tool_call("add_subtask", {"title": "Book Hotel Mitte", "day": "tomorrow"}, "c4"),
            tool_call("finish", {"summary": "Shortlisted 1 hotel within budget.", "outcome": "partial", "left_for_you": "Book it."}, "c5"),
        ])
        self.run_worker(llm)
        d = Delegation.objects.get()
        self.assertEqual(d.status, "partial", d.error)
        self.assertEqual([c["model"] for c in llm.calls], ["test/triage", "test/agent", "test/agent:online", "test/agent", "test/agent", "test/agent", "test/agent"])
        page = next(m for m in llm.calls[4]["messages"] if m["role"] == "tool" and "Hotel Mitte" in m["content"] and "#" in m["content"])
        self.assertNotIn("x()", page["content"])  # scripts stripped
        comments = list(Comment.objects.filter(task_id=self.task["id"]).order_by("id"))
        self.assertEqual([c.source for c in comments], ["ai", "ai"])
        self.assertIn("Shortlisted 1 hotel", comments[0].body)
        self.assertIn("Left for you: Book it.", comments[0].body)
        self.assertTrue(comments[1].body.startswith("📄 Hotel shortlist"))
        sub = Task.objects.get(title="Book Hotel Mitte")
        self.assertEqual(sub.assignee, self.user)
        self.assertIn(self.task["ref"], sub.description)
        self.assertTrue(Notification.objects.filter(user=self.user, title__contains="part of").exists())
        self.assertEqual(self.client.get(self.url).data["quota"], {"used": 1, "limit": 60})
        from concierge.llm import user_usage
        from concierge.models import AIUsage

        self.assertFalse(user_usage(self.user).exists())  # the concierge's daily cap is untouched
        self.assertEqual(AIUsage.objects.get(user=self.user).provider, "Delegation")

    def test_declined_and_monthly_limit(self):
        self.plus()
        self.client.post(self.url, {"action": "run"}, format="json")
        self.run_worker(FakeLLM(['{"verdict": "no", "plan": "", "reason": "It needs a phone call."}']))
        d = Delegation.objects.get()
        self.assertEqual(d.status, "declined")
        self.assertEqual(engine.runs_used(self.user), 0)
        self.assertTrue(Notification.objects.filter(title__contains="can't take on").exists())
        for _ in range(engine.MONTHLY_RUNS):
            Delegation.objects.create(task_id=self.task["id"], user=self.user, run=True, status="done")
        self.assertEqual(self.client.post(self.url, {"action": "run"}, format="json").status_code, 429)

    def test_concierge_delegates_and_suggests(self):
        from concierge import brain
        from concierge.models import ConciergeProfile

        self.plus()
        ConciergeProfile.objects.create(user=self.user, timezone="UTC")
        Task.objects.filter(pk=self.task["id"]).update(planned_date=brain.local_now(self.user).date(), assignee=self.user)
        llm = FakeLLM(['{"verdict": "full", "plan": "Compare hotels.", "reason": "Research."}', "Good morning!"])
        with mock.patch("integrations.http.requests.request", llm):
            brain.brief(self.user)
        self.assertIn("Tasks the AI agent could take on", llm.calls[1]["messages"][-1]["content"])
        self.assertIn(f"#{self.task['id']}", llm.calls[1]["messages"][-1]["content"])
        llm = FakeLLM([tool_call("delegate_task", {"task_id": self.task["id"]}), "On it."])
        with mock.patch("integrations.http.requests.request", llm):
            brain.reply(self.user, "yes, do the hotel one")
        d = Delegation.objects.filter(run=True).get()
        self.assertEqual((d.status, d.verdict), ("queued", "full"))  # reuses the morning check


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model")
class TriageParsingTests(APITestCase):
    def test_cut_off_answer_keeps_its_verdict_and_garbage_fails_loudly(self):
        auth = self.client.post("/api/auth/register/", {"email": "tp@example.com", "password": "s3cure-Passw0rd", "full_name": "T"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        user = User.objects.get(email="tp@example.com")
        user.plan = "pro_plus"
        user.save()
        ws = self.client.get("/api/workspaces/").data[0]["id"]
        task = self.client.post(f"/api/workspaces/{ws}/tasks/", {"title": "Research"}, format="json").data
        for content, status, verdict in (('{"verdict":"full","plan":"Compare three options","reason":"This is a resea', "assessed", "full"),
                                         ("", "failed", "")):
            Delegation.objects.all().delete()
            self.client.post(f"/api/workspaces/{ws}/tasks/{task['id']}/delegation/", {"action": "assess"}, format="json")
            with mock.patch("integrations.http.requests.request", FakeLLM([content])):
                engine.process()
            d = Delegation.objects.get()
            self.assertEqual((d.status, d.verdict), (status, verdict))
