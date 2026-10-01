"""GitHub App plumbing: app JWT, installation tokens, API calls, webhook signatures."""

import hashlib
import hmac
import threading
import time

import jwt
from django.conf import settings

from .http import DeliveryError, send
from .models import GitHubApp

_tokens: dict[int, tuple[str, float]] = {}
_lock = threading.Lock()


def get_app() -> GitHubApp | None:
    return GitHubApp.objects.order_by("-id").first()


def require_app() -> GitHubApp:
    app = get_app()
    if app is None:
        raise DeliveryError("The GitHub App isn't set up on this Flowly instance.", retry=False)
    return app


def app_jwt(app: GitHubApp) -> str:
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app.app_id)}, app.private_key, algorithm="RS256")


def _headers(token, scheme="token"):
    return {
        "Authorization": f"{scheme} {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def api(method, path, *, token=None, app_auth=False, json=None):
    app = require_app() if app_auth else None
    headers = _headers(app_jwt(app), "Bearer") if app_auth else (_headers(token) if token else {"Accept": "application/vnd.github+json"})
    res = send(method, f"{settings.GITHUB_API_URL}{path}", json=json, headers=headers, check_url=False)
    return res.json() if res.content else None


def installation_token(installation_id: int) -> str:
    with _lock:
        cached = _tokens.get(installation_id)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
    data = api("POST", f"/app/installations/{installation_id}/access_tokens", app_auth=True)
    # Tokens last an hour; refresh a bit early.
    with _lock:
        _tokens[installation_id] = (data["token"], time.time() + 50 * 60)
    return data["token"]


def installation_api(installation_id, method, path, json=None):
    return api(method, path, token=installation_token(installation_id), json=json)


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def manifest(base_url: str, name: str) -> dict:
    return {
        "name": name,
        "url": base_url,
        "hook_attributes": {"url": f"{base_url}/api/integrations/github/webhook/", "active": True},
        "redirect_url": f"{base_url}/github/app-created",
        "setup_url": f"{base_url}/github/installed",
        "setup_on_update": True,
        "public": True,
        "default_permissions": {"issues": "write", "metadata": "read"},
        "default_events": ["issues"],
    }


def convert_manifest(code: str) -> GitHubApp:
    data = api("POST", f"/app-manifests/{code}/conversions")
    GitHubApp.objects.all().delete()
    return GitHubApp.objects.create(
        app_id=data["id"],
        slug=data["slug"],
        name=data["name"],
        owner=(data.get("owner") or {}).get("login", ""),
        html_url=data["html_url"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        webhook_secret=data["webhook_secret"],
        private_key=data["pem"],
    )
