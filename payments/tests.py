import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User

from . import services, stripe_api
from .models import Customer, InvoicePayment, Subscription


class FakeStripe:
    """Records calls; answers like Stripe for the endpoints Flowly uses."""

    def __init__(self):
        self.calls = []
        self.n = 0
        self.sessions_paid = False
        self.endpoints = []
        self.prices = {}
        self.subs = {}

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n}"

    def respond(self, method, url, params, headers):
        path = url.split("/v1/", 1)[1]
        data = dict(params or [])
        self.calls.append((method, path, data, headers))
        key = headers["Authorization"].split()[1]
        if key == "sk_bad":
            return 401, {"error": {"message": "Invalid API key provided"}}
        if path == "products":
            return 200, {"id": self._id("prod")}
        if path == "prices" and method == "POST":
            price = {"id": self._id("price"), "unit_amount": int(data["unit_amount"]), "currency": data["currency"],
                     "tax_behavior": data.get("tax_behavior", "unspecified")}
            self.prices[price["id"]] = price
            return 200, price
        if path.startswith("prices/"):
            return 200, self.prices[path[7:]]
        if path == "billing_portal/configurations":
            return 200, {"id": "bpc_1"}
        if path == "webhook_endpoints" and method == "GET":
            return 200, {"data": self.endpoints}
        if path == "webhook_endpoints":
            self.endpoints.append({"id": "we_1", "url": data["url"]})
            return 200, {"id": "we_1", "secret": "whsec_test"}
        if path == "customers":
            return 200, {"id": self._id("cus")}
        if path.startswith("customers/"):
            return 200, {"id": path[10:]}
        if path == "checkout/sessions" and method == "POST":
            return 200, {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"}
        if path == "checkout/sessions":
            return 200, {"data": [{"payment_status": "paid"}] if self.sessions_paid else []}
        if path == "billing_portal/sessions":
            return 200, {"url": "https://billing.stripe.com/p/session/x"}
        if path == "subscriptions":
            sub = subscription(self._id("sub"), data["customer"], "active", data.get("metadata[kind]", "pro"),
                               int(data.get("items[0][price_data][unit_amount]", 600)))
            self.subs[sub["id"]] = sub
            return 200, sub
        if path.startswith("subscriptions/") and method == "DELETE":
            return 200, {**self.subs.get(path[14:], {}), "status": "canceled"}
        if path.startswith("subscriptions/"):
            sub = self.subs[path[14:]]
            if "cancel_at_period_end" in data:
                sub["cancel_at_period_end"] = data["cancel_at_period_end"] == "true"
            return 200, sub
        if path == "payment_links" and method == "GET":
            return 200, {"data": []}
        if path == "account":
            return 200, {"id": "acct_1", "settings": {"dashboard": {"display_name": "Studio Rossi"}}}
        if path == "payment_links":
            return 200, {"id": "plink_1", "url": "https://buy.stripe.com/test_1"}
        if path.startswith("payment_links/"):
            return 200, {"id": path[14:], "active": False}
        raise AssertionError(f"unexpected {method} {path}")

    def get(self, url, params=None, headers=None, timeout=None):
        return self._resp(*self.respond("GET", url, params, headers))

    def request(self, method, url, data=None, headers=None, timeout=None):
        return self._resp(*self.respond(method, url, data, headers))

    @staticmethod
    def _resp(code, body):
        r = mock.Mock()
        r.status_code, r.content = code, json.dumps(body).encode()
        r.json.return_value = body
        return r


def subscription(sid, customer, status, kind, amount, interval="month", days=30):
    end = int(time.time()) + days * 86400
    return {"id": sid, "customer": customer, "status": status, "cancel_at_period_end": False, "metadata": {"kind": kind},
            "items": {"data": [{"current_period_end": end, "price": {"unit_amount": amount, "currency": "eur",
                                                                     "recurring": {"interval": interval}}}]}}


def signed(payload: bytes, secret="whsec_test"):
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


@override_settings(STRIPE_SECRET_KEY="sk_test_flowly", STRIPE_AUTOMATIC_TAX=True, PUBLIC_URL="https://app.example",
                   API_PUBLIC_URL="https://api.example")
class StripeTests(APITestCase):
    def setUp(self):
        self.stripe = FakeStripe()
        patcher = mock.patch.multiple("payments.stripe_api.requests", get=self.stripe.get, request=self.stripe.request)
        patcher.start()
        self.addCleanup(patcher.stop)
        services._price_cache.update(data=None, at=0.0)
        from instance import conf

        conf.clear_cache()  # settings are cached per process for a few seconds
        auth = self.client.post("/api/auth/register/", {"email": "pay@example.com", "password": "s3cure-Passw0rd", "full_name": "Pay"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="pay@example.com")

    def set_up_stripe(self):
        self.user.is_staff = True
        self.user.save()
        res = self.client.post("/api/admin/stripe/setup/")
        self.assertEqual(res.status_code, 200, res.data)
        self.user.is_staff = False
        self.user.save()
        return res.data

    def webhook(self, event):
        body = json.dumps(event).encode()
        self.client.credentials()
        res = self.client.generic("POST", "/api/billing/stripe/webhook/", body, content_type="application/json",
                                  HTTP_STRIPE_SIGNATURE=signed(body))
        return res

    def test_flatten_and_signature(self):
        self.assertEqual(stripe_api.flatten({"line_items": [{"price": "p", "quantity": 1}], "automatic_tax": {"enabled": True}, "x": None}),
                         [("line_items[0][price]", "p"), ("line_items[0][quantity]", "1"), ("automatic_tax[enabled]", "true")])
        body = b'{"a":1}'
        self.assertTrue(stripe_api.verify_signature(body, signed(body), "whsec_test"))
        self.assertFalse(stripe_api.verify_signature(body, signed(body, "other"), "whsec_test"))
        old = f"t={int(time.time()) - 1000},v1=" + hmac.new(b"whsec_test", f"{int(time.time()) - 1000}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertFalse(stripe_api.verify_signature(body, old, "whsec_test"))
        self.assertEqual(stripe_api.period_end({"items": {"data": [{"current_period_end": 5}]}}), 5)

    def test_setup_checkout_webhooks_and_portal(self):
        self.assertFalse(self.client.get("/api/billing/plan/").data["stripe"])
        created = self.set_up_stripe()["created"]
        self.assertEqual(set(created), {"pro", "pro_plus", "enterprise", "portal", "webhook"})
        self.assertEqual(self.set_up_stripe()["created"], {})  # idempotent
        plan = self.client.get("/api/billing/plan/").data
        self.assertTrue(plan["stripe"])
        self.assertEqual(plan["prices"]["month"]["amount"], 600)

        url = self.client.post("/api/billing/checkout/", {"interval": "year"}, format="json").data["url"]
        self.assertTrue(url.startswith("https://checkout.stripe.com/"))
        session = next(c[2] for c in self.stripe.calls if c[1] == "checkout/sessions")
        self.assertEqual(session["mode"], "subscription")
        self.assertEqual(session["automatic_tax[enabled]"], "true")
        self.assertEqual(session["tax_id_collection[enabled]"], "true")
        self.assertEqual(session["subscription_data[metadata][kind]"], "pro")
        customer = Customer.objects.get(user=self.user)

        # Stripe → webhook: Pro on, with a grace period after the period end.
        sub = subscription("sub_live", customer.customer_id, "active", "pro", 6000, "year", days=365)
        self.assertEqual(self.webhook({"id": "evt_1", "type": "customer.subscription.created", "data": {"object": sub}}).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual((self.user.plan, self.user.plan_source), ("pro", "stripe"))
        self.assertGreater(self.user.plan_expires_at, timezone.now() + timedelta(days=366))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.client.post('/api/auth/login/', {'email': 'pay@example.com', 'password': 's3cure-Passw0rd'}).data['access']}")
        self.assertEqual(self.client.post("/api/billing/checkout/", {"interval": "month"}, format="json").status_code, 400)
        self.assertTrue(self.client.post("/api/billing/portal/").data["url"].startswith("https://billing.stripe.com/"))
        plan = self.client.get("/api/billing/plan/").data
        self.assertEqual(plan["subscription"]["interval"], "year")

        # Past due keeps Pro (Stripe retries); deleted ends it.
        self.webhook({"id": "evt_2", "type": "customer.subscription.updated", "data": {"object": {**sub, "status": "past_due"}}})
        self.user.refresh_from_db()
        self.assertEqual(self.user.plan, "pro")
        self.webhook({"id": "evt_3", "type": "customer.subscription.deleted", "data": {"object": {**sub, "status": "canceled"}}})
        self.user.refresh_from_db()
        self.assertEqual((self.user.plan, self.user.plan_expires_at), ("free", None))

    def test_bad_signature_and_manual_plans_survive(self):
        self.set_up_stripe()
        body = b'{"type": "customer.subscription.deleted"}'
        self.client.credentials()
        res = self.client.generic("POST", "/api/billing/stripe/webhook/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=x")
        self.assertEqual(res.status_code, 400)
        self.user.plan, self.user.plan_source = "pro", "manual"
        self.user.save()
        customer = Customer.objects.create(user=self.user, customer_id="cus_manual")
        self.webhook({"id": "e", "type": "customer.subscription.deleted",
                      "data": {"object": subscription("sub_old", customer.customer_id, "canceled", "pro", 600)}})
        self.user.refresh_from_db()
        self.assertEqual(self.user.plan, "pro")  # an admin's grant isn't Stripe's to end

    def test_enterprise_invoiced_subscription(self):
        from workspaces.models import Workspace

        self.set_up_stripe()
        ws = Workspace.objects.filter(memberships__user=self.user).first()
        self.user.is_staff = True
        self.user.save()
        res = self.client.post(f"/api/admin/workspaces/{ws.id}/stripe/", {
            "amount": "1200", "interval": "year", "email": "billing@acme.example", "name": "ACME S.r.l.",
            "country": "it", "postal_code": "40121", "city": "Bologna", "line1": "Via X 1", "vat_id": "IT01234567890",
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        sub_call = next(c[2] for c in self.stripe.calls if c[1] == "subscriptions")
        self.assertEqual(sub_call["collection_method"], "send_invoice")
        self.assertEqual(sub_call["items[0][price_data][unit_amount]"], "120000")
        self.assertEqual(sub_call["automatic_tax[enabled]"], "true")
        cust_call = next(c[2] for c in self.stripe.calls if c[1] == "customers")
        self.assertEqual(cust_call["tax_id_data[0][value]"], "IT01234567890")
        ws.refresh_from_db()
        self.assertTrue(ws.is_enterprise)
        self.assertEqual(ws.plan_source, "stripe")
        row = self.client.get("/api/admin/workspaces/").data[0]
        self.assertEqual(row["subscription"]["amount"], 120000)
        res = self.client.delete(f"/api/admin/workspaces/{ws.id}/stripe/")
        self.assertTrue(res.data["cancel_at_period_end"])

    def test_pay_link_on_my_invoice(self):
        from tasks.models import Tag, Task
        from timetracking.models import TimeEntry

        self.user.plan = "pro"
        self.user.save()
        ws = self.client.get("/api/workspaces/").data[0]["id"]
        base = f"/api/workspaces/{ws}"
        bad = self.client.patch("/api/my/invoicing/", {"stripe_key": "sk_bad"}, format="json")
        self.assertEqual(bad.status_code, 400)
        res = self.client.patch("/api/my/invoicing/", {"stripe_key": "rk_test_user"}, format="json")
        self.assertTrue(res.data["profile"]["stripe_connected"])
        self.assertEqual(res.data["profile"]["stripe_account"], "Studio Rossi")
        self.assertNotIn("stripe_key", res.data["profile"])
        self.client.patch(f"{base}/billing/", {"enabled": True, "default_rate": "100", "tax_rate": "22"}, format="json")
        task = self.client.post(f"{base}/tasks/", {"title": "Work"}, format="json").data
        start = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        self.client.post(f"{base}/time-entries/", {"task": task["id"], "started_at": start.isoformat(),
                         "ended_at": (start + timedelta(hours=2)).isoformat()}, format="json")
        day = start.date().isoformat()
        inv = self.client.post(f"{base}/invoices/", {"period_start": day, "period_end": day}, format="json").data
        res = self.client.post(f"{base}/invoices/{inv['id']}/payment-link/")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["url"], "https://buy.stripe.com/test_1")
        price = next(c for c in self.stripe.calls if c[1] == "prices")
        self.assertEqual(price[2]["unit_amount"], "24400")  # 200 € + 22 %
        self.assertEqual(price[3]["Authorization"], "Bearer rk_test_user")  # the user's own account
        self.assertEqual(self.client.post(f"{base}/invoices/{inv['id']}/payment-link/").data["url"], res.data["url"])
        self.assertEqual(InvoicePayment.objects.count(), 1)
        self.assertIsNone(self.client.get(f"{base}/invoices/{inv['id']}/payment-link/").data["paid_at"])
        self.stripe.sessions_paid = True
        state = self.client.get(f"{base}/invoices/{inv['id']}/payment-link/").data
        self.assertIsNotNone(state["paid_at"])
        self.assertEqual(state["invoice_status"], "paid")
        self.assertEqual(self.client.get(f"{base}/invoices/{inv['id']}/").data["payment"]["url"], res.data["url"])
        from concierge.models import Notification

        self.assertTrue(Notification.objects.filter(user=self.user, title__contains="paid online").exists())


    def test_pro_plus_checkout_and_upgrade_from_pro(self):
        self.set_up_stripe()
        plans = self.client.get("/api/billing/plan/").data["plans"]
        self.assertEqual(plans["pro_plus"]["month"]["amount"], 1000)
        self.assertEqual(plans["pro_plus"]["year"]["amount"], 10000)
        self.client.post("/api/billing/checkout/", {"interval": "month", "plan": "pro"}, format="json")
        customer = Customer.objects.get(user=self.user)
        pro = subscription("sub_pro", customer.customer_id, "active", "pro", 600)
        self.stripe.subs["sub_pro"] = pro
        self.webhook({"id": "e1", "type": "customer.subscription.created", "data": {"object": pro}})
        self.user.refresh_from_db()
        self.assertEqual(self.user.plan, "pro")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.client.post('/api/auth/login/', {'email': 'pay@example.com', 'password': 's3cure-Passw0rd'}).data['access']}")
        res = self.client.post("/api/billing/checkout/", {"interval": "year", "plan": "pro_plus"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        session = [c[2] for c in self.stripe.calls if c[1] == "checkout/sessions"][-1]
        self.assertEqual(session["subscription_data[metadata][kind]"], "pro_plus")
        plus = subscription("sub_plus", customer.customer_id, "active", "pro_plus", 10000, "year", days=365)
        self.webhook({"id": "e2", "type": "customer.subscription.created", "data": {"object": plus}})
        self.user.refresh_from_db()
        self.assertEqual(self.user.plan, "pro_plus")
        self.assertTrue(self.user.has_pro_plus and self.user.has_pro)
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "subscriptions/sub_pro" for c in self.stripe.calls))  # old Pro ended
        self.assertEqual(Subscription.objects.get(subscription_id="sub_pro").status, "canceled")
