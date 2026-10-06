from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import CodeRedemption, InviteCode, User


class InviteCodeTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="admin@example.com", password="s3cure-Passw0rd", is_staff=True)

    def register(self, email, code=None):
        data = {"email": email, "password": "s3cure-Passw0rd", "full_name": "X"}
        if code is not None:
            data["code"] = code
        self.client.credentials()
        return self.client.post("/api/auth/register/", data, format="json")

    def test_admin_creates_code_and_people_register_with_it(self):
        self.client.force_authenticate(self.admin)
        res = self.client.post("/api/admin/codes/", {"plan": "pro_plus", "max_uses": 2, "note": "Beta testers"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        code = res.data["code"]
        self.assertRegex(code, r"^FLOWLY-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertIn(f"/register?code={code}", res.data["link"])
        self.client.force_authenticate(None)

        check = self.client.get(f"/api/auth/code/?code={code.lower()}").data
        self.assertEqual((check["valid"], check["plan"], check["forever"]), (True, "pro_plus", True))
        a = self.register("anna@example.com", f" {code.lower()} ")
        self.assertEqual(a.status_code, 201, a.data)
        self.assertTrue(a.data["user"]["has_pro_plus"])
        self.assertEqual((a.data["user"]["plan"], a.data["user"]["plan_source"], a.data["user"]["plan_expires_at"]), ("pro_plus", "code", None))
        self.assertEqual(self.register("bruno@example.com", code).status_code, 201)
        # Used up: the third person is told, and no account is created.
        third = self.register("carla@example.com", code)
        self.assertEqual(third.status_code, 400)
        self.assertIn("already been used", str(third.data["code"]))
        self.assertFalse(User.objects.filter(email="carla@example.com").exists())
        # A wrong code is refused; no code is fine.
        self.assertEqual(self.register("dario@example.com", "NOPE-1234").status_code, 400)
        self.assertEqual(self.register("dario@example.com").status_code, 201)
        self.assertFalse(User.objects.get(email="dario@example.com").has_pro)

        self.client.force_authenticate(self.admin)
        listed = self.client.get("/api/admin/codes/").data[0]
        self.assertEqual((listed["uses"], len(listed["redeemed_by"])), (2, 2))
        self.assertEqual(self.client.delete(f"/api/admin/codes/{listed['id']}/").status_code, 400)  # used: switch off instead
        self.client.patch(f"/api/admin/codes/{listed['id']}/", {"active": False}, format="json")
        self.client.force_authenticate(None)
        self.assertFalse(self.client.get(f"/api/auth/code/?code={code}").data["valid"])
        self.assertTrue(User.objects.get(email="anna@example.com").has_pro_plus)  # what people got stays

    def test_only_staff_manage_codes_and_expiry_rules(self):
        user = User.objects.create_user(email="u@example.com", password="s3cure-Passw0rd")
        self.client.force_authenticate(user)
        self.assertEqual(self.client.post("/api/admin/codes/", {}, format="json").status_code, 403)
        expired = InviteCode.objects.create(code="OLD-CODE-1", valid_until=timezone.now() - timedelta(days=1))
        self.assertEqual(self.client.post("/api/auth/redeem/", {"code": expired.code}, format="json").status_code, 400)
        trial = InviteCode.objects.create(code="TRIAL-30", plan="pro", days=30)
        res = self.client.post("/api/auth/redeem/", {"code": "trial-30"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        user.refresh_from_db()
        self.assertEqual(user.plan, "pro")
        self.assertAlmostEqual((user.plan_expires_at - timezone.now()).days, 29, delta=1)
        self.assertEqual(self.client.post("/api/auth/redeem/", {"code": "TRIAL-30"}, format="json").status_code, 400)  # once per person

    def test_stripe_never_takes_a_forever_code_away(self):
        from payments import services
        from payments.models import Customer, Subscription

        code = InviteCode.objects.create(code="FRIENDS-FOREVER")
        user = User.objects.create_user(email="f@example.com", password="s3cure-Passw0rd")
        customer = Customer.objects.create(user=user, customer_id="cus_1")
        sub = Subscription.objects.create(customer=customer, subscription_id="sub_1", kind="pro", status="active",
                                          current_period_end=timezone.now() + timedelta(days=20))
        services.apply(customer)
        user.refresh_from_db()
        self.assertEqual((user.plan, user.plan_source), ("pro", "stripe"))
        self.client.force_authenticate(user)
        res = self.client.post("/api/auth/redeem/", {"code": "friends-forever"}, format="json").data
        self.assertTrue(res["stripe_subscription"])  # the UI tells them to cancel the paid plan
        user.refresh_from_db()
        self.assertEqual((user.plan, user.plan_source, user.plan_expires_at), ("pro_plus", "code", None))
        # The Stripe subscription renews or ends: the code's Pro Plus stays.
        services.apply(customer)
        sub.status = "canceled"
        sub.save()
        services.apply(customer)
        user.refresh_from_db()
        self.assertEqual((user.plan, user.plan_source, user.plan_expires_at), ("pro_plus", "code", None))

    def test_code_check_is_rate_limited(self):
        from django.core.cache import cache

        cache.clear()
        for _ in range(30):
            self.client.get("/api/auth/code/?code=GUESS")
        self.assertEqual(self.client.get("/api/auth/code/?code=GUESS").status_code, 429)
        cache.clear()
