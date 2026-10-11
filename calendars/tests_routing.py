from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from calendars import routing, travel
from calendars.models import Calendar, CalendarAccount, CalendarEvent, GeoPlace
from concierge.models import ConciergeProfile
from integrations.http import DeliveryError

ROME = ZoneInfo("Europe/Rome")
HOME = "Via Donato Creti 32, 40128 Bologna, Italy"
BO = {"county": "Bologna", "state": "Emilia-Romagna", "country": "Italia"}
PLACES = {
    "via donato creti 32, 40128 bologna, italy": [{"lat": "44.5122", "lon": "11.3600", "address": {"city": "Bologna", **BO}}],
    # A namesake street in another town of the province comes first; the one in Bologna must win.
    "via rizzoli 1, 40125 bologna bo, italy": [{"lat": "44.5540", "lon": "11.4504", "address": {"town": "Granarolo dell'Emilia", **BO}},
                                               {"lat": "44.4940", "lon": "11.3465", "address": {"city": "Bologna", **BO}}],
    "via galileo galilei 8, 40051 altedo bo, italy": [{"lat": "44.6720", "lon": "11.4890", "address": {"village": "Altedo", "town": "Malalbergo", **BO}}],
    "nowhere street 1, atlantis": [],
    # Only namesakes elsewhere: better no answer than the wrong town.
    "via verdi 3, bologna, italy": [{"lat": "44.60", "lon": "11.50", "address": {"town": "Budrio", **BO}}],
}


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeMaps:
    """Nominatim and OSRM: 1 km of road ≈ 1 minute, from the straight-line offset."""

    def __init__(self, down=False):
        self.calls = []
        self.down = down

    def __call__(self, method, url, **kwargs):
        self.calls.append(url)
        if self.down:
            raise DeliveryError("Request failed: timeout")
        if "/search?" in url:
            from urllib.parse import parse_qs, urlparse

            q = parse_qs(urlparse(url).query)["q"][0].lower()
            return FakeResponse(PLACES.get(" ".join(q.split()), []))
        coords = url.split("/route/v1/")[1].split("/", 1)[1].split("?")[0].split(";")
        (lon1, lat1), (lon2, lat2) = [map(float, c.split(",")) for c in coords]
        km = (((lat2 - lat1) * 111) ** 2 + ((lon2 - lon1) * 79) ** 2) ** 0.5
        return FakeResponse({"code": "Ok", "routes": [{"duration": km * 1.3 * 60}]})


@override_settings(NOMINATIM_URL="https://nominatim.test", OSRM_URL="https://osrm.test")
class RoutingTests(APITestCase):
    def setUp(self):
        routing._last_nominatim = 0.0
        self.user = User.objects.create_user(email="drive@example.com", password="s3cure-Passw0rd", home_location=HOME,
                                             focus={"travel_mode": "driving"})
        ConciergeProfile.objects.create(user=self.user, timezone="Europe/Rome")
        account = CalendarAccount.objects.create(user=self.user, provider="ics")
        self.cal = Calendar.objects.create(account=account, external_id="c", name="Main")
        self.now = timezone.now()
        sleep = mock.patch("calendars.routing.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def event(self, title, location, hours=24, **extra):
        start = self.now + timedelta(hours=hours)
        return CalendarEvent.objects.create(calendar=self.cal, user=self.user, external_id=title, title=title, location=location,
                                            start=start, end=start + timedelta(hours=1), start_date=start.date(), end_date=start.date(), **extra)

    def test_real_driving_time_replaces_the_estimate(self):
        town = self.event("Dentist", "Via Rizzoli 1, 40125 Bologna BO, Italy")
        away = self.event("Club", "Via Galileo Galilei 8, 40051 Altedo BO, Italy", hours=30)
        online = self.event("Call", "https://meet.google.com/x", hours=2)
        room = self.event("Retro", "Room 3", hours=3)
        lost = self.event("Party", "Nowhere Street 1, Atlantis", hours=4)
        self.assertEqual(travel.minutes(town, self.user), 45)  # nothing computed yet: the estimate
        maps = FakeMaps()
        with mock.patch("calendars.routing.send", maps):
            self.assertEqual(routing.refresh(self.user, self.now), 3)  # town, away, lost (not the call or the room)
        for e in (town, away, online, room, lost):
            e.refresh_from_db()
        self.assertEqual(town.drive_minutes, 3)
        self.assertEqual((travel.minutes(town, self.user), travel.source(town, self.user)), (10, "driving"))  # 3 + 5 parking → 10
        self.assertEqual(travel.minutes(away, self.user), 35)  # the real drive (27 + 5 → 35), not 2 × 45
        self.assertEqual((lost.drive_minutes, travel.minutes(lost, self.user), travel.source(lost, self.user)), (None, 90, "fixed"))
        self.assertEqual((online.drive_key, room.drive_key), ("", ""))
        self.assertEqual(GeoPlace.objects.count(), 4)  # home + three places, cached
        # Nothing to do the second time; a moved event or a new home is computed again.
        with mock.patch("calendars.routing.send", FakeMaps()) as again:
            self.assertEqual(routing.refresh(self.user, self.now), 0)
        CalendarEvent.objects.filter(pk=town.pk).update(location="Via Galileo Galilei 8, 40051 Altedo BO, Italy")
        maps = FakeMaps()
        with mock.patch("calendars.routing.send", maps):
            self.assertEqual(routing.refresh(self.user, self.now), 1)
        self.assertFalse(any("/search?" in u for u in maps.calls))  # both places already cached
        town.refresh_from_db()
        self.assertEqual(travel.minutes(town, self.user), 35)

    def test_fixed_mode_and_outages(self):
        town = self.event("Dentist", "Via Rizzoli 1, 40125 Bologna BO, Italy")
        with mock.patch("calendars.routing.send", FakeMaps(down=True)):
            routing.refresh(self.user, self.now)
        town.refresh_from_db()
        self.assertEqual(town.drive_key, "")  # an outage isn't an answer: tried again next time
        self.assertFalse(GeoPlace.objects.exists())
        with mock.patch("calendars.routing.send", FakeMaps()):
            routing.refresh(self.user, self.now)
        town.refresh_from_db()
        self.user.focus = {"travel_mode": "fixed"}
        self.user.save()
        self.assertEqual((travel.minutes(town, self.user), travel.source(town, self.user)), (45, "fixed"))
        # A new home makes the stored drive stale until it's recomputed.
        self.user.focus = {"travel_mode": "driving"}
        self.user.home_location = "Piazza Maggiore 1, 40124 Bologna, Italy"
        self.user.save()
        self.assertEqual(travel.minutes(town, self.user), 45)

    def test_worker_only_for_people_who_opted_in(self):
        other = User.objects.create_user(email="fixed@example.com", password="s3cure-Passw0rd", home_location=HOME)
        self.event("Dentist", "Via Rizzoli 1, 40125 Bologna BO, Italy")
        routing._last_tick = 0.0
        with mock.patch("calendars.routing.refresh", return_value=1) as refresh:
            routing.tick()
        self.assertEqual([c.args[0] for c in refresh.call_args_list], [self.user])
        self.assertNotIn(other, [c.args[0] for c in refresh.call_args_list])

    def test_briefing_and_api_say_by_car(self):
        town = self.event("Dentist", "Via Rizzoli 1, 40125 Bologna BO, Italy")
        with mock.patch("calendars.routing.send", FakeMaps()):
            routing.refresh(self.user, self.now)
        town.refresh_from_db()
        title, body = travel.briefing(town, self.user, ROME)
        self.assertIn("about 10 min by car", body)
        self.client.force_authenticate(self.user)
        data = next(e for e in self.client.get(f"/api/calendar/events/?from={town.start_date}").data if e["id"] == town.pk)
        self.assertEqual((data["travel_minutes"], data["travel_source"]), (10, "driving"))

    @override_settings(OSRM_URL="http://osrm.internal:5000", OSRM_PROFILE="car")
    def test_on_premises_osrm_and_admin_check(self):
        maps = FakeMaps()
        PLACES["piazza maggiore, bologna, italy"] = [{"lat": "44.4938", "lon": "11.3426", "address": {"city": "Bologna", **BO}}]
        PLACES["piazza del nettuno, bologna, italy"] = [{"lat": "44.4945", "lon": "11.3428", "address": {"city": "Bologna", **BO}}]
        with mock.patch("calendars.routing.send", maps):
            message = routing.check()
        self.assertIn("min by car", message)
        self.assertTrue(any(u.startswith("http://osrm.internal:5000/route/v1/car/") for u in maps.calls), maps.calls)
        staff = User.objects.create_user(email="admin@example.com", password="s3cure-Passw0rd", is_staff=True)
        self.client.force_authenticate(staff)
        with mock.patch("calendars.routing.send", FakeMaps(down=True)):
            GeoPlace.objects.all().delete()
            res = self.client.post("/api/admin/check/maps/")
        self.assertEqual(res.data["ok"], False)
        self.assertIn("Nominatim", res.data["message"])


class MatchTests(APITestCase):
    def test_town_must_match_and_province_does_not_count(self):
        results = PLACES["via verdi 3, bologna, italy"]
        self.assertIsNone(routing.best_match("Via Verdi 3, Bologna, Italy", results))
        self.assertEqual(routing.best_match("Via Rizzoli 1, 40125 Bologna BO, Italy", PLACES["via rizzoli 1, 40125 bologna bo, italy"])["lat"], "44.4940")
        self.assertEqual(routing.best_match("Studio Rossi", [{"lat": "1", "lon": "2", "address": {}}])["lat"], "1")  # no town named
        self.assertEqual(routing.towns_in("Via X 1, 40125 Bologna BO, Italy"), {"bologna", "italy"})
