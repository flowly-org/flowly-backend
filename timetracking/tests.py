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
        User.objects.filter(email="alice@example.com").update(plan="pro")  # invoices are a Pro feature
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



class FatturaPATests(APITestCase):
    """Italian e-invoice XML, validated against the official FatturaPA 1.2.2 schema."""

    def setUp(self):
        from pathlib import Path

        from lxml import etree

        self.schema = etree.XMLSchema(etree.parse(str(Path(__file__).parent / "xsd" / "FatturaPA_v1.2.2.xsd")))
        auth = self.client.post("/api/auth/register/", {"email": "f@example.com", "password": "s3cure-Passw0rd", "full_name": "F"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="f@example.com")
        self.ws = self.client.post("/api/workspaces/", {"name": "Studio"}).data["id"]
        self.base = f"/api/workspaces/{self.ws}"
        self.project = self.client.post(f"{self.base}/projects/", {"name": "Sito ACME"}).data
        self.client.patch(f"{self.base}/billing/", {"enabled": True, "default_rate": "50.00", "tax_rate": "22.00"}, format="json")
        res = self.client.patch("/api/my/invoicing/", {
            "name": "Marco Rossi", "country": "it", "vat_id": "01234567890", "tax_code": "RSSMRC80A01A944X",
            "address": "Via Indipendenza 1", "zip": "40121", "city": "Bologna", "province": "BO",
            "iban": "IT60 X054 2811 1010 0000 0123 456",
        }, format="json")
        self.assertEqual(res.data["format"], "fatturapa")
        self.client.patch(f"{self.base}/billing/projects/{self.project['id']}/", {
            "mode": "inherit", "client_name": "ACME S.r.l.", "client_vat": "09876543210", "client_address": "Corso Como 10",
            "client_zip": "20154", "client_city": "Milano", "client_province": "MI", "client_sdi_code": "M5UXCR1",
        }, format="json")
        task = self.client.post(f"{self.base}/tasks/", {"title": "Homepage", "project": self.project["id"]}, format="json").data
        start = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        self.client.post(f"{self.base}/time-entries/", {"task": task["id"], "started_at": start.isoformat(),
                                                         "ended_at": (start + timedelta(minutes=90)).isoformat()}, format="json")
        self.period = {"period_start": start.date().isoformat(), "period_end": start.date().isoformat(), "project": self.project["id"]}

    def invoice(self):
        res = self.client.post(f"{self.base}/invoices/", self.period, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def validate(self, xml: bytes):
        from lxml import etree

        doc = etree.fromstring(xml)
        self.assertTrue(self.schema.validate(doc), self.schema.error_log)
        return doc

    def test_invoices_need_pro(self):
        self.user.plan = "free"
        self.user.save()
        res = self.client.post(f"{self.base}/invoices/", self.period, format="json")
        self.assertEqual(res.status_code, 402)
        self.assertIn("Pro", res.data["detail"])

    def test_vat_invoice_is_schema_valid(self):
        self.user.plan = "pro"
        self.user.save()
        inv = self.invoice()
        check = self.client.get(f"{self.base}/invoices/{inv['id']}/einvoice/?check=1").data
        self.assertTrue(check["ok"], check)
        self.assertTrue(check["filename"].startswith("IT01234567890_"))
        res = self.client.get(f"{self.base}/invoices/{inv['id']}/einvoice/")
        self.assertEqual(res["Content-Type"], "application/xml")
        doc = self.validate(res.content)
        ns = {"p": "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"}
        text = lambda path: doc.xpath(path, namespaces=ns)[0].text
        self.assertEqual(text("//CodiceDestinatario"), "M5UXCR1")
        self.assertEqual(text("//DettaglioLinee/Quantita"), "1.50")
        self.assertEqual(text("//DettaglioLinee/PrezzoTotale"), "75.00")
        self.assertEqual(text("//DatiRiepilogo/Imposta"), "16.50")
        self.assertEqual(text("//ImportoTotaleDocumento"), "91.50")
        self.assertEqual(text("//IBAN"), "IT60X0542811101000000123456")

    def test_flat_rate_regime_with_stamp_duty_and_pec(self):
        self.user.plan = "pro"
        self.user.save()
        self.client.patch(f"{self.base}/billing/", {"tax_rate": "0", "default_rate": "60.00"}, format="json")
        res = self.client.patch("/api/my/invoicing/", {"format_settings": {"fatturapa": {
            "regime": "RF19", "stamp_duty": True, "vat_nature": "N2.2", "unknown": 1}}}, format="json")
        self.assertEqual(res.data["profile"]["format_settings"], {"fatturapa": {"regime": "RF19", "stamp_duty": True, "vat_nature": "N2.2"}})
        bad = self.client.patch("/api/my/invoicing/", {"format_settings": {"fatturapa": {"regime": "RF99"}}}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.client.patch(f"{self.base}/billing/projects/{self.project['id']}/", {
            "mode": "inherit", "client_name": "Mario Bianchi", "client_vat": "", "client_tax_code": "BNCMRA70A01F205Z",
            "client_address": "Via Roma 2", "client_zip": "20121", "client_city": "Milano", "client_sdi_code": "",
            "client_pec": "mario@pec.it",
        }, format="json")
        inv = self.invoice()
        doc = self.validate(self.client.get(f"{self.base}/invoices/{inv['id']}/einvoice/").content)
        ns = {"p": "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"}
        text = lambda path: doc.xpath(path, namespaces=ns)[0].text
        self.assertEqual(text("//CodiceDestinatario"), "0000000")
        self.assertEqual(text("//PECDestinatario"), "mario@pec.it")
        self.assertEqual(text("//DatiRiepilogo/Natura"), "N2.2")
        self.assertEqual(text("//RegimeFiscale"), "RF19")
        self.assertEqual(text("//ImportoTotaleDocumento"), "90.00")
        self.assertEqual(text("//DatiBollo/ImportoBollo"), "2.00")  # VAT-free and over €77.47

    def test_drafts_follow_settings_and_sent_invoices_are_frozen(self):
        self.user.plan = "pro"
        self.user.save()
        self.client.patch(f"{self.base}/billing/projects/{self.project['id']}/", {"client_city": ""}, format="json")
        inv = self.invoice()
        url = f"{self.base}/invoices/{inv['id']}/einvoice/"
        self.assertFalse(self.client.get(url + "?check=1").data["ok"])
        self.client.patch(f"{self.base}/billing/projects/{self.project['id']}/", {"client_city": "Milano"}, format="json")
        self.assertTrue(self.client.get(url + "?check=1").data["ok"])  # the draft picked it up
        self.client.patch(f"{self.base}/billing/projects/{self.project['id']}/", {"client_name": "ACME Italia S.p.A."}, format="json")
        self.assertEqual(self.client.get(f"{self.base}/invoices/{inv['id']}/").data["client_name"], "ACME Italia S.p.A.")
        self.client.patch(f"{self.base}/invoices/{inv['id']}/", {"status": "sent"}, format="json")
        self.client.patch(f"{self.base}/billing/projects/{self.project['id']}/", {"client_city": "Torino", "client_name": "X"}, format="json")
        xml = self.client.get(url).content
        self.assertIn(b"<Comune>Milano</Comune>", xml)
        self.assertIn("ACME Italia S.p.A.".encode(), xml)

    def test_issuer_details_belong_to_the_user_in_every_workspace(self):
        self.user.plan = "pro"
        self.user.save()
        inv = self.invoice()
        self.assertEqual(inv["issuer_name"], "Marco Rossi")
        self.assertIn("IBAN IT60X0542811101000000123456", inv["issuer_details"])
        # A second workspace uses the same details, with no billing set-up for the issuer.
        other = self.client.post("/api/workspaces/", {"name": "Altro"}).data["id"]
        self.client.patch(f"/api/workspaces/{other}/billing/", {"enabled": True, "default_rate": "40"}, format="json")
        project = self.client.post(f"/api/workspaces/{other}/projects/", {"name": "P"}).data
        task = self.client.post(f"/api/workspaces/{other}/tasks/", {"title": "T", "project": project["id"]}, format="json").data
        start = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        self.client.post(f"/api/workspaces/{other}/time-entries/", {"task": task["id"], "started_at": start.isoformat(),
                         "ended_at": (start + timedelta(hours=1)).isoformat()}, format="json")
        day = start.date().isoformat()
        inv2 = self.client.post(f"/api/workspaces/{other}/invoices/", {"period_start": day, "period_end": day}, format="json").data
        self.assertEqual(inv2["issuer_name"], "Marco Rossi")
        # Country without a format yet: explained, not crashed.
        self.client.patch("/api/my/invoicing/", {"country": "DE"}, format="json")
        check = self.client.get(f"/api/workspaces/{other}/invoices/{inv2['id']}/einvoice/?check=1").data
        self.assertEqual(check["format"], None)
        self.assertFalse(check["ok"])

    def test_missing_data_is_listed(self):
        self.user.plan = "pro"
        self.user.save()
        self.client.patch("/api/my/invoicing/", {"vat_id": "", "zip": "4012"}, format="json")
        inv = self.invoice()
        check = self.client.get(f"{self.base}/invoices/{inv['id']}/einvoice/?check=1").data
        self.assertFalse(check["ok"])
        self.assertIn("Your Partita IVA (Settings → Invoicing).", check["missing"])
        self.assertIn("Your CAP must be 5 digits.", check["missing"])
        self.assertEqual(self.client.get(f"{self.base}/invoices/{inv['id']}/einvoice/").status_code, 400)
