"""Against a real Donetick (skipped unless DONETICK_LIVE_URL and DONETICK_LIVE_TOKEN are set)."""

import json
import os
import unittest
from datetime import timedelta

import requests
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from tasks.models import Task

from . import engine
from .models import DonetickOutbox

URL, TOKEN = os.environ.get("DONETICK_LIVE_URL"), os.environ.get("DONETICK_LIVE_TOKEN")
HOOKS = os.environ.get("DONETICK_LIVE_HOOKS")


@unittest.skipUnless(URL and TOKEN, "no live Donetick")
@override_settings(INTEGRATIONS_ALLOW_PRIVATE_URLS=True)
class LiveDonetickTests(APITestCase):
    def setUp(self):
        auth = self.client.post("/api/auth/register/", {"email": "marta@example.com", "password": "s3cure-Passw0rd", "full_name": "Marta"}).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.home = self.client.post("/api/workspaces/", {"name": "Home"}).data["id"]
        self.garden = self.client.post(f"/api/workspaces/{self.home}/projects/", {"name": "Garden"}, format="json").data["id"]

    def dt(self, method, path, body=None):
        res = requests.request(method, URL + path, json=body, headers={"secretkey": TOKEN}, timeout=10)
        res.raise_for_status()
        return res.json()

    def test_round_trip(self):
        integration = self.client.post(f"/api/workspaces/{self.home}/integrations/", {
            "kind": "donetick", "name": "Donetick", "api_key": TOKEN,
            "config": {"base_url": URL, "label_projects": {"garden": self.garden}, "create_chores": True},
        }, format="json").data
        res = self.client.post(f"/api/workspaces/{self.home}/integrations/{integration['id']}/test/").data
        print("sync:", res["status"], res["response_body"] or res["last_error"])
        tasks = {t.title: t for t in Task.objects.filter(workspace_id=self.home)}
        print("tasks:", {k: (str(v.planned_date), v.project_id == self.garden, v.assignee_id is not None) for k, v in tasks.items()})
        water = tasks["Water the plants"]
        self.assertEqual(water.project_id, self.garden)
        self.assertIsNotNone(water.assignee_id)

        done = water.workspace.tags.get(group__kind="status", is_done=True)
        self.client.patch(f"/api/workspaces/{self.home}/tasks/{water.id}/", {"status": done.id}, format="json")
        trash = tasks["Take out the trash"]
        self.client.patch(f"/api/workspaces/{self.home}/tasks/{trash.id}/", {"title": "Take out the trash and glass"}, format="json")
        new = self.client.post(f"/api/workspaces/{self.home}/tasks/", {"title": "Buy light bulbs", "planned_date": str(timezone.localdate())}, format="json").data
        engine.send_outbox()
        errors = list(DonetickOutbox.objects.exclude(error="").values_list("action", "error"))
        self.assertEqual(errors, [])
        chores = {c["name"]: c for c in self.dt("GET", "/eapi/v1/chore")}
        print("donetick:", {k: c["nextDueDate"] for k, c in chores.items()})
        self.assertIn("Take out the trash and glass", chores)
        self.assertIn("Buy light bulbs", chores)
        nxt = Task.objects.get(workspace_id=self.home, title="Water the plants", status__is_done=False)
        self.assertEqual(nxt.planned_date, water.planned_date + timedelta(days=7))

        # Completed in Donetick → ticked on the next poll (one-off chores leave the list).
        self.dt("POST", f"/eapi/v1/chore/{chores['Take out the trash and glass']['id']}/complete")
        engine.poll(engine.Source(integration=water.workspace.integrations.get()))
        trash.refresh_from_db()
        self.assertTrue(trash.status.is_done)
        self.assertEqual(Task.objects.filter(title="Buy light bulbs").count(), 1)

        # Donetick's own webhook for the completion we made: must not close the new occurrence.
        if HOOKS and os.path.exists(HOOKS):
            hooks = [json.loads(line) for line in open(HOOKS) if line.strip()]
            completed = [h for h in hooks if h["type"] == "task.completed" and h["data"]["chore"]["name"] == "Water the plants"]
            print("webhooks seen:", [h["type"] for h in hooks])
            self.assertTrue(completed)
            self.client.credentials()
            res = self.client.post(f"/api/hooks/donetick/{integration['token']}/", completed[-1], format="json")
            self.assertEqual(res.status_code, 200)
            nxt.refresh_from_db()
            self.assertFalse(nxt.status.is_done)
        self.assertTrue(Task.objects.filter(pk=new["id"]).exists())
