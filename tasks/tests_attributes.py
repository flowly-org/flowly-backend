from unittest import mock

from rest_framework.test import APITestCase

from accounts.models import User
from tasks import attributes
from tasks.models import Attribute, Task
from workspaces.models import Membership


class AttributeTests(APITestCase):
    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "attr@example.com", "password": "s3cure-Passw0rd", "full_name": "Ada"})
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        self.user = User.objects.get(email="attr@example.com")
        self.ws = self.client.post("/api/workspaces/", {"name": "Studio"}).data["id"]
        self.base = f"/api/workspaces/{self.ws}"
        self.acme = self.client.post(f"{self.base}/projects/", {"name": "ACME"}).data["id"]
        self.site = self.client.post(f"{self.base}/projects/", {"name": "Site"}).data["id"]

    def attr(self, key, value, project=None, expect=201):
        res = self.client.post(f"{self.base}/attributes/", {"key": key, "value": value, "project": project}, format="json")
        self.assertEqual(res.status_code, expect, res.data)
        return res.data

    def test_keys_are_cleaned_and_checked(self):
        self.assertEqual(self.attr("Client e-mail", "x")["key"], "client_e_mail")
        self.attr("tomorrow", "x", expect=400)  # @tomorrow is a date in task lists
        self.attr("9lives", "x", expect=400)
        self.attr("client_e_mail", "y", expect=400)  # same key, same place
        self.attr("client_e_mail", "y", project=self.acme)  # a project may override it
        other = self.client.post("/api/workspaces/", {"name": "Other"}).data["id"]
        foreign = self.client.post(f"/api/workspaces/{other}/projects/", {"name": "X"}).data["id"]
        self.attr("k", "v", project=foreign, expect=400)

    def test_placeholders_project_overrides_workspace_and_template_kept(self):
        self.attr("client_email", "office@studio.example")
        self.attr("client_email", "anna@acme.example", project=self.acme)
        self.attr("contract", "Retainer 2026: 20 h/month, invoices by the 5th, net 30.", project=self.acme)
        t = self.client.post(f"{self.base}/tasks/", {"title": "Send the invoice to {{client_email}}", "project": self.acme,
                                                     "description": "Terms: {{contract}} Ask @contract owner. {{unknown}}"}, format="json").data
        self.assertEqual(t["title"], "Send the invoice to anna@acme.example")
        self.assertEqual(t["title_template"], "Send the invoice to {{client_email}}")
        self.assertEqual(t["description"], "Terms: Retainer 2026: 20 h/month, invoices by the 5th, net 30. Ask @contract owner. {{unknown}}")
        self.assertEqual(t["mentions"], [{"key": "contract", "value": "Retainer 2026: 20 h/month, invoices by the 5th, net 30."}])
        self.assertEqual(Task.objects.get(pk=t["id"]).title, "Send the invoice to {{client_email}}")  # stored raw
        # Another project falls back to the workspace's value; editing the attribute updates the task.
        other = self.client.post(f"{self.base}/tasks/", {"title": "Mail {{client_email}}", "project": self.site}, format="json").data
        self.assertEqual(other["title"], "Mail office@studio.example")
        attr = Attribute.objects.get(project_id=self.acme, key="client_email")
        self.client.patch(f"{self.base}/attributes/{attr.pk}/", {"value": "billing@acme.example"}, format="json")
        self.assertEqual(self.client.get(f"{self.base}/tasks/{t['id']}/").data["title"], "Send the invoice to billing@acme.example")
        # Plain tasks are untouched; list views render too, with one attribute query per workspace.
        plain = self.client.post(f"{self.base}/tasks/", {"title": "Call Bob"}, format="json").data
        self.assertNotIn("title_template", plain)
        titles = {x["title"] for x in self.client.get(f"{self.base}/tasks/").data}
        self.assertIn("Send the invoice to billing@acme.example", titles)

    def test_only_managers_write_and_flo_sees_them(self):
        from concierge.tools import Toolbox, describe
        from delegation.engine import describe_task

        self.attr("tone", "Formal, Italian, no emojis.")
        self.attr("contract", "Retainer 2026, net 30.", project=self.acme)
        task = Task.objects.create(workspace_id=self.ws, project_id=self.acme, title="Draft the renewal @contract in our {{tone}}",
                                   status=Task.objects.model._meta.get_field("status").related_model.objects.filter(workspace_id=self.ws, group__kind="status").first(),
                                   created_by=self.user)
        from datetime import date

        line = describe(task, date.today())
        self.assertIn("Draft the renewal @contract in our Formal, Italian, no emojis.", line)
        self.assertIn("uses attributes: - tone: Formal, Italian, no emojis. - contract: Retainer 2026, net 30.", line)
        agent = describe_task(task)
        self.assertIn("Context the team attached", agent)
        self.assertIn("- contract: Retainer 2026, net 30.", agent)
        overview = Toolbox(self.user, None, date.today()).tool_list_workspaces()
        self.assertIn("attributes of Studio: tone=Formal, Italian, no emojis.", overview)
        self.assertIn("attributes of project ACME: contract=Retainer 2026, net 30.", overview)
        bob = User.objects.create_user(email="bob@example.com", password="s3cure-Passw0rd")
        Membership.objects.create(workspace_id=self.ws, user=bob)
        self.client.force_authenticate(bob)
        self.assertEqual(len(self.client.get(f"{self.base}/attributes/").data), 2)
        self.attr("x", "y", expect=403)

    def test_render_helpers(self):
        values = {"a": "line one\nline two " + "z" * 200}
        self.assertEqual(attributes.render("{{ a }}!", {"a": "ok"}), "ok!")
        self.assertTrue(attributes.render("T {{a}}", values, 20).endswith("…"))
        self.assertNotIn("\n", attributes.render("T {{a}}", values, 200))
        self.assertEqual(attributes.mentioned("mail me@a.com and @a, @a again", values), {"a": values["a"]})  # emails aren't mentions

    def test_integrations_send_filled_in_text_and_track_raw_changes(self):
        from integrations.events import snapshot, task_payload

        self.attr("client_email", "anna@acme.example", project=self.acme)
        t = Task.objects.get(pk=self.client.post(f"{self.base}/tasks/", {"title": "Mail {{client_email}}", "project": self.acme}, format="json").data["id"])
        self.assertEqual(task_payload(t)["title"], "Mail anna@acme.example")
        self.assertEqual(snapshot(t)["title"], "Mail {{client_email}}")  # change detection works on the stored text
