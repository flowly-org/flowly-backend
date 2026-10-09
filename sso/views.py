"""Sign-in with SSO (discover, start, callback) and its set-up under /workspaces/<id>/sso/ for owners and admins."""

import secrets
from datetime import timedelta
from urllib.parse import urlencode

from django.db import IntegrityError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from instance import conf
from integrations.crypto import decrypt, encrypt
from workspaces.models import Membership
from workspaces.permissions import WorkspaceScopedMixin

from . import oidc, services
from .models import SsoConnection, SsoDomain, SsoLogin

LOGIN_TTL = timedelta(minutes=10)


def callback_url():
    from mcpauth.views import api_base

    return f"{api_base()}/api/auth/sso/callback/"


def app_url(path: str, **fragment) -> str:
    base = (conf.get("PUBLIC_URL") or "").rstrip("/")
    return f"{base}{path}" + (f"#{urlencode(fragment)}" if fragment else "")


def safe_next(value: str) -> str:
    value = (value or "").strip()
    return value if value.startswith("/") and not value.startswith("//") else "/"


def label_of(conn: SsoConnection) -> str:
    return conn.label or f"{conn.workspace.name} SSO"


class PublicView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sso"


class DiscoverView(PublicView):
    """GET ?email= → whether that email signs in with SSO (and must)."""

    def get(self, request):
        conn = services.connection_for_email(request.query_params.get("email", ""))
        if conn is None:
            return Response({"sso": False})
        return Response({"sso": True, "required": conn.required, "label": label_of(conn)})


class StartView(APIView):
    """POST {email, next?} → {url} of the provider's sign-in page. With a signed-in workspace admin and
    {"workspace": id, "test": true}, it tests a connection that isn't enabled yet."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sso"

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        test = bool(request.data.get("test"))
        if test:
            if not request.user.is_authenticated:
                raise PermissionDenied("Sign in first.")
            conn = get_object_or_404(SsoConnection.objects.select_related("workspace"), workspace_id=request.data.get("workspace"))
            if not Membership.objects.filter(workspace=conn.workspace, user=request.user,
                                             role__in=[Membership.Role.OWNER, Membership.Role.ADMIN]).exists():
                raise PermissionDenied("Only workspace owners and admins can test single sign-on.")
            email = email or request.user.email
        else:
            conn = services.connection_for_email(email)
            if conn is None:
                return Response({"detail": "Single sign-on isn't set up for this email."}, status=status.HTTP_404_NOT_FOUND)
        try:
            meta = oidc.discover(conn.issuer)
        except oidc.OidcError as exc:
            SsoConnection.objects.filter(pk=conn.pk).update(last_error=str(exc)[:500])
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        SsoLogin.objects.filter(created_at__lt=timezone.now() - LOGIN_TTL).delete()
        verifier, challenge = oidc.pkce()
        login = SsoLogin.objects.create(state=secrets.token_urlsafe(32), connection=conn, nonce=secrets.token_urlsafe(24),
                                        verifier=verifier, next=safe_next(request.data.get("next") or ("/w/%d/settings" % conn.workspace_id if test else "/")),
                                        test=test)
        url = oidc.authorize_url(meta, conn.client_id, callback_url(), login.state, login.nonce, challenge, login_hint=email)
        return Response({"url": url})


class CallbackView(PublicView):
    """The provider sends the browser back here; we sign the person in and hand the app its tokens (in the fragment)."""

    def get(self, request):
        state = request.query_params.get("state", "")
        login = SsoLogin.objects.filter(state=state).select_related("connection__workspace").first() if state else None
        if login is None or login.created_at < timezone.now() - LOGIN_TTL:
            return self.fail("This sign-in link expired. Start again.")
        login.delete()  # one use
        conn = login.connection
        if request.query_params.get("error"):
            detail = request.query_params.get("error_description") or request.query_params.get("error")
            return self.fail(f"The provider stopped the sign-in: {detail}", conn, login)
        try:
            meta = oidc.discover(conn.issuer)
            tokens = oidc.exchange(meta, conn, request.query_params.get("code", ""), login.verifier, callback_url())
            if not tokens.get("id_token"):
                raise oidc.OidcError("The provider didn't send an ID token (is the openid scope allowed?).")
            claims = oidc.verify(meta, tokens["id_token"], conn.client_id, login.nonce)
            email = oidc.email_of(claims)
            if not (conn.enabled or login.test) or not conn.workspace.is_enterprise:
                raise services.SsoError("Single sign-on is turned off for this workspace.")
            user = services.sign_in(conn, email, claims, test=login.test)
        except (oidc.OidcError, services.SsoError) as exc:
            return self.fail(str(exc), conn, login)
        from accounts.activity import touch

        touch(user)
        refresh = RefreshToken.for_user(user)
        return HttpResponseRedirect(app_url("/sso", access=str(refresh.access_token), refresh=str(refresh), next=login.next,
                                            **({"tested": "1"} if login.test else {})))

    def fail(self, message, conn=None, login=None):
        if conn is not None:
            SsoConnection.objects.filter(pk=conn.pk).update(last_error=message[:500])
        return HttpResponseRedirect(app_url("/sso", error=message, next=login.next if login else "/"))


# --- set-up ---------------------------------------------------------------------------------------------------------


def connection_data(conn: SsoConnection | None, workspace):
    return {
        "available": workspace.is_enterprise,
        "callback_url": callback_url(),
        "connection": None if conn is None else {
            "issuer": conn.issuer, "client_id": conn.client_id, "has_secret": bool(conn.client_secret), "label": conn.label,
            "enabled": conn.enabled, "required": conn.required, "auto_join": conn.auto_join,
            "tested_at": conn.tested_at, "tested_by": conn.tested_by.email if conn.tested_by_id else None,
            "last_error": conn.last_error,
            "domains": [{"id": d.id, "domain": d.domain, "verified": d.verified_at is not None, "record_name": d.record_name,
                         "record_value": d.record_value} for d in conn.domains.all()],
        },
    }


class ManageView(WorkspaceScopedMixin, APIView):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.require_manager()

    def connection(self):
        return SsoConnection.objects.filter(workspace=self.workspace).prefetch_related("domains").first()

    def require_enterprise(self):
        if not self.workspace.is_enterprise:
            raise PermissionDenied("Single sign-on is part of the Enterprise plan.")


class ConnectionView(ManageView):
    """GET the set-up · PUT {issuer, client_id, client_secret?, label, enabled, required, auto_join} · DELETE."""

    def get(self, request, workspace_pk):
        return Response(connection_data(self.connection(), self.workspace))

    def put(self, request, workspace_pk):
        self.require_enterprise()
        conn = self.connection() or SsoConnection(workspace=self.workspace)
        data = request.data
        issuer = (data.get("issuer", conn.issuer) or "").strip().rstrip("/")
        client_id = (data.get("client_id", conn.client_id) or "").strip()
        if not issuer.startswith("https://"):
            raise ValidationError({"issuer": "Use the provider's https:// issuer URL."})
        if not client_id:
            raise ValidationError({"client_id": "Paste the client (application) ID from your provider."})
        changed = issuer != conn.issuer or client_id != conn.client_id
        if changed or not conn.pk:
            try:
                oidc.discover(issuer)
            except oidc.OidcError as exc:
                raise ValidationError({"issuer": str(exc)}) from exc
        conn.issuer, conn.client_id = issuer, client_id
        secret = str(data.get("client_secret") or "").strip()
        if secret and secret != (decrypt(conn.client_secret) if conn.client_secret else ""):
            conn.client_secret = encrypt(secret)
            changed = True
        conn.label = str(data.get("label", conn.label) or "")[:60]
        conn.auto_join = bool(data.get("auto_join", conn.auto_join))
        if changed:
            conn.tested_at = None  # a new provider set-up has to be tested again
        conn.enabled = bool(data.get("enabled", conn.enabled))
        conn.required = bool(data.get("required", conn.required)) and conn.enabled
        has_domain = bool(conn.pk) and conn.domains.filter(verified_at__isnull=False).exists()
        if conn.enabled and not has_domain:
            raise ValidationError({"enabled": "Verify at least one email domain first."})
        if conn.required and not conn.tested_at:
            raise ValidationError({"required": "Sign in once with “Test sign-in” before requiring SSO, so nobody gets locked out."})
        conn.save()
        return Response(connection_data(self.connection(), self.workspace))

    def delete(self, request, workspace_pk):
        SsoConnection.objects.filter(workspace=self.workspace).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DomainsView(ManageView):
    """POST {domain} adds a domain to verify · POST {id, verify: true} checks its DNS record · DELETE ?id= removes it."""

    def post(self, request, workspace_pk):
        self.require_enterprise()
        conn = self.connection()
        if conn is None:
            raise ValidationError({"detail": "Save the provider settings first."})
        try:
            if request.data.get("verify"):
                domain = get_object_or_404(SsoDomain, pk=request.data.get("id"), connection=conn)
                services.verify_domain(domain)
            else:
                name = services.clean_domain(request.data.get("domain", ""))
                if SsoDomain.objects.filter(domain=name, verified_at__isnull=False).exclude(connection=conn).exists():
                    raise services.SsoError(f"{name} is already verified by another workspace.")
                SsoDomain.objects.get_or_create(connection=conn, domain=name)
        except services.SsoError as exc:
            raise ValidationError({"domain": str(exc)}) from exc
        except IntegrityError as exc:
            raise ValidationError({"domain": "That domain is already verified by another workspace."}) from exc
        return Response(connection_data(self.connection(), self.workspace))

    def delete(self, request, workspace_pk):
        conn = self.connection()
        if conn is not None:
            conn.domains.filter(pk=request.query_params.get("id")).delete()
            if not conn.domains.filter(verified_at__isnull=False).exists():
                SsoConnection.objects.filter(pk=conn.pk).update(enabled=False, required=False)
        return Response(connection_data(self.connection(), self.workspace))
