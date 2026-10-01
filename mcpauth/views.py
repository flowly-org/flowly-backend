"""OAuth 2.1 for MCP: discovery (RFC 9728 / RFC 8414), dynamic registration (RFC 7591),
authorization code + PKCE S256, refresh-token rotation, revocation (RFC 7009)."""

import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode, urlparse

from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework.response import Response
from rest_framework.views import APIView

from instance import conf

from .models import OAuthClient, OAuthCode, OAuthGrant, digest

#: mcp = the MCP endpoints; api = the REST API (used by Flowly's own desktop/mobile clients).
SCOPES = ["mcp", "api"]


def api_base():
    return conf.get("API_PUBLIC_URL") or conf.get("PUBLIC_URL")


def mcp_resource():
    return f"{api_base()}/api/mcp/"


def resource_workspace(resource: str):
    """None if `resource` isn't one of our MCP URLs; 0 for the personal endpoint; else the workspace id."""
    import re

    for base in filter(None, [conf.get("API_PUBLIC_URL"), conf.get("PUBLIC_URL")]):
        m = re.fullmatch(re.escape(base) + r"/(?:api/)?mcp(?:/w/(\d+))?/?", resource or "")
        if m:
            return int(m.group(1)) if m.group(1) else 0
    return None


def accepted_resources():
    """Kept for callers that need the personal endpoint variants."""
    out = set()
    for base in filter(None, [conf.get("API_PUBLIC_URL"), conf.get("PUBLIC_URL")]):
        out |= {f"{base}/api/mcp/", f"{base}/api/mcp", f"{base}/mcp"}
    return out


def with_cors(response):
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Mcp-Protocol-Version, Mcp-Session-Id"
    response["Access-Control-Expose-Headers"] = "WWW-Authenticate, Mcp-Session-Id"
    return response


def oauth_error(error, description="", status=400):
    return with_cors(JsonResponse({"error": error, "error_description": description}, status=status))


@method_decorator(csrf_exempt, name="dispatch")
class PublicView(View):
    def options(self, request, *args, **kwargs):
        return with_cors(HttpResponse(status=204))


class ProtectedResourceMetadata(PublicView):
    def get(self, request, path=""):
        import re

        m = re.match(r"(?:api/)?mcp/w/(\d+)", path or "")
        resource = f"{api_base()}/api/mcp/w/{m.group(1)}/" if m else mcp_resource()
        return with_cors(JsonResponse({
            "resource": resource,
            "authorization_servers": [api_base()],
            "scopes_supported": SCOPES,
            "bearer_methods_supported": ["header"],
            "resource_name": "Flowly",
            "resource_documentation": f"{conf.get('PUBLIC_URL')}/settings",
        }))


class AuthorizationServerMetadata(PublicView):
    def get(self, request, path=""):
        base = api_base()
        return with_cors(JsonResponse({
            "issuer": base,
            "authorization_endpoint": f"{conf.get('PUBLIC_URL') or base}/oauth/authorize",
            "token_endpoint": f"{base}/api/oauth/token",
            "registration_endpoint": f"{base}/api/oauth/register",
            "revocation_endpoint": f"{base}/api/oauth/revoke",
            "scopes_supported": SCOPES,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "service_documentation": f"{conf.get('PUBLIC_URL')}/settings",
        }))


def valid_redirect(uri: str) -> bool:
    try:
        p = urlparse(uri)
    except ValueError:
        return False
    if p.fragment or not p.netloc:
        return False
    if p.scheme == "https":
        return True
    # Native / CLI clients (Claude Code, Inspector, Cursor…) listen on loopback.
    return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")


class RegisterView(PublicView):
    def post(self, request):
        try:
            data = json.loads(request.body or b"{}")
        except ValueError:
            return oauth_error("invalid_client_metadata", "Body must be JSON.")
        uris = data.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and valid_redirect(u) for u in uris):
            return oauth_error("invalid_redirect_uri", "Give https (or loopback http) redirect_uris.")
        method = data.get("token_endpoint_auth_method", "none")
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            return oauth_error("invalid_client_metadata", "Unsupported token_endpoint_auth_method.")
        secret = secrets.token_urlsafe(32) if method != "none" else ""
        client = OAuthClient.objects.create(
            client_id="flc_" + secrets.token_urlsafe(18), secret_hash=digest(secret) if secret else "",
            name=str(data.get("client_name") or "")[:200], uri=str(data.get("client_uri") or "")[:500],
            logo_uri=str(data.get("logo_uri") or "")[:500], redirect_uris=uris[:10],
        )
        body = {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at.timestamp()),
            "client_name": client.name,
            "redirect_uris": client.redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
            "scope": "mcp",
        }
        if secret:
            body.update(client_secret=secret, client_secret_expires_at=0)
        return with_cors(JsonResponse(body, status=201))


def check_authorize_params(params):
    """Validate an authorization request; returns (client, error_message)."""
    client = OAuthClient.objects.filter(client_id=params.get("client_id", "")).first()
    if client is None:
        return None, "Unknown client. Remove the connector and add it again."
    if params.get("redirect_uri") not in client.redirect_uris:
        return None, "The redirect URI isn't registered for this client."
    if params.get("response_type") != "code":
        return client, "Only response_type=code is supported."
    if params.get("code_challenge_method") != "S256" or len(params.get("code_challenge", "")) < 43:
        return client, "PKCE with S256 is required."
    resource = params.get("resource")
    if resource and resource_workspace(resource) is None:
        return client, f"Unknown resource {resource}."
    scope = params.get("scope") or "mcp"
    if any(s not in SCOPES + ["openid", "offline_access"] for s in scope.split()):
        return client, f"Unsupported scope {scope}."
    return client, None


def granted_scope(params) -> str:
    requested = (params.get("scope") or "mcp").split()
    return " ".join(s for s in SCOPES if s in requested) or "mcp"


class AuthorizeView(APIView):
    """Used by the consent page: GET describes the request, POST {…params, decision} returns the redirect."""

    def get(self, request):
        client, error = check_authorize_params(request.query_params)
        if error:
            return Response({"detail": error}, status=400)
        host = urlparse(request.query_params["redirect_uri"]).hostname
        workspace = None
        ws_id = resource_workspace(request.query_params.get("resource", ""))
        if ws_id:
            ws = request.user.memberships.filter(workspace_id=ws_id).select_related("workspace").first()
            if ws is None:
                return Response({"detail": "You aren't a member of that workspace."}, status=403)
            workspace = {"id": ws.workspace_id, "name": ws.workspace.name}
        return Response({"client": {"name": client.name or host, "uri": client.uri, "logo_uri": client.logo_uri,
                                    "redirect_host": host}, "user": request.user.email, "workspace": workspace,
                         "scopes": granted_scope(request.query_params).split()})

    def post(self, request):
        params = request.data
        client, error = check_authorize_params(params)
        if error:
            return Response({"detail": error}, status=400)
        redirect = params["redirect_uri"]
        sep = "&" if "?" in redirect else "?"
        state = {"state": params["state"]} if params.get("state") else {}
        if params.get("decision") != "allow":
            return Response({"redirect": redirect + sep + urlencode({"error": "access_denied", **state})})
        ws_id = resource_workspace(params.get("resource", ""))
        if ws_id and not request.user.memberships.filter(workspace_id=ws_id).exists():
            return Response({"detail": "You aren't a member of that workspace."}, status=403)
        code = OAuthCode.issue(
            client=client, user=request.user, redirect_uri=redirect, code_challenge=params["code_challenge"],
            scope=granted_scope(params), resource=params.get("resource") or "",
        )
        return Response({"redirect": redirect + sep + urlencode({"code": code, **state, "iss": api_base()})})


def client_from_request(request, form):
    client_id, secret = form.get("client_id"), form.get("client_secret")
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            client_id, secret = base64.b64decode(header[6:]).decode().split(":", 1)
        except Exception:
            return None
    client = OAuthClient.objects.filter(client_id=client_id or "").first()
    if client is None:
        return None
    if client.secret_hash and (not secret or digest(secret) != client.secret_hash):
        return None
    return client


def token_response(grant, access, refresh):
    return with_cors(JsonResponse({
        "access_token": access, "token_type": "Bearer",
        "expires_in": int((grant.access_expires_at - timezone.now()).total_seconds()),
        "refresh_token": refresh, "scope": grant.scope,
    }, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}))


class TokenView(PublicView):
    @transaction.atomic
    def post(self, request):
        form = request.POST if request.POST else json.loads(request.body or b"{}")
        client = client_from_request(request, form)
        if client is None:
            return oauth_error("invalid_client", "Unknown client or bad credentials.", 401)
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            code = OAuthCode.objects.select_for_update().filter(code_hash=digest(form.get("code", "")), client=client).first()
            if code is None or code.used or code.expires_at < timezone.now():
                return oauth_error("invalid_grant", "The code is invalid or expired.")
            code.used = True
            code.save(update_fields=["used"])
            if form.get("redirect_uri") and form["redirect_uri"] != code.redirect_uri:
                return oauth_error("invalid_grant", "redirect_uri mismatch.")
            verifier = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if not verifier or not secrets.compare_digest(challenge, code.code_challenge):
                return oauth_error("invalid_grant", "PKCE verification failed.")
            grant = OAuthGrant(client=client, user=code.user, scope=code.scope, resource=code.resource)
            access, refresh = grant.rotate()
            grant.save()
            return token_response(grant, access, refresh)
        if grant_type == "refresh_token":
            grant = OAuthGrant.objects.select_for_update().filter(
                refresh_hash=digest(form.get("refresh_token", "")), client=client, revoked=False
            ).first()
            if grant is None or grant.refresh_expires_at < timezone.now() or not grant.user.is_active:
                return oauth_error("invalid_grant", "The refresh token is invalid or expired.")
            access, refresh = grant.rotate()
            grant.save()
            return token_response(grant, access, refresh)
        return oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")


class RevokeView(PublicView):
    def post(self, request):
        form = request.POST if request.POST else json.loads(request.body or b"{}")
        h = digest(form.get("token", ""))
        OAuthGrant.objects.filter(revoked=False).filter(access_hash=h).update(revoked=True)
        OAuthGrant.objects.filter(revoked=False).filter(refresh_hash=h).update(revoked=True)
        return with_cors(HttpResponse(status=200))


def authenticate_access_token(raw: str):
    """(user, workspace_id or None) for a valid OAuth access token, else None.

    A grant made for a workspace endpoint (resource …/mcp/w/<id>/) only works on that workspace.
    """
    if not raw.startswith(OAuthGrant.ACCESS_PREFIX):
        return None
    grant = OAuthGrant.objects.select_related("user").filter(access_hash=digest(raw), revoked=False).first()
    if grant is None or grant.access_expires_at < timezone.now() or not grant.user.is_active:
        return None
    ws = resource_workspace(grant.resource) if grant.resource else 0
    if ws is None:
        return None
    if not grant.last_used_at or (timezone.now() - grant.last_used_at).total_seconds() > 300:
        OAuthGrant.objects.filter(pk=grant.pk).update(last_used_at=timezone.now())
    return grant.user, (ws or None)


def authenticate_workspace_token(raw: str):
    """(bot user, workspace_id) for a valid workspace service token, else None."""
    from .models import WorkspaceToken

    if not raw.startswith(WorkspaceToken.PREFIX):
        return None
    token = WorkspaceToken.objects.select_related("bot").filter(token_hash=digest(raw)).first()
    if token is None or not token.bot.is_active:
        return None
    if not token.last_used_at or (timezone.now() - token.last_used_at).total_seconds() > 300:
        WorkspaceToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    return token.bot, token.workspace_id


class GrantsView(APIView):
    """Connected apps of the current user (Settings)."""

    def get(self, request):
        grants = request.user.oauth_grants.filter(revoked=False).select_related("client")
        return Response([
            {"id": g.id, "client": g.client.name or urlparse(g.client.redirect_uris[0]).hostname,
             "created_at": g.created_at, "last_used_at": g.last_used_at}
            for g in grants
        ])

    def delete(self, request, pk):
        grant = get_object_or_404(request.user.oauth_grants, pk=pk)
        grant.revoked = True
        grant.save(update_fields=["revoked"])
        return Response(status=204)


class WorkspaceTokensView(APIView):
    """Owners/admins: GET/POST /api/workspaces/<ws>/mcp-tokens/, DELETE …/<id>/."""

    def _membership(self, request, ws):
        from workspaces.models import Membership

        m = Membership.objects.filter(workspace_id=ws, user=request.user).select_related("workspace").first()
        if m is None:
            from django.http import Http404

            raise Http404
        if not m.can_manage:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("Only workspace owners and admins can manage service tokens.")
        return m

    def get(self, request, ws):
        m = self._membership(request, ws)
        return Response({
            "url": f"{api_base()}/api/mcp/w/{ws}/",
            "tokens": [{"id": t.id, "name": t.name, "hint": t.hint, "bot": t.bot.full_name, "created_at": t.created_at,
                        "last_used_at": t.last_used_at} for t in m.workspace.mcp_tokens.select_related("bot")],
        })

    def post(self, request, ws):
        from .models import WorkspaceToken

        m = self._membership(request, ws)
        name = (request.data.get("name") or "").strip()[:80] or "Automation"
        token, raw = WorkspaceToken.issue(m.workspace, name, request.user)
        return Response({"id": token.id, "name": token.name, "hint": token.hint, "bot": token.bot.full_name,
                         "created_at": token.created_at, "last_used_at": None, "token": raw}, status=201)

    def delete(self, request, ws, pk):
        m = self._membership(request, ws)
        get_object_or_404(m.workspace.mcp_tokens, pk=pk).revoke()
        return Response(status=204)
