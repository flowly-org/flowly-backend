from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from calendars.models import Calendar, CalendarAccount, CalendarEvent, TimeBlock
from concierge.models import ConciergeProfile, Notification

ROME = ZoneInfo("Europe/Rome")


class TimeBlockTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "tb@example.com", "password": "s3cure-Passw0rd", "full_name": "T"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="tb@example.com")
        ConciergeProfile.objects.create(user=self.user, timezone="Europe/Rome")
        self.ws = self.client.get("/api/workspaces/").data[0]["id"]
        self.day = timezone.now().astimezone(ROME).date() + timedelta(days=1)
        account = CalendarAccount.objects.create(user=self.user, email="x@gmail.com")
        cal = Calendar.objects.create(account=account, external_id="p", name="Main")
        at = lambda h, m=0: datetime(self.day.year, self.day.month, self.day.day, h, m, tzinfo=ROME)  # noqa: E731
        self.at = at
        CalendarEvent.objects.create(calendar=cal, user=self.user, external_id="e1", title="Standup", start=at(9), end=at(9, 30),
                                     start_date=self.day, end_date=self.day)
        CalendarEvent.objects.create(calendar=cal, user=self.user, external_id="e2", title="Client call", start=at(10, 30), end=at(12),
                                     start_date=self.day, end_date=self.day)

    def task(self, title, minutes=None, **extra):
        return self.client.post(f"/api/workspaces/{self.ws}/tasks/", {"title": title, "planned_date": str(self.day), "assignee": self.user.id,
                                                                      "estimate_minutes": minutes, **extra}, format="json").data

    def test_free_slots_and_auto_plan(self):
        report = self.task("Write the report", 90)
        mail = self.task("Answer mail", 20)
        self.task("Call the bank")  # no estimate → 30 min
        day = self.client.get(f"/api/my/blocks/?date={self.day}").data
        free = [(datetime.fromisoformat(str(f["start"])).astimezone(ROME).strftime("%H:%M"), datetime.fromisoformat(str(f["end"])).astimezone(ROME).strftime("%H:%M")) for f in day["free"]]
        self.assertEqual(free, [("09:30", "10:30"), ("12:00", "18:00")])
        proposal = self.client.post("/api/my/blocks/plan/", {"date": str(self.day)}, format="json").data
        placed = {p["title"]: datetime.fromisoformat(str(p["start"])).astimezone(ROME).strftime("%H:%M") for p in proposal["placed"]}
        # 90 min doesn't fit before the call: it goes after; the short ones fill the gap.
        self.assertEqual(placed["Write the report"], "12:00")
        self.assertEqual(placed["Answer mail"], "09:30")
        self.assertEqual(TimeBlock.objects.count(), 0)
        self.client.post("/api/my/blocks/plan/", {"date": str(self.day), "apply": True}, format="json")
        self.assertEqual(TimeBlock.objects.count(), 3)
        again = self.client.post("/api/my/blocks/plan/", {"date": str(self.day)}, format="json").data
        self.assertEqual(again["placed"], [])  # already blocked

    def test_create_move_delete_feed_and_reminder(self):
        from concierge import jobs

        report = self.task("Write the report", 60)
        res = self.client.post("/api/my/blocks/", {"task": report["id"], "start": f"{self.day}T14:00"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(datetime.fromisoformat(str(res.data["end"])).astimezone(ROME).hour, 15)  # from the estimate
        moved = self.client.patch(f"/api/my/blocks/{res.data['id']}/", {"start": self.at(16).isoformat()}, format="json").data
        self.assertEqual(datetime.fromisoformat(str(moved["end"])).astimezone(ROME).hour, 17)  # same length
        self.assertEqual(self.client.post("/api/my/blocks/", {"title": "", "start": f"{self.day}T14:00"}, format="json").status_code, 400)

        url = self.client.post("/api/calendar/feed/").data["url"]
        body = self.client.get("/api/ical/" + url.rsplit("/", 1)[1]).content.decode()
        self.assertIn("SUMMARY:▶ Write the report", body)

        TimeBlock.objects.update(start=timezone.now() - timedelta(minutes=1), end=timezone.now() + timedelta(minutes=59))
        jobs.send_block_starts()
        jobs.send_block_starts()
        self.assertEqual(Notification.objects.filter(title="▶ Time for: Write the report").count(), 1)
        self.assertEqual(self.client.delete(f"/api/my/blocks/{res.data['id']}/").status_code, 204)

    def test_flo_block_and_plan(self):
        from concierge.tools import Toolbox

        report = self.task("Write the report", 45)
        box = Toolbox(self.user, None, self.day)
        out = box.run("block_time", f'{{"task_id": {report["id"]}, "start": "{self.day}T15:00"}}')
        self.assertIn("15:00–15:45", out)
        self.task("Pay invoices", 30)
        proposal = box.run("plan_day", f'{{"day": "{self.day}"}}')
        self.assertIn("Proposed", proposal)
        self.assertIn("Pay invoices", proposal)
        self.assertNotIn("Write the report", proposal)  # already blocked

    def test_working_hours_setting(self):
        res = self.client.patch("/api/auth/me/", {"focus": {"day_start": "08:00", "day_end": "16:00"}}, format="json")
        self.assertEqual(res.data["focus"]["day_end"], "16:00")
        self.assertEqual(self.client.patch("/api/auth/me/", {"focus": {"day_start": "18:00", "day_end": "08:00"}}, format="json").status_code, 400)
        self.assertEqual(self.client.patch("/api/auth/me/", {"focus": {"day_start": "8am"}}, format="json").status_code, 400)
