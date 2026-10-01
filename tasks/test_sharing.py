from rest_framework.test import APITestCase

from concierge.models import Notification
from workspaces.models import Membership


class SharingTests(APITestCase):
    def setUp(self):
        reg = lambda e: self.client.post("/api/auth/register/", {"email": e, "password": "s3cure-Passw0rd", "full_name": e[0].upper()}).data
        self.alice, self.bob, self.carol = reg("alice@example.com"), reg("bob@example.com"), reg("carol@example.com")
        self.as_user(self.alice)
        self.ws = self.client.post("/api/workspaces/", {"name": "Team"}).data
        Membership.objects.create(workspace_id=self.ws["id"], user_id=self.bob["user"]["id"])
        self.base = f"/api/workspaces/{self.ws['id']}"

    def as_user(self, auth):
        if auth is None:
            self.client.credentials()
        else:
            self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")

    def task(self, **data):
        return self.client.post(f"{self.base}/tasks/", {"title": "T", **data}, format="json").data

    def test_private_tasks_only_for_creator_and_assignee(self):
        secret = self.task(title="Surprise party", visibility="private")
        mine_for_bob = self.task(title="For Bob", visibility="private", assignee=self.bob["user"]["id"])
        self.assertEqual(len(secret["ref"]), 10)
        self.as_user(self.bob)
        titles = [t["title"] for t in self.client.get(f"{self.base}/tasks/").data]
        self.assertEqual(titles, ["For Bob"])
        self.assertEqual(self.client.get(f"{self.base}/tasks/{secret['id']}/").status_code, 404)
        self.assertEqual(self.client.get(f"{self.base}/tasks/{secret['id']}/comments/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/t/{secret['ref']}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/t/{mine_for_bob['ref']}/").data["access"], "member")

    def test_public_link_is_read_only_and_hides_emails(self):
        t = self.task(title="Roadmap", visibility="public", description="Q4 plan")
        self.client.post(f"{self.base}/tasks/{t['id']}/comments/", {"body": "Looks good"}, format="json")
        self.as_user(None)
        res = self.client.get(f"/api/t/{t['ref']}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.data["access"], res.data["can_comment"], res.data["task"]["title"]), ("public", False, "Roadmap"))
        self.assertNotIn("id", res.data["workspace"])
        self.assertEqual(res.data["comments"][0]["author"]["email"], "")
        self.assertEqual(self.client.post(f"/api/t/{t['ref']}/comments/", {"body": "spam"}, format="json").status_code, 401)

    def test_team_link_requires_sign_in_and_membership(self):
        t = self.task(title="Team only")
        self.as_user(None)
        self.assertEqual(self.client.get(f"/api/t/{t['ref']}/").status_code, 401)
        self.as_user(self.carol)
        self.assertEqual(self.client.get(f"/api/t/{t['ref']}/").status_code, 404)
        self.assertEqual(self.client.get("/api/t/nope/").status_code, 404)

    def test_sharing_with_people(self):
        t = self.task(title="Contract review")
        res = self.client.post(f"{self.base}/tasks/{t['id']}/shares/", {"email": "Carol@example.com"}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.data["registered"])
        self.client.post(f"{self.base}/tasks/{t['id']}/shares/", {"email": "dave@example.com"}, format="json")
        self.assertEqual(self.client.get(f"{self.base}/tasks/{t['id']}/").data["visibility"], "shareable")
        self.assertTrue(Notification.objects.filter(user__email="carol@example.com", kind="shared").exists())

        self.as_user(self.carol)
        res = self.client.get(f"/api/t/{t['ref']}/")
        self.assertEqual((res.data["access"], res.data["can_comment"], res.data["can_edit"]), ("shared", True, False))
        self.assertEqual(self.client.post(f"/api/t/{t['ref']}/comments/", {"body": "Signed"}, format="json").status_code, 201)
        self.assertEqual([x["title"] for x in self.client.get("/api/my/shared/").data], ["Contract review"])
        self.assertEqual(self.client.get(f"{self.base}/tasks/{t['id']}/").status_code, 404)  # still not a member

        # Dave signs up later and gets access.
        dave = self.client.post("/api/auth/register/", {"email": "dave@example.com", "password": "s3cure-Passw0rd"}).data
        self.as_user(dave)
        self.assertEqual(self.client.get(f"/api/t/{t['ref']}/").data["access"], "shared")

        # Un-sharing removes access.
        self.as_user(self.alice)
        share_id = next(s["id"] for s in self.client.get(f"{self.base}/tasks/{t['id']}/shares/").data if s["email"] == "carol@example.com")
        self.client.delete(f"{self.base}/tasks/{t['id']}/shares/{share_id}/")
        self.as_user(self.carol)
        self.assertEqual(self.client.get(f"/api/t/{t['ref']}/").status_code, 404)

    def test_people_shared_on_a_public_task_can_still_comment(self):
        t = self.task(title="Launch", visibility="public")
        self.client.post(f"{self.base}/tasks/{t['id']}/shares/", {"email": "carol@example.com"}, format="json")
        self.assertEqual(self.client.get(f"{self.base}/tasks/{t['id']}/").data["visibility"], "public")
        self.as_user(self.carol)
        res = self.client.get(f"/api/t/{t['ref']}/").data
        self.assertEqual((res["access"], res["can_comment"]), ("shared", True))

    def test_bulk_create_is_all_or_nothing(self):
        res = self.client.post(f"{self.base}/tasks/bulk/", {"tasks": [{"title": "One"}, {"title": "Two", "planned_date": "2026-10-02"}]}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual([t["title"] for t in res.data], ["One", "Two"])
        res = self.client.post(f"{self.base}/tasks/bulk/", {"tasks": [{"title": "Three"}, {"title": ""}]}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("1", res.json()["tasks"])
        self.assertEqual(len(self.client.get(f"{self.base}/tasks/").data), 2)
