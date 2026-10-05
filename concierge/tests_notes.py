from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from concierge.tests import FakeWorld
from integrations.models import Integration
from tasks.models import Task
from workspaces.models import Membership


@override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="test/model", EMAIL_INBOUND_DOMAIN="in.flowly.test")
class NotesAndEmailTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "marta@example.com", "password": "s3cure-Passw0rd", "full_name": "Marta Bianchi"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.user = User.objects.get(email="marta@example.com")
        self.user.plan = "pro"
        self.user.save()
        self.ws = self.client.post("/api/workspaces/", {"name": "Agency"}).data["id"]
        self.site = self.client.post(f"/api/workspaces/{self.ws}/projects/", {"name": "Website"}, format="json").data["id"]
        self.luca = User.objects.create_user(email="luca.rossi@example.com", password="s3cure-Passw0rd", full_name="Luca Rossi")
        Membership.objects.create(workspace_id=self.ws, user=self.luca)

    def test_meeting_notes_to_bulk_text_and_assignees(self):
        world = FakeWorld(llm_replies=[{"role": "assistant", "content":
            "- Send the offer to Rossi Srl =Luca-Rossi @2030-01-10 +Website\n  they asked for two options\n- Book the venue =Marta-Bianchi ~30m"}])
        with mock.patch("integrations.http.requests.request", world):
            res = self.client.post(f"/api/workspaces/{self.ws}/tasks/from-notes/", {"text": "Weekly sync. Luca will send the offer by the 10th. Marta books the venue."}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        system = world.llm_calls[0]["json"]["messages"][0]["content"]
        self.assertIn("Luca-Rossi", system)
        self.assertIn("Things Marta Bianchi (the person importing this) must do", system)
        from tasks.bulk import create_from_text
        from datetime import date
        from workspaces.models import Workspace

        tasks = create_from_text(self.user, Workspace.objects.get(pk=self.ws), res.data["text"], date(2030, 1, 6))
        offer, venue = tasks
        self.assertEqual((offer.assignee, str(offer.planned_date), offer.project_id, offer.description), (self.luca, "2030-01-10", self.site, "they asked for two options"))
        self.assertEqual((venue.assignee, venue.estimate_minutes), (self.user, 30))
        self.assertEqual(self.client.post(f"/api/workspaces/{self.ws}/tasks/from-notes/", {"text": "hi"}, format="json").status_code, 400)

    def email(self, inbox, body, subject="Next steps", message_id="<m1@x>"):
        self.client.credentials()
        return self.client.post("/api/integrations/email/inbound/", {
            "to": f"{inbox['token']}@in.flowly.test", "from": "Marta <marta@example.com>", "subject": subject,
            "text": body, "message_id": message_id}, format="json")

    def test_email_ai_splits_long_mail_and_falls_back(self):
        inbox = self.client.post(f"/api/workspaces/{self.ws}/integrations/", {"kind": "email", "name": "In", "config": {"ai": "auto"}}, format="json").data
        self.assertEqual(inbox["config"]["ai"], "auto")
        long_body = "Hi team,\n" + "Following our call, here is what we agreed. " * 15 + "\nLuca: please update the homepage. I'll prepare the invoice."
        world = FakeWorld(llm_replies=[{"role": "assistant", "content": "- Update the homepage =Luca-Rossi +Website\n- Prepare the invoice =Marta-Bianchi"}])
        with mock.patch("integrations.http.requests.request", world):
            self.email(inbox, long_body)
            self.email(inbox, long_body)  # provider retry: not imported twice
        tasks = {t.title: t for t in Task.objects.filter(workspace_id=self.ws)}
        self.assertEqual(set(tasks), {"Update the homepage", "Prepare the invoice"})
        self.assertEqual(tasks["Update the homepage"].assignee, self.luca)
        self.assertIn("from an email by marta@example.com: “Next steps”", tasks["Update the homepage"].description)
        self.assertEqual(len(world.llm_calls), 1)
        delivery = Integration.objects.get(kind="email").deliveries.order_by("id")
        self.assertEqual([d.status for d in delivery], ["success", "ignored"])

        # Short email: a single task, no AI. Nothing actionable: also a single task.
        Task.objects.all().delete()
        world = FakeWorld(llm_replies=[{"role": "assistant", "content": "NONE"}])
        with mock.patch("integrations.http.requests.request", world):
            self.email(inbox, "Buy milk", subject="Milk", message_id="<m2@x>")
            self.email(inbox, long_body, subject="FYI", message_id="<m3@x>")
        self.assertEqual(sorted(Task.objects.values_list("title", flat=True)), ["FYI", "Milk"])
        self.assertEqual(len(world.llm_calls), 1)

        # Without Pro (and no workspace AI provider): plain single task.
        self.user.plan = "free"
        self.user.save()
        with mock.patch("integrations.http.requests.request", FakeWorld()):
            self.email(inbox, long_body, subject="Plain", message_id="<m4@x>")
        self.assertTrue(Task.objects.filter(title="Plain").exists())
