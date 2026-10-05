from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from concierge.models import ConciergeProfile, Notification
from concierge.tests import FakeWorld
from insights import review
from insights.models import WeeklyReview
from insights.stats import monday
from tasks.models import Task
from timetracking.models import TimeEntry
from workspaces.models import Membership

ROME = ZoneInfo("Europe/Rome")


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model")
class InsightsTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "in@example.com", "password": "s3cure-Passw0rd", "full_name": "Ines"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="in@example.com")
        ConciergeProfile.objects.create(user=self.user, timezone="Europe/Rome")
        self.ws = self.client.post("/api/workspaces/", {"name": "Studio"}).data["id"]
        self.site = self.client.post(f"/api/workspaces/{self.ws}/projects/", {"name": "Website"}, format="json").data["id"]
        self.bob = User.objects.create_user(email="bob@example.com", password="s3cure-Passw0rd", full_name="Bob")
        Membership.objects.create(workspace_id=self.ws, user=self.bob)
        self.today = timezone.now().astimezone(ROME).date()
        statuses = self.client.get(f"/api/workspaces/{self.ws}/tag-groups/").data
        self.done = next(t["id"] for g in statuses if g["kind"] == "status" for t in g["tags"] if t["is_done"])

    def task(self, title, **extra):
        return self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": title, "assignee": self.user.id, "planned_date": str(self.today), **extra}, format="json").data

    def finish(self, task):
        self.client.patch(f"/api/workspaces/{self.ws}/tasks/{task['id']}/", {"status": self.done}, format="json")

    def test_scopes(self):
        a = self.task("Homepage", project=self.site, estimate_minutes=60, due_date=str(self.today + timedelta(days=1)))
        self.finish(a)
        TimeEntry.objects.create(workspace_id=self.ws, user=self.user, task_id=a["id"], project_id=self.site,
                                 started_at=timezone.now() - timedelta(hours=2), ended_at=timezone.now() - timedelta(minutes=30), duration_seconds=5400)
        self.task("Old thing", planned_date=str(self.today - timedelta(days=3)), due_date=str(self.today - timedelta(days=1)))
        self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": "Bob's", "assignee": self.bob.id}, format="json")

        me = self.client.get("/api/insights/?scope=me&weeks=4").data
        t = me["totals"]
        self.assertEqual((t["completed"], t["minutes"], t["overdue"], t["carried_over"], t["on_time_rate"], t["estimate_ratio"]), (1, 90, 1, 1, 100, 1.5))
        self.assertEqual(len(me["weeks"]), 4)
        self.assertEqual(me["weeks"][-1]["completed"], 1)
        self.assertEqual(me["projects"][0]["name"], "Website")
        self.assertEqual(me["people"], [])  # personal scope: no breakdown by person

        ws = self.client.get(f"/api/insights/?scope=workspace&id={self.ws}").data
        self.assertEqual({p["name"]: p["open"] for p in ws["people"]}, {"Ines": 1, "Bob": 1})
        project = self.client.get(f"/api/insights/?scope=project&id={self.site}").data
        self.assertEqual(project["totals"]["completed"], 1)
        other = self.client.post("/api/workspaces/", {"name": "Mine"}).data["id"]
        stranger = User.objects.create_user(email="s@example.com", password="s3cure-Passw0rd")
        from workspaces.models import Workspace

        foreign = Workspace.objects.create(name="Foreign")
        Membership.objects.create(workspace=foreign, user=stranger)
        self.assertEqual(self.client.get(f"/api/insights/?scope=workspace&id={foreign.id}").status_code, 404)

    def test_weekly_review_page_and_schedule(self):
        self.finish(self.task("Ship it"))
        self.task("Slipping one", planned_date=str(self.today - timedelta(days=2)))
        page = self.client.get("/api/my/review/").data
        self.assertEqual(page["text"], "")
        self.assertEqual(page["stats"]["totals"]["completed"], 1)
        self.assertEqual([x["title"] for x in page["stats"]["slipping"]], ["Slipping one"])
        # Free plan: a plain summary.
        written = self.client.post("/api/my/review/", {}, format="json").data
        self.assertIn("1 tasks done", written["text"])
        # Pro: Flo writes it.
        self.user.plan = "pro"
        self.user.save()
        world = FakeWorld(llm_replies=[{"role": "assistant", "content": "Solid week. • Next: finish Slipping one."}])
        with mock.patch("integrations.http.requests.request", world):
            written = self.client.post("/api/my/review/", {}, format="json").data
        self.assertIn("Solid week", written["text"])
        self.assertIn("Slipping one", world.llm_calls[0]["json"]["messages"][1]["content"])

        # Scheduled: Friday 16:00 local by default, once per week.
        WeeklyReview.objects.all().delete()
        friday = monday(self.today) + timedelta(days=4)
        at = datetime(friday.year, friday.month, friday.day, 16, 30, tzinfo=ROME)
        self.assertEqual(list(review.due_reviews(at - timedelta(hours=1))), [])
        due = list(review.due_reviews(at))
        self.assertEqual([(p.user, w) for p, w in due], [(self.user, monday(self.today))])
        with mock.patch("insights.review.timezone.now", return_value=at), mock.patch("integrations.http.requests.request", FakeWorld(llm_replies=[{"role": "assistant", "content": "Week done."}])):
            review.send_due()
        self.assertTrue(Notification.objects.filter(title__startswith="Your weekly review").exists())
        self.assertEqual(list(review.due_reviews(at)), [])
        settings = self.client.patch("/api/concierge/", {"review_day": "sun", "review_time": "19:00"}, format="json").data
        self.assertEqual(settings["settings"]["review_day"], "sun")
