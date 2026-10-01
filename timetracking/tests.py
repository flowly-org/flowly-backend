from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import ApiToken, User
from timetracking.models import TimeEntry
from workspaces.models import Membership


class TimeTrackingTests(APITestCase):
    def setUp(self):
        reg = lambda e: self.client.post("/api/auth/register/", {"email": e, "password": "s3cure-Passw0rd", "full_name": e[0].upper()}).data
        self.alice, self.bob = reg("alice@example.com"), reg("bob@example.com")
        self.as_user(self.alice)
        self.ws = self.client.post("/api/workspaces/", {"name": "Agency"}).data
        Membership.objects.create(workspace_id=self.ws["id"], user_id=self.bob["user"]["id"])
        self.base = f"/api/workspaces/{self.ws['id']}"
        self.site = self.client.post(f"{self.base}/projects/", {"name": "Website"}).data
        self.internal = self.client.post(f"{self.base}/projects/", {"name": "Internal"}).data
        self.task = self.client.post(f"{self.base}/tasks/", {"title": "Build homepage", "project": self.site["id"]}, format="json").data

    def as_user(self, auth):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")

    def log(self, minutes, *, task=None, project=None, days_ago=0):
        start = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
        data = {"started_at": start.isoformat(), "duration_seconds": minutes * 60, "description": "work"}
        if task:
            data["task"] = task
        if project:
            data["project"] = project
        res = self.client.post(f"{self.base}/time-entries/", data, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def test_timer_start_switch_stop(self):
        res = self.client.post("/api/my/timer/", {"action": "start", "workspace": self.ws["id"], "task": self.task["id"]}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["entry"]["project"], self.site["id"])
        first = TimeEntry.objects.get()
        TimeEntry.objects.filter(pk=first.pk).update(started_at=timezone.now() - timedelta(minutes=30))
        # Starting another timer stops the first one.
        self.client.post("/api/my/timer/", {"action": "start", "workspace": self.ws["id"], "description": "email"}, format="json")
        first.refresh_from_db()
        self.assertFalse(first.running)
        self.assertGreaterEqual(first.duration_seconds, 1799)
        self.assertEqual(TimeEntry.objects.filter(ended_at__isnull=True).count(), 1)
        self.assertIsNotNone(self.client.get("/api/my/timer/").data["entry"])
        self.client.post("/api/my/timer/", {"action": "stop"}, format="json")
        self.assertIsNone(self.client.get("/api/my/timer/").data["entry"])

    def test_entries_validation_and_privacy(self):
        self.log(60, task=self.task["id"])
        bad = self.client.post(f"{self.base}/time-entries/", {"started_at": timezone.now().isoformat()}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.as_user(self.bob)
        self.log(30, project=self.internal["id"])
        self.assertEqual(len(self.client.get(f"{self.base}/time-entries/").data), 1)  # only his own
        alice_entry = TimeEntry.objects.get(user__email="alice@example.com")
        self.assertEqual(self.client.delete(f"{self.base}/time-entries/{alice_entry.id}/").status_code, 404)
        self.as_user(self.alice)  # owner sees everyone's
        self.assertEqual(len(self.client.get(f"{self.base}/time-entries/").data), 2)

    def test_billing_rates_opt_out_report_and_invoice(self):
        self.log(90, task=self.task["id"])  # Website
        self.log(45, project=self.internal["id"])  # Internal (opted out below)
        self.log(20, task=self.task["id"], days_ago=40)  # outside the invoice period
        # Billing off: nothing billable in reports, invoices refused.
        rep = self.client.get(f"{self.base}/time-entries/report/").data
        self.assertEqual((rep["billing_enabled"], rep["totals"]["amount"]), (False, "0.00"))
        today = timezone.localdate().isoformat()
        period = {"period_start": (timezone.localdate() - timedelta(days=7)).isoformat(), "period_end": today}
        self.assertEqual(self.client.post(f"{self.base}/invoices/", period, format="json").status_code, 400)

        self.client.patch(f"{self.base}/billing/", {"enabled": True, "default_rate": "50.00", "rounding_minutes": 15,
                                                    "tax_rate": "22.00", "issuer_name": "Alice Studio"}, format="json")
        self.client.patch(f"{self.base}/billing/projects/{self.site['id']}/", {"mode": "hourly", "hourly_rate": "80.00", "client_name": "ACME"}, format="json")
        self.client.patch(f"{self.base}/billing/projects/{self.internal['id']}/", {"mode": "off"}, format="json")
        billing = self.client.get(f"{self.base}/billing/").data
        rates = {p["name"]: p["effective_rate"] for p in billing["projects"]}
        self.assertEqual(rates, {"Website": "80.00", "Internal": None})

        rep = self.client.get(f"{self.base}/time-entries/report/?group=project&from={period['period_start']}").data
        site_row = next(r for r in rep["rows"] if r["label"] == "Website")
        self.assertEqual(site_row["amount"], "120.00")  # 1.5h × 80
        self.assertEqual(next(r for r in rep["rows"] if r["label"] == "Internal")["amount"], "0.00")

        inv = self.client.post(f"{self.base}/invoices/", {**period, "project": self.site["id"]}, format="json").data
        self.assertEqual((inv["number"], inv["client_name"], inv["subtotal"], inv["tax_amount"], inv["total"]),
                         ("INV-0001", "ACME", "120.00", "26.40", "146.40"))
        self.assertEqual(inv["lines"][0]["description"], "Build homepage")
        # Invoiced time is locked and not invoiced twice.
        locked = TimeEntry.objects.get(invoice_id=inv["id"])
        self.assertEqual(self.client.patch(f"{self.base}/time-entries/{locked.id}/", {"description": "x"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(f"{self.base}/invoices/", {**period, "project": self.site["id"]}, format="json").status_code, 400)
        csv = self.client.get(f"{self.base}/invoices/{inv['id']}/csv/").content.decode()
        self.assertIn("Total,,,146.40", csv.replace("\r", ""))
        # Deleting the draft releases the time; a sent invoice can't be deleted.
        self.client.delete(f"{self.base}/invoices/{inv['id']}/")
        self.assertFalse(TimeEntry.objects.filter(invoice__isnull=False).exists())
        inv2 = self.client.post(f"{self.base}/invoices/", {**period, "project": self.site["id"]}, format="json").data
        self.assertEqual(inv2["number"], "INV-0002")
        self.client.patch(f"{self.base}/invoices/{inv2['id']}/", {"status": "sent"}, format="json")
        self.assertEqual(self.client.delete(f"{self.base}/invoices/{inv2['id']}/").status_code, 400)
        self.assertEqual(self.client.patch(f"{self.base}/invoices/{inv2['id']}/", {"client_name": "Evil"}, format="json").status_code, 400)
        # Members can't see invoices.
        self.as_user(self.bob)
        self.assertEqual(self.client.get(f"{self.base}/invoices/").status_code, 403)

    def test_switching_to_own_rate_starts_from_default(self):
        self.client.patch(f"{self.base}/billing/", {"enabled": True, "default_rate": "45.00"}, format="json")
        res = self.client.patch(f"{self.base}/billing/projects/{self.site['id']}/", {"mode": "hourly"}, format="json")
        self.assertEqual((res.status_code, res.data["hourly_rate"]), (200, "45.00"))

    def test_invoice_lines_add_up_as_printed(self):
        self.client.patch(f"{self.base}/billing/", {"enabled": True, "default_rate": "80.00"}, format="json")
        entry = self.log(90, task=self.task["id"])
        TimeEntry.objects.filter(pk=entry["id"]).update(duration_seconds=90 * 60 + 2)  # a timer's stray seconds
        today = timezone.localdate().isoformat()
        inv = self.client.post(f"{self.base}/invoices/", {"period_start": today, "period_end": today}, format="json").data
        line = inv["lines"][0]
        self.assertEqual((line["hours"], line["amount"]), ("1.50", "120.00"))

    def test_project_opt_outs_from_projects_panel(self):
        res = self.client.patch(f"{self.base}/projects/{self.site['id']}/", {"time_tracking": False}, format="json")
        self.assertFalse(res.data["time_tracking"])
        self.assertEqual(self.client.post("/api/my/timer/", {"action": "start", "workspace": self.ws["id"], "task": self.task["id"]}, format="json").status_code, 400)
        start = timezone.now().isoformat()
        self.assertEqual(self.client.post(f"{self.base}/time-entries/", {"task": self.task["id"], "started_at": start, "duration_seconds": 600}, format="json").status_code, 400)
        self.client.patch(f"{self.base}/projects/{self.site['id']}/", {"time_tracking": True}, format="json")
        self.log(30, task=self.task["id"])
        # Billing opt-out from the same panel (owners/admins only).
        res = self.client.patch(f"{self.base}/projects/{self.site['id']}/", {"billable": False}, format="json")
        self.assertFalse(res.data["billable"])
        self.assertFalse(TimeEntry.objects.get().billable)
        self.assertEqual(self.client.get(f"{self.base}/billing/").data["projects"][0]["mode"], "off")
        self.as_user(self.bob)
        self.assertEqual(self.client.patch(f"{self.base}/projects/{self.site['id']}/", {"billable": True}, format="json").status_code, 403)

    def test_workspace_time_tracking_switch(self):
        res = self.client.patch(f"{self.base}/", {"time_tracking": False}, format="json")
        self.assertFalse(res.data["time_tracking"])
        self.assertEqual(self.client.post("/api/my/timer/", {"action": "start", "workspace": self.ws["id"], "description": "x"}, format="json").status_code, 400)
        res = self.client.post(f"{self.base}/time-entries/", {"started_at": timezone.now().isoformat(), "duration_seconds": 60}, format="json")
        self.assertEqual(res.status_code, 400)
        self.client.patch(f"{self.base}/", {"time_tracking": True}, format="json")
        self.assertTrue(self.client.get(f"{self.base}/").data["time_tracking"])

    def test_rounding(self):
        self.client.patch(f"{self.base}/billing/", {"enabled": True, "default_rate": "60.00", "rounding_minutes": 15}, format="json")
        self.log(7, project=self.internal["id"])  # rounds up to 15 min
        rep = self.client.get(f"{self.base}/time-entries/report/").data
        self.assertEqual((rep["totals"]["billable_seconds"], rep["totals"]["amount"]), (900, "15.00"))

    def test_mcp_time_tools(self):
        _, raw = ApiToken.issue(User.objects.get(email="alice@example.com"), "test")
        self.client.credentials()  # stored credentials would override the per-request header
        call = lambda name, args: self.client.post("/api/mcp/", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}}, format="json", HTTP_AUTHORIZATION=f"Bearer {raw}").json()["result"]
        out = call("start_timer", {"task_id": self.task["id"]})
        self.assertIn("Timer started on Build homepage", out["content"][0]["text"])
        out = call("log_time", {"minutes": 50, "task_id": self.task["id"], "day": "yesterday"})
        self.assertFalse(out["isError"], out)
        self.assertIn("0h50m", out["content"][0]["text"])
        self.assertIn("Stopped", call("stop_timer", {})["content"][0]["text"])
        self.assertIn("Agency / Website", call("time_report", {"start": (timezone.localdate() - timedelta(days=3)).isoformat()})["content"][0]["text"])
        self.assertTrue(call("log_time", {"minutes": 0})["isError"])
        self.assertEqual(Decimal(TimeEntry.objects.count()), 2)
