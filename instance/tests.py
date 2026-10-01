from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from concierge import telegram
from instance import conf
from instance.models import InstanceSetting


class FakeResponse:
    def __init__(self, data):
        import json

        self.status_code, self._data, self.text = 200, data, json.dumps(data)
        self.content = self.text.encode()

    def json(self):
        return self._data


class AdminPanelTests(APITestCase):
    def setUp(self):
        conf.clear_cache()
        self.admin = self.client.post("/api/auth/register/", {"email": "root@example.com", "password": "s3cure-Passw0rd"}).data
        User.objects.filter(email="root@example.com").update(is_staff=True)
        self.user = self.client.post("/api/auth/register/", {"email": "u@example.com", "password": "s3cure-Passw0rd"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin['access']}")

    def tearDown(self):
        conf.clear_cache()

    def test_non_staff_forbidden(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.user['access']}")
        self.assertEqual(self.client.get("/api/admin/settings/").status_code, 403)

    @override_settings(OPENROUTER_MODEL="env/model")
    def test_settings_override_mask_and_revert(self):
        rows = {r["key"]: r for r in self.client.get("/api/admin/settings/").data}
        self.assertEqual((rows["OPENROUTER_MODEL"]["value"], rows["OPENROUTER_MODEL"]["source"]), ("env/model", "environment"))

        with mock.patch("integrations.http.requests.request", return_value=FakeResponse({"ok": True, "result": True})):
            res = self.client.patch("/api/admin/settings/", {"OPENROUTER_MODEL": "admin/model", "TELEGRAM_BOT_TOKEN": "123:secret-token-xyz"}, format="json")
        rows = {r["key"]: r for r in res.data}
        self.assertEqual((rows["OPENROUTER_MODEL"]["value"], rows["OPENROUTER_MODEL"]["source"]), ("admin/model", "admin"))
        self.assertEqual(rows["TELEGRAM_BOT_TOKEN"]["value"], "…-xyz")
        self.assertTrue(InstanceSetting.objects.get(key="TELEGRAM_BOT_TOKEN").value.startswith("enc:"))
        self.assertEqual(conf.get("TELEGRAM_BOT_TOKEN"), "123:secret-token-xyz")
        self.assertTrue(telegram.enabled())

        self.assertEqual(self.client.patch("/api/admin/settings/", {"TELEGRAM_MODE": "carrier-pigeon"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch("/api/admin/settings/", {"PUBLIC_URL": "flowly.net"}, format="json").status_code, 400)
        self.client.patch("/api/admin/settings/", {"OPENROUTER_MODEL": None}, format="json")
        self.assertEqual(conf.get("OPENROUTER_MODEL"), "env/model")

    def test_users_and_plans(self):
        users = self.client.get("/api/admin/users/?q=u@").data
        self.assertEqual([u["email"] for u in users], ["u@example.com"])
        res = self.client.patch(f"/api/admin/users/{users[0]['id']}/", {"plan": "pro"}, format="json")
        self.assertEqual(res.data["plan"], "pro")
        self.assertTrue(User.objects.get(email="u@example.com").has_pro)
        me = User.objects.get(email="root@example.com")
        self.assertEqual(self.client.patch(f"/api/admin/users/{me.id}/", {"is_staff": False}, format="json").status_code, 400)

    def test_overview_and_checks(self):
        self.assertIn("users", self.client.get("/api/admin/overview/").data)
        res = self.client.post("/api/admin/check/openrouter/").data
        self.assertFalse(res["ok"])
        self.assertIn("No OpenRouter API key", res["message"])


class GitHubAppAdminTests(APITestCase):
    def setUp(self):
        conf.clear_cache()
        self.admin = self.client.post("/api/auth/register/", {"email": "root@example.com", "password": "s3cure-Passw0rd"}).data
        User.objects.filter(email="root@example.com").update(is_staff=True)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin['access']}")

    def test_connect_verify_and_remove(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from integrations.models import GitHubApp

        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.assertFalse(self.client.get("/api/admin/github-app/").data["configured"])
        self.assertEqual(self.client.put("/api/admin/github-app/", {"app_id": "1"}, format="json").status_code, 400)

        def fake(method, url, **kw):
            if url.endswith("/app"):
                return FakeResponse({"slug": "flowly-acme", "name": "Flowly ACME", "owner": {"login": "acme"},
                                     "html_url": "https://github.com/apps/flowly-acme"})
            return FakeResponse([{"id": 5, "account": {"login": "acme", "type": "Organization"}, "repository_selection": "selected"}])

        body = {"app_id": "123", "slug": "x", "client_id": "Iv1.abc", "client_secret": "s", "webhook_secret": "w", "private_key": pem}
        with mock.patch("integrations.http.requests.request", side_effect=fake):
            res = self.client.put("/api/admin/github-app/", body, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data["app"]["slug"], res.data["app"]["owner"]), ("flowly-acme", "acme"))
        self.assertEqual(res.data["installations"][0]["account"], "acme")
        self.assertNotIn("private_key", res.data["app"])
        self.assertTrue(res.data["webhook_url"].endswith("/api/integrations/github/webhook/"))

        # Updating only the webhook secret keeps the rest.
        with mock.patch("integrations.http.requests.request", side_effect=fake):
            self.client.put("/api/admin/github-app/", {"webhook_secret": "new"}, format="json")
        self.assertEqual(GitHubApp.objects.get().webhook_secret, "new")

        with mock.patch("integrations.http.requests.request", return_value=FakeResponse({"message": "Bad credentials"})) as m:
            m.return_value.status_code = 401
            res = self.client.put("/api/admin/github-app/", {"private_key": pem}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.client.delete("/api/admin/github-app/").status_code, 204)
        self.assertFalse(GitHubApp.objects.exists())
