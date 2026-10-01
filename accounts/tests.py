from rest_framework.test import APITestCase


class GuideTests(APITestCase):
    def test_progress_follows_what_the_user_sets_up(self):
        auth = self.client.post(
            "/api/auth/register/", {"email": "g@example.com", "password": "s3cure-Passw0rd", "full_name": "G"}
        ).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        res = self.client.get("/api/my/guide/").data
        self.assertEqual([s["key"] for s in res["steps"]][:3], ["task", "project", "tags"])
        done = {s["key"] for s in res["steps"] if s["done"]}
        before = res["done"]
        self.assertNotIn("task", done)
        self.assertNotIn("home", done)
        ws = self.client.get("/api/workspaces/").data[0]["id"]
        self.client.post(f"/api/workspaces/{ws}/tasks/", {"title": "First"}, format="json")
        self.client.patch("/api/auth/me/", {"home_location": "Bologna, Italy"}, format="json")
        res = self.client.get("/api/my/guide/").data
        done = {s["key"] for s in res["steps"] if s["done"]}
        self.assertTrue({"task", "home"} <= done)
        self.assertEqual(res["done"], before + 2)
        self.assertEqual(res["total"], 10)
