import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from instance import conf
from mcpauth.models import OAuthGrant


@override_settings(PUBLIC_URL="https://app.flowly.test", API_PUBLIC_URL="https://api.flowly.test")
class OAuthFlowTests(TestCase):
    def setUp(self):
        conf.clear_cache()
        self.web = APIClient()
        auth = self.web.post("/api/auth/register/", {"email": "u@example.com", "password": "s3cure-Passw0rd", "full_name": "U"}).data
        self.web.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.client_app = APIClient()  # the MCP client (ChatGPT / Claude)

    def tearDown(self):
        conf.clear_cache()

    def mcp(self, token, method="tools/list", params=None):
        return self.client_app.post("/api/mcp/", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                                    format="json", HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_discovery(self):
        res = self.client_app.post("/api/mcp/", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, format="json")
        self.assertEqual(res.status_code, 401)
        meta_url = res["WWW-Authenticate"].split('resource_metadata="')[1].split('"')[0]
        self.assertEqual(meta_url, "https://api.flowly.test/.well-known/oauth-protected-resource/api/mcp/")
        prm = self.client_app.get(urlparse(meta_url).path).json()
        self.assertEqual((prm["resource"], prm["authorization_servers"]), ("https://api.flowly.test/api/mcp/", ["https://api.flowly.test"]))
        asm = self.client_app.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(asm["authorization_endpoint"], "https://app.flowly.test/oauth/authorize")
        self.assertEqual(asm["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(self.client_app.options("/api/oauth/token")["Access-Control-Allow-Origin"], "*")

    def test_full_flow(self):
        # 1. Dynamic client registration (public client, like ChatGPT / Claude)
        reg = self.client_app.post("/api/oauth/register", {"client_name": "ChatGPT",
            "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"], "token_endpoint_auth_method": "none"}, format="json")
        self.assertEqual(reg.status_code, 201, reg.content)
        client_id = reg.json()["client_id"]
        self.assertEqual(self.client_app.post("/api/oauth/register", {"redirect_uris": ["http://evil.example/cb"]}, format="json").status_code, 400)

        # 2. Consent (the user, signed in to Flowly)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": client_id, "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
                  "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256",
                  "state": "xyz", "scope": "mcp", "resource": "https://api.flowly.test/api/mcp/"}
        info = self.web.get("/api/oauth/authorize/", params).data
        self.assertEqual(info["client"]["name"], "ChatGPT")
        denied = self.web.post("/api/oauth/authorize/", {**params, "decision": "deny"}, format="json").data["redirect"]
        self.assertIn("error=access_denied", denied)
        redirect = self.web.post("/api/oauth/authorize/", {**params, "decision": "allow"}, format="json").data["redirect"]
        q = parse_qs(urlparse(redirect).query)
        self.assertEqual(q["state"], ["xyz"])
        code = q["code"][0]

        # 3. Token exchange with PKCE (wrong verifier first)
        bad = self.client_app.post("/api/oauth/token", {"grant_type": "authorization_code", "code": code, "client_id": client_id,
            "redirect_uri": params["redirect_uri"], "code_verifier": "wrong" * 10})
        self.assertEqual(bad.json()["error"], "invalid_grant")
        # The code is single-use, so a failed attempt burns it: get a new one.
        redirect = self.web.post("/api/oauth/authorize/", {**params, "decision": "allow"}, format="json").data["redirect"]
        code = parse_qs(urlparse(redirect).query)["code"][0]
        tok = self.client_app.post("/api/oauth/token", {"grant_type": "authorization_code", "code": code, "client_id": client_id,
            "redirect_uri": params["redirect_uri"], "code_verifier": verifier}).json()
        self.assertEqual(tok["token_type"], "Bearer")
        self.assertTrue(tok["access_token"].startswith("flo_"))

        # 4. Use it on MCP; search/fetch are there for ChatGPT
        names = [t["name"] for t in self.mcp(tok["access_token"]).json()["result"]["tools"]]
        self.assertIn("search", names)
        self.assertIn("fetch", names)
        self.mcp(tok["access_token"], "tools/call", {"name": "create_task", "arguments": {"title": "Quarterly report"}})
        import json as _json
        found = _json.loads(self.mcp(tok["access_token"], "tools/call", {"name": "search", "arguments": {"query": "quarterly"}}).json()["result"]["content"][0]["text"])
        self.assertEqual(found["results"][0]["url"].split("/t/")[0], "https://app.flowly.test")
        doc = _json.loads(self.mcp(tok["access_token"], "tools/call", {"name": "fetch", "arguments": {"id": found["results"][0]["id"]}}).json()["result"]["content"][0]["text"])
        self.assertIn("Quarterly report", doc["text"])
        # OAuth access tokens are only for MCP, not the REST API.
        self.assertEqual(APIClient().get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {tok['access_token']}").status_code, 401)

        # 5. Refresh rotates; the old refresh token stops working
        new = self.client_app.post("/api/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id}).json()
        self.assertNotEqual(new["access_token"], tok["access_token"])
        self.assertEqual(self.mcp(tok["access_token"]).status_code, 401)
        again = self.client_app.post("/api/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id})
        self.assertEqual(again.json()["error"], "invalid_grant")

        # 6. The user sees and revokes the connected app
        grants = self.web.get("/api/oauth/grants/").data
        self.assertEqual(grants[0]["client"], "ChatGPT")
        self.web.delete(f"/api/oauth/grants/{grants[0]['id']}/")
        self.assertEqual(self.mcp(new["access_token"]).status_code, 401)
        self.assertTrue(OAuthGrant.objects.get().revoked)

    def test_confidential_client_and_bad_params(self):
        reg = self.client_app.post("/api/oauth/register", {"client_name": "Cursor", "redirect_uris": ["http://127.0.0.1:3334/callback"],
                                                           "token_endpoint_auth_method": "client_secret_post"}, format="json").json()
        self.assertIn("client_secret", reg)
        res = self.web.get("/api/oauth/authorize/", {"client_id": reg["client_id"], "redirect_uri": "http://127.0.0.1:9999/other",
                                                     "response_type": "code", "code_challenge": "x" * 43, "code_challenge_method": "S256"})
        self.assertEqual(res.status_code, 400)
        res = self.web.get("/api/oauth/authorize/", {"client_id": reg["client_id"], "redirect_uri": "http://127.0.0.1:3334/callback",
                                                     "response_type": "code", "code_challenge": "x" * 43, "code_challenge_method": "plain"})
        self.assertEqual(res.status_code, 400)
        tok = self.client_app.post("/api/oauth/token", {"grant_type": "authorization_code", "code": "nope", "client_id": reg["client_id"], "client_secret": "wrong"})
        self.assertEqual(tok.status_code, 401)


@override_settings(PUBLIC_URL="https://app.flowly.test", API_PUBLIC_URL="https://api.flowly.test")
class WorkspaceEndpointTests(TestCase):
    def setUp(self):
        conf.clear_cache()
        self.web = APIClient()
        auth = self.web.post("/api/auth/register/", {"email": "o@example.com", "password": "s3cure-Passw0rd", "full_name": "Olga"}).data
        self.web.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")
        self.team = self.web.post("/api/workspaces/", {"name": "Ops"}).data
        self.personal = next(w for w in self.web.get("/api/workspaces/").data if w["name"] == "Personal")
        from django.utils import timezone

        today = timezone.localdate().isoformat()
        self.web.post(f"/api/workspaces/{self.personal['id']}/tasks/", {"title": "Private dentist", "planned_date": today}, format="json")
        self.web.post(f"/api/workspaces/{self.team['id']}/tasks/", {"title": "Rotate certificates", "planned_date": today}, format="json")

    def tearDown(self):
        conf.clear_cache()

    def call(self, url, token, name, args=None, method="tools/call"):
        params = {"name": name, "arguments": args or {}} if method == "tools/call" else {}
        return APIClient().post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, format="json",
                                HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_service_token_acts_as_bot_in_one_workspace(self):
        res = self.web.post(f"/api/workspaces/{self.team['id']}/mcp-tokens/", {"name": "n8n"})
        self.assertEqual(res.status_code, 201)
        raw = res.data["token"]
        self.assertEqual(res.data["bot"], "Bot: n8n")
        info = self.web.get(f"/api/workspaces/{self.team['id']}/mcp-tokens/").data
        self.assertEqual(info["url"], f"https://api.flowly.test/api/mcp/w/{self.team['id']}/")
        url = f"/api/mcp/w/{self.team['id']}/"
        import json as _json
        out = self.call(url, raw, "list_tasks", {"everyone": True}).json()["result"]["content"][0]["text"]
        self.assertIn("Rotate certificates", out)
        self.assertNotIn("dentist", out)
        created = self.call(url, raw, "create_task", {"title": "Renew domain", "workspace": "Personal"}).json()["result"]["content"][0]["text"]
        self.assertIn("workspace=Ops", created)  # the workspace argument can't escape the scope
        found = _json.loads(self.call(url, raw, "search", {"query": "dentist"}).json()["result"]["content"][0]["text"])
        self.assertEqual(found["results"], [])
        init = self.call(url, raw, None, method="initialize").json()["result"]
        self.assertIn("limited to the workspace “Ops”", init["instructions"])
        # Not usable elsewhere.
        self.assertEqual(self.call("/api/mcp/", raw, "list_tasks").status_code, 401)
        self.assertEqual(self.call(f"/api/mcp/w/{self.personal['id']}/", raw, "list_tasks").status_code, 401)
        # The bot shows up as a member; revoking deactivates it.
        members = [m["user"]["full_name"] for m in self.web.get(f"/api/workspaces/{self.team['id']}/members/").data]
        self.assertIn("Bot: n8n", members)
        self.web.delete(f"/api/workspaces/{self.team['id']}/mcp-tokens/{res.data['id']}/")
        self.assertEqual(self.call(url, raw, "list_tasks").status_code, 401)

    def test_members_use_personal_tokens_on_workspace_endpoints(self):
        from accounts.models import ApiToken, User

        _, raw = ApiToken.issue(User.objects.get(email="o@example.com"), "cli")
        out = self.call(f"/api/mcp/w/{self.team['id']}/", raw, "list_tasks", {"everyone": True}).json()["result"]["content"][0]["text"]
        self.assertIn("Rotate certificates", out)
        self.assertNotIn("dentist", out)
        stranger = self.web.post("/api/auth/register/", {"email": "x@example.com", "password": "s3cure-Passw0rd"}).data
        _, raw2 = ApiToken.issue(User.objects.get(email="x@example.com"), "cli")
        self.assertEqual(self.call(f"/api/mcp/w/{self.team['id']}/", raw2, "list_tasks").status_code, 401)
        self.assertTrue(stranger)

    def test_workspace_resource_metadata_and_oauth_binding(self):
        res = APIClient().post(f"/api/mcp/w/{self.team['id']}/", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, format="json")
        meta = res["WWW-Authenticate"].split('resource_metadata="')[1].split('"')[0]
        self.assertTrue(meta.endswith(f"/.well-known/oauth-protected-resource/api/mcp/w/{self.team['id']}/"))
        prm = APIClient().get(urlparse(meta).path).json()
        self.assertEqual(prm["resource"], f"https://api.flowly.test/api/mcp/w/{self.team['id']}/")
        # OAuth grant bound to the workspace resource only works there.
        reg = APIClient().post("/api/oauth/register", {"client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}, format="json").json()
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": reg["client_id"], "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "response_type": "code",
                  "code_challenge": challenge, "code_challenge_method": "S256", "resource": prm["resource"], "decision": "allow"}
        self.assertEqual(self.web.get("/api/oauth/authorize/", params).data["workspace"]["name"], "Ops")
        code = parse_qs(urlparse(self.web.post("/api/oauth/authorize/", params, format="json").data["redirect"]).query)["code"][0]
        tok = APIClient().post("/api/oauth/token", {"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
                                                    "redirect_uri": params["redirect_uri"], "code_verifier": verifier}).json()
        self.assertEqual(self.call(f"/api/mcp/w/{self.team['id']}/", tok["access_token"], "list_tasks").status_code, 200)
        self.assertEqual(self.call("/api/mcp/", tok["access_token"], "list_tasks").status_code, 401)
