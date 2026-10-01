from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import ApiToken, User
from tasks.models import Task


class McpTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        auth = self.api.post("/api/auth/register/", {"email": "a@example.com", "password": "s3cure-Passw0rd", "full_name": "Ann"}).data
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        res = self.api.post("/api/auth/tokens/", {"name": "Claude Code"})
        self.assertEqual(res.status_code, 201)
        self.raw = res.data["token"]
        self.token_id = res.data["id"]
        self.assertTrue(self.raw.startswith("flw_"))
        self.assertNotIn("token", self.api.get("/api/auth/tokens/").data[0])

    def rpc(self, method, params=None, id_=1, token=None):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if id_ is not None:
            body["id"] = id_
        client = APIClient()
        return client.post("/api/mcp/", body, format="json", HTTP_AUTHORIZATION=f"Bearer {token or self.raw}")

    def test_handshake_and_tools(self):
        res = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(res.json()["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(res.json()["result"]["serverInfo"]["name"], "flowly")
        self.assertEqual(self.rpc("notifications/initialized", id_=None).status_code, 202)
        names = [t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]]
        self.assertIn("create_task", names)
        self.assertNotIn("remember", names)

        out = self.rpc("tools/call", {"name": "create_task", "arguments": {"title": "Ship MCP", "day": "today"}}).json()["result"]
        self.assertFalse(out["isError"], out)
        self.assertIn("Ship MCP", out["content"][0]["text"])
        task = Task.objects.get(title="Ship MCP")
        self.assertEqual(task.assignee.email, "a@example.com")
        out = self.rpc("tools/call", {"name": "list_tasks", "arguments": {}}).json()["result"]
        self.assertIn(f"#{task.id} Ship MCP", out["content"][0]["text"])
        out = self.rpc("tools/call", {"name": "complete_task", "arguments": {"task_id": 99999}}).json()["result"]
        self.assertTrue(out["isError"])
        self.assertEqual(self.rpc("tools/call", {"name": "rm_rf"}).json()["error"]["code"], -32602)
        self.assertEqual(self.rpc("resources/list").json()["error"]["code"], -32601)

    def test_auth(self):
        res = APIClient().post("/api/mcp/", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertIn("Bearer", res["WWW-Authenticate"])
        self.assertEqual(self.rpc("ping", token="flw_nope").status_code, 401)
        # Tokens also work on the REST API, and revoking them stops access.
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        self.assertEqual(client.get("/api/auth/me/").data["email"], "a@example.com")
        self.api.delete(f"/api/auth/tokens/{self.token_id}/")
        self.assertEqual(self.rpc("ping").status_code, 401)
        self.assertFalse(ApiToken.objects.exists())
        self.assertTrue(User.objects.exists())
