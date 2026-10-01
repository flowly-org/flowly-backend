import json
from datetime import time, timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from concierge import jobs
from concierge.models import ConciergeProfile, Notification
from integrations.models import Integration
from tasks.models import Task
from workspaces.models import Membership


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code, self._data = status, data
        self.text = json.dumps(data)
        self.content = self.text.encode()

    def json(self):
        return self._data


class FakeWorld:
    """Fakes both the LLM (/chat/completions) and Telegram (api.telegram.org) over requests.request."""

    def __init__(self, llm_replies=(), updates=()):
        self.llm_replies = list(llm_replies)
        self.updates = list(updates)
        self.sent = []
        self.llm_calls = []

    def __call__(self, method, url, json=None, data=None, headers=None, timeout=None):
        if "/chat/completions" in url:
            self.llm_calls.append({"url": url, "json": json, "headers": headers})
            message = self.llm_replies.pop(0)
            return FakeResponse({"choices": [{"message": message}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
        if url.endswith("/getMe"):
            return FakeResponse({"ok": True, "result": {"username": "FlowlyBot"}})
        if url.endswith("/getUpdates"):
            updates, self.updates = self.updates, []
            return FakeResponse({"ok": True, "result": updates})
        if url.endswith("/sendMessage"):
            self.sent.append(json)
            return FakeResponse({"ok": True, "result": {}})
        return FakeResponse({"ok": True, "result": True})


def tool_call(name, args, call_id="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model", TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_MODE="polling")
class ConciergeTests(APITestCase):
    def setUp(self):
        from concierge import telegram

        telegram._me_cache.clear()
        self.alice = self.register("alice@example.com", "Alice")
        self.bob = self.register("bob@example.com", "Bob")
        self.as_user(self.alice)
        self.ws = self.client.post("/api/workspaces/", {"name": "Team"}).data
        Membership.objects.create(workspace_id=self.ws["id"], user_id=self.bob["user"]["id"])
        self.base = f"/api/workspaces/{self.ws['id']}"
        groups = self.client.get(f"{self.base}/tag-groups/").data
        self.prio = {t["name"]: t["id"] for g in groups if g["kind"] == "priority" for t in g["tags"]}
        self.user = User.objects.get(email="alice@example.com")
        self.today = timezone.localdate().isoformat()

    def register(self, email, name):
        return self.client.post("/api/auth/register/", {"email": email, "password": "s3cure-Passw0rd", "full_name": name}).data

    def as_user(self, auth):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")

    def make_pro(self, user=None):
        user = user or self.user
        user.plan = "pro"
        user.save()

    def link_telegram(self, chat_id=555):
        profile, _ = ConciergeProfile.objects.get_or_create(user=self.user)
        profile.telegram_chat_id = chat_id
        profile.timezone = "UTC"
        profile.save()
        return profile

    # --- notifications ------------------------------------------------------

    def test_assignment_urgent_and_comment_notifications(self):
        self.as_user(self.bob)
        self.client.post(f"{self.base}/tasks/", {"title": "Normal", "assignee": self.alice["user"]["id"]}, format="json")
        task = self.client.post(
            f"{self.base}/tasks/",
            {"title": "Server down", "assignee": self.alice["user"]["id"], "priority": self.prio["High"], "planned_date": self.today},
            format="json",
        ).data
        self.client.post(f"{self.base}/tasks/{task['id']}/comments/", {"body": "Customers are calling"}, format="json")
        # Own changes don't notify.
        self.as_user(self.alice)
        self.client.post(f"{self.base}/tasks/", {"title": "Mine", "assignee": self.alice["user"]["id"]}, format="json")

        res = self.client.get("/api/notifications/").data
        self.assertEqual(res["unread"], 3)
        kinds = [n["kind"] for n in res["results"]]
        self.assertEqual(sorted(kinds), ["assigned", "comment", "urgent"])
        urgent = next(n for n in res["results"] if n["kind"] == "urgent")
        self.assertEqual(urgent["title"], "Urgent today: Server down")
        self.assertEqual(self.client.post("/api/notifications/", {"all": True}, format="json").data["unread"], 0)

    def test_alerts_go_to_telegram_only_for_linked_pro_users(self):
        self.link_telegram()
        self.as_user(self.bob)
        self.client.post(f"{self.base}/tasks/", {"title": "Free plan", "assignee": self.alice["user"]["id"]}, format="json")
        self.assertEqual(Notification.objects.get().telegram, "none")
        self.make_pro()
        self.client.post(f"{self.base}/tasks/", {"title": "Pro plan", "assignee": self.alice["user"]["id"]}, format="json")
        world = FakeWorld()
        with mock.patch("integrations.http.requests.request", world):
            jobs.send_alerts()
        self.assertEqual([m["text"] for m in world.sent], ["📌 Bob assigned you “Pro plan”\nTeam."])
        self.assertEqual(Notification.objects.filter(telegram="sent").count(), 1)

    # --- telegram linking & chat ---------------------------------------------

    def test_link_and_chat_with_tools(self):
        self.make_pro()
        world = FakeWorld()
        with mock.patch("integrations.http.requests.request", world):
            url = self.client.post("/api/concierge/telegram/link/").data["url"]
        self.assertTrue(url.startswith("https://t.me/FlowlyBot?start="))
        code = url.split("start=")[1]

        Task.objects.create(workspace_id=self.ws["id"], title="Write report", assignee=self.user, planned_date=timezone.localdate(),
                            status=Task.objects.model._meta.get_field("status").related_model.objects.get(workspace_id=self.ws["id"], name="To do"))
        world = FakeWorld(
            llm_replies=[
                tool_call("list_tasks", {"day": "today"}),
                tool_call("create_task", {"title": "Call accountant", "workspace": "team", "day": "tomorrow", "priority": "High"}, "c2"),
                {"role": "assistant", "content": "You have “Write report” today. I added “Call accountant” for tomorrow."},
            ],
            updates=[
                {"update_id": 10, "message": {"chat": {"id": 777, "type": "private"}, "from": {"username": "alice_tg"}, "text": f"/start {code}"}},
                {"update_id": 11, "message": {"chat": {"id": 777, "type": "private"}, "text": "what's up today? also call the accountant tomorrow"}},
            ],
        )
        with mock.patch("integrations.http.requests.request", world):
            jobs.poll_updates()
        profile = ConciergeProfile.objects.get(user=self.user)
        self.assertEqual((profile.telegram_chat_id, profile.telegram_username), (777, "alice_tg"))
        self.assertIn("Connected to Flowly as Alice", world.sent[0]["text"])
        self.assertIn("I added “Call accountant”", world.sent[-1]["text"])
        # The tool results reached the model, and the task really exists.
        tool_msgs = [m for m in world.llm_calls[-1]["json"]["messages"] if m["role"] == "tool"]
        self.assertIn("Write report", tool_msgs[0]["content"])
        created = Task.objects.get(title="Call accountant")
        self.assertEqual((created.assignee, created.priority.name, created.planned_date), (self.user, "High", timezone.localdate() + timedelta(days=1)))
        self.assertEqual(world.llm_calls[0]["json"]["model"], "test/model")
        self.assertEqual(world.llm_calls[0]["headers"]["Authorization"], "Bearer or-key")
        # Offset advanced: nothing is processed twice.
        with mock.patch("integrations.http.requests.request", FakeWorld()):
            jobs.poll_updates()
        self.assertEqual(Task.objects.filter(title="Call accountant").count(), 1)

    def test_free_users_are_told_it_is_a_pro_feature(self):
        self.link_telegram(chat_id=900)
        world = FakeWorld(updates=[{"update_id": 1, "message": {"chat": {"id": 900, "type": "private"}, "text": "hi"}}])
        with mock.patch("integrations.http.requests.request", world):
            jobs.poll_updates()
        self.assertIn("Pro feature", world.sent[0]["text"])
        self.assertEqual(world.llm_calls, [])

    def test_morning_brief_is_sent_once(self):
        self.make_pro()
        profile = self.link_telegram()
        profile.brief_time = (timezone.now() - timedelta(minutes=5)).time()
        profile.save()
        world = FakeWorld(llm_replies=[{"role": "assistant", "content": "Good morning! Nothing urgent today."}])
        jobs._last_brief_check = 0
        with mock.patch("integrations.http.requests.request", world):
            jobs.send_briefs()
            jobs._last_brief_check = 0
            jobs.send_briefs()
        self.assertEqual([m["text"] for m in world.sent], ["Good morning! Nothing urgent today."])
        self.assertTrue(Notification.objects.filter(user=self.user, kind="brief").exists())

    # --- AI providers ---------------------------------------------------------

    @override_settings(INTEGRATIONS_ALLOW_PRIVATE_URLS=True)
    def test_workspace_provider_exclusive_data_and_encrypted_key(self):
        self.make_pro()
        res = self.client.post(
            f"{self.base}/integrations/",
            {"kind": "openai_compatible", "name": "Local LLM", "api_key": "sk-local-secret",
             "config": {"base_url": "http://10.1.2.3:8000/v1/", "model": "llama3"}},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertNotIn("api_key", res.data)
        self.assertEqual(res.data["info"]["api_key"], "…cret")
        self.assertTrue(res.data["config"]["exclusive"])
        stored = Integration.objects.get(pk=res.data["id"]).credentials["api_key"]
        self.assertTrue(stored.startswith("enc:") and "sk-local-secret" not in stored)

        Task.objects.create(workspace_id=self.ws["id"], title="Secret plan", assignee=self.user, planned_date=timezone.localdate(),
                            status=Task.objects.model._meta.get_field("status").related_model.objects.get(workspace_id=self.ws["id"], name="To do"))
        # On the platform provider the exclusive workspace's tasks stay hidden…
        world = FakeWorld(llm_replies=[tool_call("list_tasks", {}), {"role": "assistant", "content": "ok"}])
        with mock.patch("integrations.http.requests.request", world):
            self.client.post("/api/concierge/chat/", {"message": "my day?"}, format="json")
        tool_out = [m for m in world.llm_calls[-1]["json"]["messages"] if m["role"] == "tool"][0]["content"]
        self.assertNotIn("Secret plan", tool_out)
        self.assertIn("hidden", tool_out)
        # …while the workspace's own endpoint sees them.
        self.client.patch("/api/concierge/", {"provider": res.data["id"]}, format="json")
        world = FakeWorld(llm_replies=[tool_call("list_tasks", {}), {"role": "assistant", "content": "ok"}])
        with mock.patch("integrations.http.requests.request", world):
            reply = self.client.post("/api/concierge/chat/", {"message": "my day?"}, format="json").data["reply"]
        self.assertEqual(reply, "ok")
        self.assertTrue(world.llm_calls[0]["url"].startswith("http://10.1.2.3:8000/v1/chat/completions"))
        self.assertEqual(world.llm_calls[0]["headers"]["Authorization"], "Bearer sk-local-secret")
        tool_out = [m for m in world.llm_calls[-1]["json"]["messages"] if m["role"] == "tool"][0]["content"]
        self.assertIn("Secret plan", tool_out)

    def test_settings_endpoint(self):
        res = self.client.get("/api/concierge/").data
        self.assertEqual((res["plan"], res["has_pro"], res["available"]), ("free", False, False))
        res = self.client.patch("/api/concierge/", {"timezone": "Mars/Base"}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.patch("/api/concierge/", {"timezone": "Europe/Rome", "brief_time": "07:30"}, format="json").data
        self.assertEqual(res["settings"]["brief_time"], "07:30")
