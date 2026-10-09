import json
import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from sso.models import SsoConnection, SsoDomain, SsoLogin
from workspaces.models import Membership, Workspace

ISSUER = "https://idp.acme.example"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())), "kid": "k1", "use": "sig", "alg": "RS256"}
META = {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks", "token_endpoint_auth_methods_supported": ["client_secret_basic"]}


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeIdP:
    """Answers discovery, keys and the token call; `claims` decides what the next ID token says."""

    def __init__(self):
        self.claims = {}
        self.token_calls = []
        self.nonce = ""  # from the authorize URL, like a real provider

    def id_token(self, nonce, **overrides):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": "flowly-client", "sub": "u-1", "iat": now, "exp": now + 300, "nonce": nonce,
                  "email": "bob@acme.com", "email_verified": True, "name": "Bob Bianchi", **self.claims, **overrides}
        return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})

    def send(self, method, url, **kwargs):
        if url.endswith("/.well-known/openid-configuration"):
            return FakeResponse(META)
        if url == META["jwks_uri"]:
            return FakeResponse({"keys": [JWK]})
        if url == META["token_endpoint"]:
            self.token_calls.append(kwargs)
            return FakeResponse({"access_token": "at", "id_token": self.id_token(self.claims.pop("_nonce", self.nonce))})
        raise AssertionError(url)


@override_settings(PUBLIC_URL="https://app.flowly.test", API_PUBLIC_URL="https://api.flowly.test")
class SsoTests(APITestCase):
    def setUp(self):
        self.idp = FakeIdP()
        patcher = mock.patch("sso.oidc.send", side_effect=self.idp.send)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner_auth = self.register("anna@acme.com")
        self.owner = User.objects.get(email="anna@acme.com")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.owner_auth['access']}")
        self.ws = Workspace.objects.get(pk=self.client.post("/api/workspaces/", {"name": "Acme"}).data["id"])
        self.url = f"/api/workspaces/{self.ws.pk}/sso/"

    def register(self, email):
        res = self.client.post("/api/auth/register/", {"email": email, "password": "s3cure-Passw0rd", "full_name": email.split("@")[0]})
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def enterprise(self):
        self.ws.plan = "enterprise"
        self.ws.save()

    def configure(self, **extra):
        return self.client.put(self.url, {"issuer": ISSUER, "client_id": "flowly-client", "client_secret": "s3cret", **extra}, format="json")

    def verified(self):
        self.enterprise()
        self.configure()
        self.client.post(self.url + "domains/", {"domain": "acme.com"}, format="json")
        domain = SsoDomain.objects.get(domain="acme.com")
        with mock.patch("sso.services.txt_records", return_value=[domain.record_value]):
            self.client.post(self.url + "domains/", {"id": domain.id, "verify": True}, format="json")
        return SsoConnection.objects.get(workspace=self.ws)

    def sign_in(self, email="bob@acme.com", test=False):
        """Start → provider → callback; returns the redirect's fragment as a dict."""
        body = {"email": email} if not test else {"workspace": self.ws.pk, "test": True}
        res = self.client.post("/api/auth/sso/start/", body, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        q = parse_qs(urlparse(res.data["url"]).query)
        self.idp.nonce = q["nonce"][0]
        self.assertEqual((q["code_challenge_method"], q["redirect_uri"]), (["S256"], ["https://api.flowly.test/api/auth/sso/callback/"]))
        self.client.credentials()
        back = self.client.get("/api/auth/sso/callback/", {"code": "abc", "state": q["state"][0]})
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.owner_auth['access']}")
        self.assertEqual(back.status_code, 302)
        self.assertTrue(back["Location"].startswith("https://app.flowly.test/sso#"), back["Location"])
        return {k: v[0] for k, v in parse_qs(urlparse(back["Location"]).fragment).items()}

    def test_setup_needs_enterprise_and_keeps_the_secret(self):
        self.assertEqual(self.configure().status_code, 403)
        self.enterprise()
        res = self.configure(label="Acme login")
        self.assertEqual(res.status_code, 200, res.data)
        conn = res.data["connection"]
        self.assertEqual((conn["issuer"], conn["has_secret"], conn["label"]), (ISSUER, True, "Acme login"))
        self.assertNotIn("s3cret", json.dumps(res.data))
        self.assertNotEqual(SsoConnection.objects.get().client_secret, "s3cret")  # encrypted at rest
        self.assertEqual(res.data["callback_url"], "https://api.flowly.test/api/auth/sso/callback/")
        # Members can't see or change it.
        bob = User.objects.create_user(email="m@acme.com", password="x-Long-pass-1")
        Membership.objects.create(workspace=self.ws, user=bob)
        self.client.force_authenticate(bob)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_domains_are_proven_with_dns_and_claimed_once(self):
        self.enterprise()
        self.configure()
        self.assertEqual(self.client.post(self.url + "domains/", {"domain": "gmail.com"}, format="json").status_code, 400)
        res = self.client.post(self.url + "domains/", {"domain": "@ACME.com"}, format="json")
        domain = res.data["connection"]["domains"][0]
        self.assertEqual((domain["domain"], domain["record_name"]), ("acme.com", "_flowly-verification.acme.com"))
        with mock.patch("sso.services.txt_records", return_value=["something else"]):
            self.assertEqual(self.client.post(self.url + "domains/", {"id": domain["id"], "verify": True}, format="json").status_code, 400)
        self.assertEqual(self.configure(enabled=True).status_code, 400)  # no verified domain yet
        with mock.patch("sso.services.txt_records", return_value=[domain["record_value"]]):
            res = self.client.post(self.url + "domains/", {"id": domain["id"], "verify": True}, format="json")
        self.assertTrue(res.data["connection"]["domains"][0]["verified"])
        other = Workspace.objects.create(name="Rival", plan="enterprise")
        Membership.objects.create(workspace=other, user=self.owner, role="owner")
        self.client.put(f"/api/workspaces/{other.pk}/sso/", {"issuer": ISSUER, "client_id": "x"}, format="json")
        res = self.client.post(f"/api/workspaces/{other.pk}/sso/domains/", {"domain": "acme.com"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("another workspace", res.data["domain"])

    def test_new_person_signs_in_and_joins(self):
        conn = self.verified()
        self.assertFalse(self.client.get("/api/auth/sso/discover/", {"email": "bob@acme.com"}).data["sso"])  # not enabled yet
        self.assertEqual(self.configure(enabled=True).status_code, 200)
        found = self.client.get("/api/auth/sso/discover/", {"email": "Bob@Acme.com"}).data
        self.assertEqual((found["sso"], found["required"], found["label"]), (True, False, "Acme SSO"))
        self.assertFalse(self.client.get("/api/auth/sso/discover/", {"email": "bob@other.com"}).data["sso"])

        fragment = self.sign_in()
        bob = User.objects.get(email="bob@acme.com")
        self.assertEqual((bob.full_name, bob.has_usable_password()), ("Bob Bianchi", False))
        self.assertTrue(Membership.objects.filter(workspace=self.ws, user=bob, role="member").exists())
        self.assertTrue(bob.memberships.filter(workspace__name="Personal").exists())
        self.assertEqual(fragment["next"], "/")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {fragment['access']}")
        self.assertEqual(self.client.get("/api/auth/me/").data["email"], "bob@acme.com")
        # Client secret sent as HTTP Basic; PKCE verifier included.
        call = self.idp.token_calls[0]
        self.assertTrue(call["headers"]["Authorization"].startswith("Basic "))
        self.assertTrue(call["data"]["code_verifier"])
        self.assertFalse(SsoLogin.objects.exists())  # one use
        conn.refresh_from_db()
        self.assertIsNone(conn.tested_at)  # not a test

    def test_bad_tokens_are_refused(self):
        self.verified()
        self.configure(enabled=True)
        for claims, why in [({"email": "eve@evil.com"}, "isn't on a domain"), ({"aud": "someone-else"}, "didn't check out"),
                            ({"_nonce": "wrong"}, "nonce"), ({"email_verified": False, "preferred_username": "x"}, "verified email")]:
            self.idp.claims = dict(claims)
            fragment = self.sign_in()
            self.assertNotIn("access", fragment)
            self.assertIn(why, fragment["error"])
        self.assertFalse(User.objects.filter(email="eve@evil.com").exists())
        # A replayed or unknown state goes nowhere.
        res = self.client.get("/api/auth/sso/callback/", {"code": "abc", "state": "nope"})
        self.assertIn("expired", parse_qs(urlparse(res["Location"]).fragment)["error"][0])

    def test_required_sso_after_a_test(self):
        conn = self.verified()
        self.assertEqual(self.configure(enabled=True, required=True).status_code, 400)  # never tested
        self.idp.claims = {"email": "anna@acme.com"}
        fragment = self.sign_in(test=True)  # works before it's enabled
        self.assertEqual(fragment["tested"], "1")
        self.assertEqual(fragment["next"], f"/w/{self.ws.pk}/settings")
        conn.refresh_from_db()
        self.assertEqual(conn.tested_by, self.owner)
        res = self.configure(enabled=True, required=True)
        self.assertEqual(res.status_code, 200, res.data)
        # Passwords stop working for the domain…
        res = self.client.post("/api/auth/login/", {"email": "anna@acme.com", "password": "s3cure-Passw0rd"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["sso"], "required")
        self.assertIn("requires single sign-on", res.data["detail"])
        res = self.client.post("/api/auth/register/", {"email": "new@acme.com", "password": "s3cure-Passw0rd", "full_name": "N"})
        self.assertEqual(res.status_code, 400)
        # …except for instance admins (break-glass) and other domains.
        self.owner.is_staff = True
        self.owner.save()
        self.assertEqual(self.client.post("/api/auth/login/", {"email": "anna@acme.com", "password": "s3cure-Passw0rd"}).status_code, 200)
        self.register("someone@elsewhere.com")
        # A new provider set-up must be tested again: not while SSO is required (it could lock everyone out)…
        self.assertEqual(self.configure(client_id="other-client").status_code, 400)
        res = self.configure(client_id="other-client", required=False)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(res.data["connection"]["tested_at"])
        # …and losing the Enterprise plan switches SSO off.
        self.ws.plan = "standard"
        self.ws.save()
        self.assertFalse(self.client.get("/api/auth/sso/discover/", {"email": "bob@acme.com"}).data["sso"])

    def test_hs256_tokens_are_never_accepted(self):
        from sso import oidc

        token = jwt.encode({"iss": ISSUER, "aud": "flowly-client", "sub": "1", "iat": 1, "exp": 9999999999, "nonce": "n"},
                           "s3cret-shared-key-long-enough-32b", algorithm="HS256")
        with self.assertRaisesRegex(oidc.OidcError, "Unsupported"):
            oidc.verify(META, token, "flowly-client", "n")
