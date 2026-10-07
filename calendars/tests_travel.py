from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.focus import clean
from accounts.models import User
from calendars import travel
from calendars.blocks import free_slots
from calendars.models import Calendar, CalendarAccount, CalendarEvent
from concierge.models import ConciergeProfile, Notification
from tasks.load import busy_minutes

ROME = ZoneInfo("Europe/Rome")
HOME = "Via Donato Creti 32, 40128 Bologna, Italy"


class TravelTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "tr@example.com", "password": "s3cure-Passw0rd", "full_name": "T"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="tr@example.com")
        self.user.home_location = HOME
        self.user.save()
        ConciergeProfile.objects.create(user=self.user, timezone="Europe/Rome")
        self.day = timezone.now().astimezone(ROME).date() + timedelta(days=1)
        account = CalendarAccount.objects.create(user=self.user, provider="ics")
        self.cal = Calendar.objects.create(account=account, external_id="p", name="Main")

    def at(self, h, m=0, day=None):
        d = day or self.day
        return datetime(d.year, d.month, d.day, h, m, tzinfo=ROME)

    def event(self, title, start, end, location="", all_day=False):
        return CalendarEvent.objects.create(
            calendar=self.cal, user=self.user, external_id=f"{title}{start}", title=title, start=start, end=end,
            start_date=start.astimezone(ROME).date(), end_date=end.astimezone(ROME).date(), location=location, all_day=all_day,
        )

    def test_kind_of_place(self):
        town = self.event("Dentist", self.at(10), self.at(11), "Via Rizzoli 1, 40125 Bologna BO, Italy")
        away = self.event("Club", self.at(18), self.at(20), "Via Galileo Galilei, 8, 40051 Altedo BO, Italy")
        online = self.event("Call", self.at(12), self.at(13), "https://zoom.us/j/1")
        nearby = self.event("Retro", self.at(14), self.at(15), "Room 3")
        self.assertEqual([travel.kind(e, HOME) for e in (town, away, online, nearby)], ["in_town", "away", "", ""])
        self.assertEqual([travel.minutes(e, self.user) for e in (town, away, online)], [45, 90, 0])
        self.assertEqual(travel.leave_at(town, self.user), self.at(9, 15))

    def test_travel_counts_as_busy(self):
        self.event("Dentist", self.at(10), self.at(11), "Via Rizzoli 1, Bologna, Italy")
        self.assertEqual(busy_minutes(self.user, self.day, ROME), 60 + 2 * 45)
        slots = [(s.astimezone(ROME).strftime("%H:%M"), e.astimezone(ROME).strftime("%H:%M")) for s, e in free_slots(self.user, self.day, ROME, from_now=False)]
        self.assertEqual(slots, [("09:00", "09:15"), ("11:45", "18:00")])
        self.user.focus = {"transfer_minutes": 30}
        self.user.save()
        self.assertEqual(busy_minutes(self.user, self.day, ROME), 60 + 2 * 30)

    def test_event_api_shows_leave_time(self):
        e = self.event("Dentist", self.at(10), self.at(11), "Via Rizzoli 1, Bologna, Italy")
        res = self.client.get(f"/api/calendar/events/?from={self.day}")
        self.assertEqual(res.status_code, 200, res.data)
        data = next(x for x in res.data if x["id"] == e.id)
        self.assertEqual((data["travel"], data["travel_minutes"]), ("in_town", 45))
        self.assertEqual(datetime.fromisoformat(str(data["leave_at"]).replace("Z", "+00:00")), self.at(9, 15))

    def test_briefing_goes_out_once_before_leaving(self):
        now = timezone.now()
        soon = self.event("Dentist", now + timedelta(minutes=55), now + timedelta(minutes=115), "Via Rizzoli 1, Bologna, Italy")
        later = self.event("Lunch", now + timedelta(hours=3), now + timedelta(hours=4), "Via Indipendenza 2, Bologna, Italy")
        online = self.event("Call", now + timedelta(minutes=30), now + timedelta(minutes=60), "https://meet.google.com/x")
        self.assertEqual(travel.send_briefings(self.user, now), 0)  # off by default
        self.user.focus = {"appointment_briefs": "in_person"}
        self.user.save()
        self.assertEqual(travel.due_briefings(self.user, now), [soon])
        self.assertEqual(travel.send_briefings(self.user, now), 1)
        self.assertEqual(travel.send_briefings(self.user, now), 0)
        n = Notification.objects.get(user=self.user, kind="reminder")
        self.assertIn("Dentist at", n.title)
        self.assertIn("Leave by", n.body)
        self.assertIn("https://www.google.com/maps/search/?api=1&query=Via+Rizzoli+1", n.body)
        self.assertEqual(n.data, {"event": soon.pk})
        # "all" also covers online events (no travel: briefed `lead` minutes before they start).
        self.user.focus = {"appointment_briefs": "all", "appointment_lead_minutes": 30}
        self.user.save()
        self.assertEqual(set(travel.due_briefings(self.user, now)), {soon, online})
        self.assertNotIn(later, travel.due_briefings(self.user, now))

    def test_settings_validation(self):
        self.assertEqual(clean({"appointment_briefs": "all", "transfer_minutes": 30}), {"appointment_briefs": "all", "transfer_minutes": 30})
        with self.assertRaises(ValueError):
            clean({"appointment_briefs": "sometimes"})
        with self.assertRaises(ValueError):
            clean({"transfer_minutes": 500})
