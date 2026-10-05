from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from calendars import ics
from calendars.models import CalendarAccount, CalendarEvent

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:standup-1
DTSTART;TZID=Europe/Rome:20261005T093000
DTEND;TZID=Europe/Rome:20261005T094500
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:trip-1
DTSTART;VALUE=DATE:20261010
DTEND;VALUE=DATE:20261013
SUMMARY:Trip to Berlin
LOCATION:Berlin
END:VEVENT
BEGIN:VEVENT
UID:gone-1
DTSTART:20261006T100000Z
DTEND:20261006T110000Z
STATUS:CANCELLED
SUMMARY:Cancelled thing
END:VEVENT
END:VCALENDAR
"""


class Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self.content, self.headers = status, body, headers or {}


class FakeCalDav:
    """Principal → home → two calendars (one VTODO-only), and a REPORT returning the ICS above."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, data=None, timeout=None, allow_redirects=None, headers=None):
        self.calls.append((method, url))
        if method == "PROPFIND" and url == "https://dav.example.org/dav/":
            return Resp(207, b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/</d:href><d:propstat><d:prop>'
                             b'<d:current-user-principal><d:href>/dav/principals/marta/</d:href></d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>')
        if method == "PROPFIND" and url.endswith("/principals/marta/"):
            return Resp(207, b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/dav/principals/marta/</d:href>'
                             b'<d:propstat><d:prop><c:calendar-home-set><d:href>/dav/calendars/marta/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response></d:multistatus>')
        if method == "PROPFIND" and url.endswith("/calendars/marta/"):
            return Resp(207, b'''<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:a="http://apple.com/ns/ical/">
              <d:response><d:href>/dav/calendars/marta/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
              <d:response><d:href>/dav/calendars/marta/personal/</d:href><d:propstat><d:prop><d:displayname>Personal</d:displayname>
                <d:resourcetype><d:collection/><c:calendar/></d:resourcetype><a:calendar-color>#ff0000FF</a:calendar-color>
                <c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>
              <d:response><d:href>/dav/calendars/marta/tasks/</d:href><d:propstat><d:prop><d:displayname>Tasks</d:displayname>
                <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
                <c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>
            </d:multistatus>''')
        if method == "REPORT":
            body = ICS.replace("&", "&amp;").replace("<", "&lt;").encode()
            return Resp(207, b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/x.ics</d:href>'
                             b'<d:propstat><d:prop><c:calendar-data>' + body + b'</c:calendar-data></d:prop></d:propstat></d:response></d:multistatus>')
        return Resp(404)


@override_settings(INTEGRATIONS_ALLOW_PRIVATE_URLS=True, API_PUBLIC_URL="https://api.example", PUBLIC_URL="https://app.example")
class CalendarSourcesTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "c@example.com", "password": "s3cure-Passw0rd", "full_name": "C"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="c@example.com")
        self.lo = datetime(2026, 10, 4, tzinfo=dt_timezone.utc)
        self.hi = datetime(2026, 10, 20, tzinfo=dt_timezone.utc)

    def test_ics_occurrences(self):
        out = ics.occurrences([ICS], self.lo, self.hi, ZoneInfo("Europe/Rome"))
        titles = [v["title"] for _, v in out]
        self.assertEqual(titles.count("Standup"), 3)
        self.assertNotIn("Cancelled thing", titles)
        trip = next(v for _, v in out if v["title"] == "Trip to Berlin")
        self.assertEqual((trip["all_day"], trip["start_date"], trip["end_date"]), (True, date(2026, 10, 10), date(2026, 10, 12)))
        standups = sorted(v["start"] for _, v in out if v["title"] == "Standup")
        self.assertEqual(standups[0].astimezone(dt_timezone.utc).hour, 7)  # 9:30 Rome = 7:30 UTC
        self.assertEqual(len({k for k, _ in out}), len(out))  # distinct ids per occurrence

    def test_caldav_account(self):
        fake = FakeCalDav()
        with mock.patch("calendars.caldav.requests.Session.request", side_effect=fake), \
                mock.patch("calendars.sync.timezone.now", return_value=datetime(2026, 10, 4, 12, tzinfo=dt_timezone.utc)):
            res = self.client.post("/api/calendar/caldav/", {"server_url": "https://dav.example.org/dav/", "username": "marta", "password": "app-pass"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual([c["name"] for c in res.data["calendars"]], ["Personal"])  # VTODO-only list skipped
        account = CalendarAccount.objects.get()
        self.assertNotEqual(account.refresh_token, "app-pass")  # encrypted
        self.assertEqual(CalendarEvent.objects.filter(title="Standup").count(), 3)
        self.assertTrue(CalendarEvent.objects.get(title="Trip to Berlin").all_day)
        with mock.patch("calendars.caldav.requests.Session.request", return_value=Resp(401)):
            bad = self.client.post("/api/calendar/caldav/", {"server_url": "https://dav.example.org/dav/", "username": "x", "password": "y"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("app password", bad.data["detail"])

    def test_ics_subscription_and_bad_link(self):
        page = mock.Mock(status_code=200)
        page.raw.read.return_value = ICS.encode()
        with mock.patch("calendars.providers.requests.get", return_value=page), \
                mock.patch("calendars.sync.timezone.now", return_value=datetime(2026, 10, 4, 12, tzinfo=dt_timezone.utc)):
            res = self.client.post("/api/calendar/ics/", {"url": "webcal://cal.example.org/team.ics", "name": "Team"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["label"], "Team")
        self.assertEqual(CalendarEvent.objects.filter(title="Standup").count(), 3)
        page.raw.read.return_value = b"<html>not a calendar</html>"
        with mock.patch("calendars.providers.requests.get", return_value=page):
            bad = self.client.post("/api/calendar/ics/", {"url": "https://cal.example.org/x"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_microsoft_events(self):
        from calendars import microsoft

        account = CalendarAccount.objects.create(user=self.user, provider="microsoft", email="m@outlook.com")
        page = {"value": [
            {"id": "a", "subject": "Board meeting", "isAllDay": False, "start": {"dateTime": "2026-10-06T08:00:00.0000000"},
             "end": {"dateTime": "2026-10-06T09:30:00.0000000"}, "location": {"displayName": "Room 2"}, "webLink": "https://outlook/x"},
            {"id": "b", "subject": "Holiday", "isAllDay": True, "start": {"dateTime": "2026-10-08T00:00:00.0000000"},
             "end": {"dateTime": "2026-10-10T00:00:00.0000000"}},
            {"id": "c", "subject": "Declined", "isAllDay": False, "responseStatus": {"response": "declined"},
             "start": {"dateTime": "2026-10-06T10:00:00.0000000"}, "end": {"dateTime": "2026-10-06T11:00:00.0000000"}},
        ]}
        with mock.patch("calendars.microsoft._get", return_value=page):
            out = dict(microsoft.events(account, "cal-1", self.lo, self.hi, ZoneInfo("Europe/Rome")))
        self.assertEqual(set(out), {"a", "b"})
        self.assertEqual((out["a"]["location"], out["a"]["start_date"]), ("Room 2", date(2026, 10, 6)))
        self.assertEqual((out["b"]["all_day"], out["b"]["start_date"], out["b"]["end_date"]), (True, date(2026, 10, 8), date(2026, 10, 9)))
        self.assertIn("login.microsoftonline.com", microsoft.auth_url("s"))

    def test_ical_feed_out(self):
        ws = self.client.get("/api/workspaces/").data[0]["id"]
        today = timezone.localdate()
        self.client.post(f"/api/workspaces/{ws}/tasks/", {"title": "Write the report", "planned_date": str(today), "due_date": str(today + timedelta(days=2)), "assignee": self.user.id}, format="json")
        self.assertIsNone(self.client.get("/api/calendar/feed/").data["url"])
        url = self.client.post("/api/calendar/feed/").data["url"]
        self.assertTrue(url.startswith("https://api.example/api/ical/"))
        self.client.credentials()
        res = self.client.get(url.replace("https://api.example", ""))
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("SUMMARY:Write the report", body)
        self.assertIn("SUMMARY:⚑ Due: Write the report", body)
        self.assertIn("https://app.example/t/", body)
        self.assertEqual(self.client.get("/api/ical/wrong-token-wrong-token-xx.ics").status_code, 404)
