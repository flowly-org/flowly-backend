import json
from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from concierge import jobs
from concierge.models import ConciergeProfile, Notification
from concierge.notify import local_today
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

    def raise_for_status(self):
        pass


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
        if url.endswith("/getFile"):
            return FakeResponse({"ok": True, "result": {"file_path": "photos/list.jpg", "file_size": 1234}})
        if "/file/bot" in url:
            return FakeResponse({"jpeg": "bytes"})
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
        self.assertEqual((created.assignee, created.priority.name, created.planned_date), (self.user, "High", local_today(self.user) + timedelta(days=1)))
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
        from workspaces.models import Workspace

        self.make_pro()
        Workspace.objects.filter(pk=self.ws["id"]).update(plan="enterprise")  # own AI providers are Enterprise
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

    def test_name_and_language(self):
        self.make_pro()
        res = self.client.patch("/api/concierge/", {"name": "Jarvis", "language": "it"}, format="json")
        self.assertEqual((res.data["settings"]["name"], res.data["settings"]["language"]), ("Jarvis", "it"))
        self.assertEqual(self.client.patch("/api/concierge/", {"language": "klingon"}, format="json").status_code, 400)
        world = FakeWorld(llm_replies=[{"role": "assistant", "content": "Ciao!"}])
        with mock.patch("integrations.http.requests.request", world):
            self.client.post("/api/concierge/chat/", {"message": "hi"}, format="json")
        system = world.llm_calls[0]["json"]["messages"][0]["content"]
        self.assertIn("You are Jarvis", system)
        self.assertIn("Always write in Italian", system)
        # Fixed bot texts follow the language too.
        self.link_telegram(chat_id=321)
        world = FakeWorld(updates=[{"update_id": 5, "message": {"chat": {"id": 321, "type": "private"}, "text": "/help"}}])
        with mock.patch("integrations.http.requests.request", world):
            jobs.poll_updates()
        self.assertTrue(world.sent[0]["text"].startswith("Sono Jarvis"))

    def test_settings_endpoint(self):
        res = self.client.get("/api/concierge/").data
        self.assertEqual((res["plan"], res["has_pro"], res["available"]), ("free", False, False))
        res = self.client.patch("/api/concierge/", {"timezone": "Mars/Base"}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.patch("/api/concierge/", {"timezone": "Europe/Rome", "brief_time": "07:30"}, format="json").data
        self.assertEqual(res["settings"]["brief_time"], "07:30")


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model", OPENROUTER_VISION_MODEL="test/vision")
class PhotoImportTests(APITestCase):
    PNG = (b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "p@example.com", "password": "s3cure-Passw0rd", "full_name": "P"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="p@example.com")
        self.ws = self.client.get("/api/workspaces/").data[0]["id"]
        self.url = f"/api/workspaces/{self.ws}/tasks/from-photo/"

    def upload(self, data=None, name="list.png", content_type="image/png"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(self.url, {"image": SimpleUploadedFile(name, data or self.PNG, content_type=content_type)}, format="multipart")

    def fake(self, reply):
        calls = []

        def request(method, url, json=None, data=None, headers=None, timeout=None):
            calls.append(json)
            return FakeResponse({"choices": [{"message": {"role": "assistant", "content": reply}}], "usage": {"prompt_tokens": 900, "completion_tokens": 40}})

        return calls, request

    def test_free_user_without_workspace_provider_is_told_why(self):
        res = self.upload()
        self.assertEqual(res.status_code, 402)
        self.assertIn("Pro", res.data["detail"])

    def test_pro_user_gets_bulk_text_from_the_vision_model(self):
        self.user.plan = "pro"
        self.user.save()
        calls, request = self.fake("```\n- Comprare il pane\n- [x] Chiamare Luca\n  per la cena\n```")
        with mock.patch("integrations.http.requests.request", request):
            res = self.upload()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["text"], "- Comprare il pane\n- [x] Chiamare Luca\n  per la cena")
        body = calls[0]
        self.assertEqual(body["model"], "test/vision")
        parts = body["messages"][0]["content"]
        self.assertIn("bulk-add format", parts[0]["text"])
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        from concierge.models import AIUsage

        self.assertEqual(AIUsage.objects.get(user=self.user).prompt_tokens, 900)

    def test_nothing_legible_and_bad_files(self):
        self.user.plan = "pro"
        self.user.save()
        _, request = self.fake("NONE")
        with mock.patch("integrations.http.requests.request", request):
            self.assertEqual(self.upload().status_code, 422)
        self.assertEqual(self.upload(name="x.gif", content_type="image/gif").status_code, 400)
        other = self.client.post("/api/workspaces/", {"name": "Other"}).data["id"]
        Membership.objects.filter(workspace_id=other).delete()
        from django.core.files.uploadedfile import SimpleUploadedFile

        res = self.client.post(f"/api/workspaces/{other}/tasks/from-photo/", {"image": SimpleUploadedFile("a.png", self.PNG, content_type="image/png")}, format="multipart")
        self.assertEqual(res.status_code, 404)

    @override_settings(INTEGRATIONS_ALLOW_PRIVATE_URLS=True)
    def test_workspace_provider_is_used_even_for_free_users(self):
        from workspaces.models import Workspace

        Workspace.objects.filter(pk=self.ws).update(plan="enterprise")
        Integration.objects.create(
            workspace_id=self.ws, kind="openai_compatible", name="Local", enabled=True,
            config={"base_url": "http://10.0.0.5:8000/v1", "model": "llava", "exclusive": True},
        )
        calls, request = self.fake("- Task from local model")
        with mock.patch("integrations.http.requests.request", request):
            res = self.upload()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(calls[0]["model"], "llava")



@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model", TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_MODE="polling")
class TelegramPhotoAndTimerTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "t@example.com", "password": "s3cure-Passw0rd", "full_name": "T"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="t@example.com")
        self.user.plan = "pro"
        self.user.save()
        ConciergeProfile.objects.create(user=self.user, telegram_chat_id=777, timezone="UTC")
        self.personal = self.client.get("/api/workspaces/").data[0]["id"]
        self.mensa = self.client.post("/api/workspaces/", {"name": "Mensa"}).data["id"]
        self.client.post(f"/api/workspaces/{self.mensa}/projects/", {"name": "Cucina"}, format="json")

    def photo_update(self, caption=""):
        return {"update_id": 1, "message": {"chat": {"id": 777, "type": "private"}, "from": {"username": "t"},
                "caption": caption, "photo": [{"file_id": "small"}, {"file_id": "big"}]}}

    def test_photo_becomes_tasks_in_the_named_workspace_and_undo(self):
        from concierge import bot

        listing = "- Ordinare piatti +Cucina\n  200 pezzi\n- [x] Chiamare fornitore\n- Menu della settimana @2030-01-07"
        world = FakeWorld(llm_replies=[{"role": "assistant", "content": listing}])
        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.photo_update("lista per mensa domani"))
        tasks = {t.title: t for t in Task.objects.filter(workspace_id=self.mensa)}
        self.assertEqual(set(tasks), {"Ordinare piatti", "Chiamare fornitore", "Menu della settimana"})
        tomorrow = timezone.localdate() + timedelta(days=1)
        self.assertEqual(tasks["Ordinare piatti"].planned_date, tomorrow)
        self.assertEqual(tasks["Ordinare piatti"].project.name, "Cucina")
        self.assertEqual(tasks["Ordinare piatti"].description, "200 pezzi")
        self.assertTrue(tasks["Chiamare fornitore"].status.is_done)
        self.assertEqual(str(tasks["Menu della settimana"].planned_date), "2030-01-07")
        self.assertEqual(tasks["Ordinare piatti"].assignee, self.user)
        reply = world.sent[-1]["text"]
        self.assertIn("3", reply)
        self.assertIn("Mensa", reply)
        self.assertIn("/undo", reply)
        # The vision request carried the image and the caption.
        body = world.llm_calls[0]["json"]
        self.assertIn("lista per mensa domani", body["messages"][0]["content"][0]["text"])
        self.assertTrue(body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update({"update_id": 2, "message": {"chat": {"id": 777, "type": "private"}, "text": "/undo"}})
        self.assertFalse(Task.objects.filter(workspace_id=self.mensa).exists())

    def test_photo_without_tasks_and_without_pro(self):
        from concierge import bot

        world = FakeWorld(llm_replies=[{"role": "assistant", "content": "NONE"}])
        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.photo_update())
        self.assertFalse(Task.objects.exists())
        self.user.plan = "free"
        self.user.save()
        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.photo_update())
        self.assertIn("Pro", world.sent[-1]["text"])

    def test_concierge_starts_and_stops_the_timer(self):
        from concierge import brain

        task = self.client.post(f"/api/workspaces/{self.personal}/tasks/", {"title": "Write slides"}, format="json").data
        world = FakeWorld(llm_replies=[
            tool_call("start_timer", {"task_id": task["id"]}),
            {"role": "assistant", "content": "Timer running on Write slides."},
            tool_call("stop_timer", {}, "c2"),
            {"role": "assistant", "content": "Stopped."},
        ])
        with mock.patch("integrations.http.requests.request", world):
            brain.reply(self.user, "start the timer on the slides")
            self.assertEqual(self.client.get("/api/my/timer/").data["entry"]["task"], task["id"])
            brain.reply(self.user, "stop")
        self.assertIsNone(self.client.get("/api/my/timer/").data["entry"])


    RELEASE = {"tag_name": "v0.3.0", "assets": [{"name": "Flowly-Android.apk", "size": 3_700_000},
                                                {"name": "Flowly-macOS-universal.dmg", "size": 9_500_000},
                                                {"name": "Flowly-Linux-x86_64.AppImage", "size": 84_000_000}]}

    def fake_github(self, url, **kwargs):
        if "api.github.com" in url:
            return FakeResponse(self.RELEASE)
        response = mock.Mock(status_code=200)
        response.raw.read.return_value = b"APK-BYTES"
        return response

    def fake_upload(self, url, data=None, files=None, timeout=None):
        self.uploads.append((url, data, files))
        return FakeResponse({"ok": True, "result": {"document": {"file_id": "tg-file-1"}}})

    def test_flo_sends_app_links_and_files(self):
        from concierge import bot, downloads as apps

        apps._cache["data"] = None
        self.uploads = []
        world = FakeWorld(llm_replies=[
            tool_call("app_downloads", {"platform": "android", "send_file": True}),
            {"role": "assistant", "content": "Here's the Android app."},
        ])
        with mock.patch("integrations.http.requests.request", world), \
                mock.patch("concierge.downloads.requests.get", side_effect=self.fake_github), \
                mock.patch("requests.post", side_effect=self.fake_upload):
            bot.handle_update({"update_id": 1, "message": {"chat": {"id": 777, "type": "private"}, "text": "send me the android app"}})
            tool_result = world.llm_calls[1]["json"]["messages"][-1]["content"]
            self.assertIn("releases/latest/download/Flowly-Android.apk (4 MB)", tool_result)
            self.assertIn("0.3.0", tool_result)
            self.assertEqual(len(self.uploads), 1)
            self.assertEqual(self.uploads[0][2]["document"][0], "Flowly-Android.apk")
            # The second time Telegram's file id is reused: no new upload.
            apps.send_file(777, "Flowly-Android.apk")
            self.assertEqual(len(self.uploads), 1)
            # /apps works without the model; the AppImage is too big to send, so it stays a link.
            bot.handle_update({"update_id": 2, "message": {"chat": {"id": 777, "type": "private"}, "text": "/apps linux"}})
            self.assertIn("Flowly-Linux-x86_64.AppImage (84 MB)", world.sent[-1]["text"])
            self.assertEqual(apps.sendable("linux"), [])
            self.assertEqual(apps.sendable("mac"), ["Flowly-macOS-universal.dmg"])
            self.assertIn("Add to Home Screen", apps.describe("iphone"))

    def test_app_links_on_the_web_chat_without_files(self):
        from concierge.tools import Toolbox

        from concierge import downloads as apps

        apps._cache["data"] = None
        with mock.patch("concierge.downloads.requests.get", side_effect=self.fake_github):
            box = Toolbox(self.user, None, timezone.localdate())
            text = box.tool_app_downloads("desktop", send_file=True)
        self.assertIn("Flowly-macOS-universal.dmg", text)
        self.assertNotIn("Flowly-Android.apk", text)
        self.assertIn("only be sent on Telegram", text)
        self.assertEqual(box.outbox, [])

@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model", TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_MODE="polling",
                   SLACK_CLIENT_ID="cid", SLACK_CLIENT_SECRET="csecret", SLACK_SIGNING_SECRET="sign",
                   PUBLIC_URL="https://app.example", API_PUBLIC_URL="https://api.example")
class TeamChatTests(APITestCase):
    GROUP = -100777

    def setUp(self):
        from concierge import telegram
        from workspaces.models import Workspace

        telegram._me_cache.clear()
        reg = lambda e, n: self.client.post("/api/auth/register/", {"email": e, "password": "s3cure-Passw0rd", "full_name": n}).data
        self.boss = reg("boss@example.com", "Boss")
        self.mate = reg("mate@example.com", "Mate")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.boss['access']}")
        self.ws = self.client.post("/api/workspaces/", {"name": "Mensa"}).data["id"]
        Membership.objects.create(workspace_id=self.ws, user_id=self.mate["user"]["id"])
        self.base = f"/api/workspaces/{self.ws}"
        self.workspace = Workspace.objects.get(pk=self.ws)
        self.boss_user = User.objects.get(email="boss@example.com")
        self.mate_user = User.objects.get(email="mate@example.com")
        today = local_today(self.boss_user).isoformat()
        self.client.post(f"{self.base}/tasks/", {"title": "Ordinare piatti", "assignee": self.mate["user"]["id"], "planned_date": today}, format="json")
        self.client.post(f"{self.base}/tasks/", {"title": "Segreto del capo", "visibility": "private", "assignee": self.boss["user"]["id"], "planned_date": today}, format="json")

    def enterprise(self, on=True):
        self.workspace.plan = "enterprise" if on else "standard"
        self.workspace.save()

    def group_msg(self, text, from_id=4242, **extra):
        msg = {"message_id": 10, "chat": {"id": self.GROUP, "type": "supergroup", "title": "Cucina"},
               "from": {"id": from_id, "first_name": "Gino"}, **extra}
        if text is not None:
            msg["text"] = text
        return {"update_id": 1, "message": msg}

    def link_group(self, world):
        from concierge import bot

        with mock.patch("integrations.http.requests.request", world):
            res = self.client.post(f"{self.base}/integrations/", {"kind": "telegram_group", "name": "Cucina", "config": {}}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        command = res.data["info"]["link_command"]
        self.assertTrue(command.startswith("/link@FlowlyBot "))
        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.group_msg(command))
        return Integration.objects.get(pk=res.data["id"])

    # --- plans ------------------------------------------------------------------

    def test_enterprise_gating_and_admin_plan(self):
        for kind, config in (("telegram_group", {}), ("openrouter", {"model": "x"})):
            res = self.client.post(f"{self.base}/integrations/", {"kind": kind, "name": kind, "config": config, "api_key": "k"}, format="json")
            self.assertEqual(res.status_code, 400)
            self.assertIn("Enterprise", str(res.data))
        self.assertEqual(self.client.patch(f"/api/admin/workspaces/{self.ws}/", {"plan": "enterprise"}, format="json").status_code, 403)
        self.boss_user.is_staff = True
        self.boss_user.save()
        res = self.client.patch(f"/api/admin/workspaces/{self.ws}/", {"plan": "enterprise"}, format="json")
        self.assertTrue(res.data["is_enterprise"])
        self.assertEqual(self.client.get("/api/admin/workspaces/", {"q": "mens"}).data[0]["owner"], "boss@example.com")
        self.assertTrue(self.client.get(f"/api/workspaces/{self.ws}/").data["is_enterprise"])
        with mock.patch("integrations.http.requests.request", FakeWorld()):
            res = self.client.post(f"{self.base}/integrations/", {"kind": "telegram_group", "name": "g", "config": {}}, format="json")
        self.assertEqual(res.status_code, 201)

    # --- Telegram groups --------------------------------------------------------

    def test_telegram_group_link_today_mine_and_privacy(self):
        from concierge import bot

        self.enterprise()
        world = FakeWorld()
        integration = self.link_group(world)
        self.assertEqual(integration.config["chat_id"], self.GROUP)
        self.assertNotIn("link_code", integration.config)
        self.assertIn("Linked to the Flowly workspace “Mensa”", world.sent[-1]["text"])
        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.group_msg("/today@FlowlyBot"))
            brief = world.sent[-1]["text"]
            bot.handle_update(self.group_msg("/mine"))  # not linked to Flowly
            mine_unlinked = world.sent[-1]["text"]
            ConciergeProfile.objects.create(user=self.mate_user, telegram_chat_id=4242)
            bot.handle_update(self.group_msg("/mine"))
            mine = world.sent[-1]["text"]
            bot.handle_update(self.group_msg("/today@OtherBot"))  # someone else's command: ignored
        self.assertIn("Ordinare piatti", brief)
        self.assertIn("Mate (1)", brief)
        self.assertNotIn("Segreto", brief)
        self.assertIn("Link your Flowly account", mine_unlinked)
        self.assertIn("Ordinare piatti", mine)
        self.assertEqual(world.sent[-1]["text"], mine)

    def test_ask_unlinked_reads_only_linked_acts_as_themselves(self):
        from concierge import bot

        self.enterprise()
        world = FakeWorld()
        self.link_group(world)
        world.llm_replies = [
            tool_call("list_tasks", {"everyone": True}),
            tool_call("create_task", {"title": "Hack"}, "c2"),
            {"role": "assistant", "content": "Here's the list."},
        ]
        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.group_msg("/ask what's on today?", from_id=999))
        tool_outputs = [m["content"] for m in world.llm_calls[-1]["json"]["messages"] if m["role"] == "tool"]
        self.assertIn("Ordinare piatti", tool_outputs[0])
        self.assertNotIn("Segreto", tool_outputs[0])  # private tasks never reach a group
        self.assertIn("Error", tool_outputs[1])
        self.assertFalse(Task.objects.filter(title="Hack").exists())
        tools_offered = [t["function"]["name"] for t in world.llm_calls[0]["json"]["tools"]]
        self.assertEqual(tools_offered, ["list_tasks"])

        ConciergeProfile.objects.create(user=self.mate_user, telegram_chat_id=4242)
        world.llm_replies = [tool_call("create_task", {"title": "Comprare tovaglie", "day": "today"}), {"role": "assistant", "content": "Added."}]
        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.group_msg("aggiungi comprare tovaglie", reply_to_message={"from": {"username": "FlowlyBot"}}))
        task = Task.objects.get(title="Comprare tovaglie")
        self.assertEqual((task.workspace_id, task.created_by, task.assignee), (self.ws, self.mate_user, self.mate_user))
        self.assertEqual(world.sent[-1]["text"], "Added.")
        self.assertEqual(world.sent[-1]["reply_parameters"]["message_id"], 10)

    def test_group_photo_and_undo(self):
        from concierge import bot

        self.enterprise()
        world = FakeWorld()
        self.link_group(world)
        ConciergeProfile.objects.create(user=self.mate_user, telegram_chat_id=4242)
        world.llm_replies = [{"role": "assistant", "content": "- Pelare patate\n- Lavare pentole"}]
        photo = {"photo": [{"file_id": "s"}, {"file_id": "b"}], "caption": "/tasks"}
        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.group_msg(None, **photo))
        self.assertEqual(set(Task.objects.filter(workspace_id=self.ws, created_by=self.mate_user).values_list("title", flat=True)),
                         {"Pelare patate", "Lavare pentole"})
        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.group_msg("/undo", from_id=31337))  # not the author
            self.assertTrue(Task.objects.filter(title="Pelare patate").exists())
            bot.handle_update(self.group_msg("/undo"))
        self.assertFalse(Task.objects.filter(title="Pelare patate").exists())

    def test_updates_and_brief_schedule(self):
        from concierge import teambot
        from integrations.worker import process_due

        self.enterprise()
        world = FakeWorld()
        integration = self.link_group(world)
        self.client.post(f"{self.base}/tasks/", {"title": "Nuovo menu"}, format="json")
        with mock.patch("integrations.http.requests.request", world):
            process_due()
        self.assertTrue(any("Nuovo menu" in m["text"] for m in world.sent))
        from django.utils import timezone as tz

        earlier = (tz.now() - timedelta(minutes=1)).strftime("%H:%M")
        integration.config = {**integration.config, "brief": True, "brief_time": earlier, "weekends": True, "timezone": "UTC"}
        integration.save()
        self.assertEqual([i.pk for i in teambot.due_briefs()], [integration.pk])
        self.assertEqual(list(teambot.due_briefs()), [])  # once a day
        self.enterprise(False)
        world.sent.clear()
        from concierge import bot

        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.group_msg("/today"))
        self.assertIn("Enterprise", world.sent[-1]["text"])

    # --- Slack ------------------------------------------------------------------

    def slack_post(self, path, body: bytes, content_type, sign=True):
        import hashlib
        import hmac
        import time as _t

        ts = str(int(_t.time()))
        sig = "v0=" + hmac.new(b"sign", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest() if sign else "v0=bad"
        self.client.credentials()
        return self.client.generic("POST", path, body, content_type=content_type,
                                   HTTP_X_SLACK_REQUEST_TIMESTAMP=ts, HTTP_X_SLACK_SIGNATURE=sig)

    def slack_world(self):
        world = FakeWorld(llm_replies=[{"role": "assistant", "content": "Mate has 1 task."}])
        world.slack = []

        def request(method, url, json=None, data=None, headers=None, timeout=None):
            if url.startswith("https://slack.com/api/oauth.v2.access"):
                return FakeResponse({"ok": True, "access_token": "xoxb-1", "team": {"id": "T1", "name": "Mensa HQ"}, "bot_user_id": "UBOT"})
            if url.startswith("https://slack.com/api/users.info"):
                return FakeResponse({"ok": True, "user": {"profile": {"email": "MATE@example.com", "real_name": "Mate"}}})
            if url.startswith("https://slack.com/api/") or url.startswith("https://hooks.slack.com/commands"):
                world.slack.append((url.rsplit("/", 1)[-1], json, headers))
                return FakeResponse({"ok": True, "channels": [{"id": "C123456", "name": "cucina"}]})
            return world(method, url, json=json, data=data, headers=headers, timeout=timeout)

        return world, request

    def test_slack_install_events_and_commands(self):
        import json as _json
        from urllib.parse import parse_qs, urlparse

        from concierge import teamchat_slack
        from concierge.models import BotJob

        self.assertEqual(self.client.post("/api/integrations/slack/install/", {"workspace": self.ws}, format="json").status_code, 402)
        self.enterprise()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.mate['access']}")
        self.assertEqual(self.client.post("/api/integrations/slack/install/", {"workspace": self.ws}, format="json").status_code, 403)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.boss['access']}")
        url = self.client.post("/api/integrations/slack/install/", {"workspace": self.ws}, format="json").data["url"]
        q = parse_qs(urlparse(url).query)
        self.assertIn("users:read.email", q["scope"][0])
        world, request = self.slack_world()
        with mock.patch("integrations.http.requests.request", request):
            res = self.client.get("/api/integrations/slack/oauth/callback/", {"code": "c", "state": q["state"][0]})
        self.assertEqual(res["Location"], f"https://app.example/w/{self.ws}/integrations?slack=connected")
        integration = Integration.objects.get(kind="slack_bot")
        self.assertEqual(integration.config["team_id"], "T1")
        self.assertTrue(integration.credentials["bot_token"].startswith("enc:"))

        self.assertEqual(self.slack_post("/api/integrations/slack/events/", b"{}", "application/json", sign=False).status_code, 401)
        res = self.slack_post("/api/integrations/slack/events/", _json.dumps({"type": "url_verification", "challenge": "abc"}).encode(), "application/json")
        self.assertEqual(res.data, {"challenge": "abc"})
        event = {"type": "event_callback", "team_id": "T1", "event": {"type": "app_mention", "user": "U1", "text": "<@UBOT> who has what?",
                                                                       "channel": "C1", "ts": "1.2"}}
        self.assertEqual(self.slack_post("/api/integrations/slack/events/", _json.dumps(event).encode(), "application/json").status_code, 200)
        form = "team_id=T1&user_id=U1&channel_id=C1&text=today&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2Fx"
        res = self.slack_post("/api/integrations/slack/commands/", form.encode(), "application/x-www-form-urlencoded")
        self.assertIn("On it", res.data["text"])
        self.assertEqual(BotJob.objects.count(), 2)
        with mock.patch("integrations.http.requests.request", request):
            teamchat_slack.process()
        self.assertEqual(set(BotJob.objects.values_list("status", flat=True)), {"done"})
        posted = {name: body for name, body, _ in world.slack}
        self.assertEqual(posted["chat.postMessage"]["text"], "Mate has 1 task.")
        self.assertEqual(posted["chat.postMessage"]["thread_ts"], "1.2")
        self.assertIn("Ordinare piatti", posted["x"]["text"])
        self.assertIn("Mate (Flowly user Mate", world.llm_calls[0]["json"]["messages"][0]["content"])  # recognised by email

        # Channel picker
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.boss['access']}")
        with mock.patch("integrations.http.requests.request", request):
            channels = self.client.get(f"/api/workspaces/{self.ws}/integrations/{integration.id}/slack-channels/").data
        self.assertEqual(channels, [{"id": "C123456", "name": "cucina"}])


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model")
class PersonalityMemoryTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "pm@example.com", "password": "s3cure-Passw0rd", "full_name": "Pia"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="pm@example.com")
        self.user.plan = "pro"
        self.user.save()
        self.ws = self.client.get("/api/workspaces/").data[0]["id"]

    def test_settings_prompt_and_tools(self):
        from concierge import brain

        data = self.client.get("/api/concierge/").data
        self.assertEqual(data["settings"]["personality"], "friendly")
        self.assertEqual([p["key"] for p in data["personalities"]], ["friendly", "coach", "butler", "minimal", "buddy", "calm"])
        res = self.client.patch("/api/concierge/", {"personality": "coach", "memory": "- Deep work before 11\n- Calls Studio “work”", "learn": False}, format="json")
        self.assertEqual((res.data["settings"]["personality"], res.data["settings"]["learn"]), ("coach", False))
        self.assertEqual(self.client.patch("/api/concierge/", {"personality": "pirate"}, format="json").status_code, 400)
        # Habits from activity: enough tasks in one workspace.
        for i in range(6):
            self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": f"T{i}"}, format="json")
        prompt = brain.system_prompt(self.user, brain.local_now(self.user))
        self.assertIn("productivity coach", prompt)
        self.assertIn("Deep work before 11", prompt)
        self.assertIn("adds most tasks to", prompt)

        world = FakeWorld(llm_replies=[tool_call("remember", {"note": "Deep work before 11"}),
                                       tool_call("remember", {"note": "Prefers short briefs"}, "c2"),
                                       tool_call("forget", {"words": "work”"}, "c3"), {"role": "assistant", "content": "Ok."}])
        with mock.patch("integrations.http.requests.request", world):
            brain.reply(self.user, "remember I like short briefs; and I don't call Studio work anymore")
        self.user.concierge.refresh_from_db()
        self.assertEqual(self.user.concierge.memory, "- Deep work before 11\n- Prefers short briefs")

    def test_daily_reflection(self):
        from concierge.models import ConciergeMessage, ConciergeProfile
        from concierge.personality import due_reflections, reflect

        profile = ConciergeProfile.objects.create(user=self.user, memory="- Likes mornings")
        for text in ("I'm off on Fridays", "please keep the brief to 3 lines", "Marta is my business partner"):
            ConciergeMessage.objects.create(user=self.user, role="user", content=text)
        self.assertEqual([p.pk for p in due_reflections()], [profile.pk])
        world = FakeWorld(llm_replies=[{"role": "assistant", "content": '{"memory": ["Likes mornings", "Off on Fridays", "Wants 3-line briefs", "Marta is their business partner"]}'}])
        with mock.patch("integrations.http.requests.request", world):
            self.assertTrue(reflect(profile))
        profile.refresh_from_db()
        self.assertEqual(profile.memory.count("\n- "), 3)
        self.assertIn("Off on Fridays", profile.memory)
        self.assertIn("Never store sensitive data", world.llm_calls[0]["json"]["messages"][0]["content"])
        self.assertEqual(due_reflections(), [])  # done for today
        profile.learn = False
        profile.memory_updated_at = None
        profile.save()
        self.assertEqual(due_reflections(), [])  # learning switched off


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model", TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_MODE="polling")
class VoiceMessageTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "v@example.com", "password": "s3cure-Passw0rd", "full_name": "V"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="v@example.com")
        self.user.plan = "pro"
        self.user.save()
        ConciergeProfile.objects.create(user=self.user, telegram_chat_id=555, timezone="UTC")

    def voice(self, duration=12):
        return {"update_id": 9, "message": {"chat": {"id": 555, "type": "private"}, "from": {"username": "v"},
                "voice": {"file_id": "vf1", "duration": duration, "mime_type": "audio/ogg"}}}

    def test_dictated_list_becomes_tasks(self):
        from concierge import bot

        world = FakeWorld(llm_replies=[
            {"role": "assistant", "content": "Buy milk, call Anna tomorrow, and book the train for Friday."},
            tool_call("add_tasks", {"lines": "Buy milk\nCall Anna @tomorrow\nBook the train @fri"}),
            {"role": "assistant", "content": "Added 3 tasks."},
        ])
        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.voice())
        audio_call = world.llm_calls[0]["json"]
        self.assertEqual(audio_call["model"], "~google/gemini-flash-latest")
        part = audio_call["messages"][0]["content"][1]
        self.assertEqual((part["type"], part["input_audio"]["format"]), ("input_audio", "ogg"))
        users = [m["content"] for m in world.llm_calls[1]["json"]["messages"] if m["role"] == "user"]
        self.assertIn("(voice message) Buy milk", users[-1])
        titles = set(Task.objects.values_list("title", flat=True))
        self.assertEqual(titles, {"Buy milk", "Call Anna", "Book the train"})
        reply = world.sent[-1]["text"]
        self.assertTrue(reply.startswith("🎙 “Buy milk, call Anna tomorrow"))
        self.assertIn("Added 3 tasks.", reply)

    def test_long_silent_and_free(self):
        from concierge import bot

        world = FakeWorld(llm_replies=[{"role": "assistant", "content": "NONE"}])
        with mock.patch("integrations.http.requests.request", world):
            bot.handle_update(self.voice(duration=900))
            self.assertIn("under 5 minutes", world.sent[-1]["text"])
            bot.handle_update(self.voice())
            self.assertIn("make out any words", world.sent[-1]["text"])
            self.user.plan = "free"
            self.user.save()
            bot.handle_update(self.voice())
        self.assertIn("Pro", world.sent[-1]["text"])
