from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from concierge import jobs
from concierge.models import ConciergeMessage, ConciergeProfile, Notification
from tasks.models import Task
from workspaces.models import Membership

ALL_DAY = {"day_start": "00:00", "day_end": "23:59"}


class ForwardNewTasksTests(APITestCase):
    def setUp(self):
        self.me = User.objects.create_user(email="me@example.com", password="s3cure-Passw0rd", plan="pro", focus=ALL_DAY)
        self.profile = ConciergeProfile.objects.create(user=self.me, timezone="Europe/Rome", telegram_chat_id=4242)
        self.bob = User.objects.create_user(email="bob@example.com", password="s3cure-Passw0rd", full_name="Bob")
        self.client.force_authenticate(self.me)
        self.ws = self.client.post("/api/workspaces/", {"name": "Studio"}).data["id"]
        Membership.objects.create(workspace_id=self.ws, user=self.bob)
        self.site = self.client.post(f"/api/workspaces/{self.ws}/projects/", {"name": "Site"}).data["id"]
        self.today = timezone.now().astimezone(timezone.get_current_timezone()).date()
        from concierge.notify import local_today

        self.today = local_today(self.me)

    def add(self, who, title, **extra):
        self.client.force_authenticate(who)
        res = self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": title, "assignee": self.me.id, "project": self.site,
                                                                      "planned_date": str(self.today), **extra}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def kinds(self):
        return list(Notification.objects.filter(user=self.me).order_by("id").values_list("kind", "telegram"))

    def test_what_gets_forwarded(self):
        self.add(self.me, "My own task")  # you know about it
        self.add(self.bob, "Review the homepage")  # a teammate, for today
        self.add(self.bob, "Later thing", planned_date=str(self.today + timedelta(days=3)))  # not today: a plain assignment
        self.add(self.bob, "Due today, unplanned", planned_date=None, due_date=str(self.today))
        self.assertEqual(self.kinds(), [("new_task", "pending"), ("assigned", "pending"), ("new_task", "pending")])
        n = Notification.objects.filter(kind="new_task").first()
        self.assertEqual(n.body, "Bob added it (Studio / Site).")

    def test_quiet_hours_and_opt_out(self):
        self.me.focus = {"day_start": "00:00", "day_end": "00:01"}
        self.me.save()
        with mock.patch("concierge.notify.working_now", return_value=False):
            self.add(self.bob, "At night")
        self.profile.forward_new_tasks = False
        self.profile.save()
        self.add(self.bob, "Opted out")
        self.assertEqual(self.kinds(), [("new_task", "none"), ("new_task", "none")])  # still in the bell, not on Telegram
        # The website switch round-trips.
        self.client.force_authenticate(self.me)
        self.assertEqual(self.client.patch("/api/concierge/", {"forward_new_tasks": True}, format="json").status_code, 200)
        self.assertIs(self.client.get("/api/concierge/").data["settings"]["forward_new_tasks"], True)
        self.client.patch("/api/concierge/", {"forward_new_tasks": False}, format="json")
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.forward_new_tasks)

    def test_burst_becomes_one_message_after_it_settles(self):
        for title in ("Fix checkout", "Answer ACME", "Update the FAQ"):
            self.add(self.bob, title)
        done = self.add(self.bob, "Already handled")
        Task.objects.filter(pk=done["id"]).update(status=Task.objects.get(pk=done["id"]).status.group.tags.get(is_done=True))
        with mock.patch("concierge.jobs.telegram.send_message") as send:
            jobs.forward_new_tasks()  # too soon: the burst may still be arriving
            send.assert_not_called()
            jobs.forward_new_tasks(now=timezone.now() + timedelta(minutes=1))
        send.assert_called_once()
        chat, text = send.call_args.args
        self.assertEqual(chat, 4242)
        self.assertTrue(text.startswith("3 new tasks for today:"), text)
        self.assertIn("Fix checkout (Studio / Site, from Bob)", text)
        self.assertNotIn("Already handled", text)
        self.assertEqual(set(Notification.objects.filter(kind="new_task").values_list("telegram", flat=True)), {"sent"})
        # Part of Flo's conversation, so a reply has context.
        self.assertTrue(ConciergeMessage.objects.filter(user=self.me, channel="telegram", content=text).exists())
        # The regular alert sender leaves these alone.
        with mock.patch("concierge.jobs.telegram.send_message") as send:
            Notification.objects.filter(kind="new_task").update(telegram="pending")
            jobs.send_alerts()
        self.assertFalse(any("new tasks" in str(c) for c in send.call_args_list))

    def test_integrations_count_as_someone_else(self):
        from concierge import notify

        task = Task.objects.create(workspace_id=self.ws, title="Bug #42", assignee=self.me, planned_date=self.today,
                                   status=Task.objects.model._meta.get_field("status").related_model.objects.filter(workspace_id=self.ws, group__kind="status").first())
        notify.task_changed(None, task, source="GitHub")
        n = Notification.objects.get(kind="new_task")
        self.assertEqual((n.body, n.data["who"]), ("GitHub added it (Studio).", "GitHub"))
