from django.utils import timezone
from rest_framework import authentication, exceptions, serializers

from .models import ApiToken


class ApiTokenAuthentication(authentication.BaseAuthentication):
    """`Authorization: Bearer flw_…` personal access tokens (other Bearer values fall through to JWT)."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode()
        parts = header.split()
        if len(parts) != 2 or parts[0] != self.keyword or not parts[1].startswith(ApiToken.PREFIX):
            return None
        token = ApiToken.objects.select_related("user").filter(token_hash=ApiToken.hash(parts[1])).first()
        if token is None or not token.user.is_active:
            raise exceptions.AuthenticationFailed("Invalid or revoked API token.")
        if not token.last_used_at or (timezone.now() - token.last_used_at).total_seconds() > 300:
            ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        return token.user, token

    def authenticate_header(self, request):
        return 'Bearer realm="flowly"'


class ApiTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApiToken
        fields = ["id", "name", "hint", "created_at", "last_used_at"]
        read_only_fields = ["id", "hint", "created_at", "last_used_at"]


class OAuthApiAuthentication(authentication.BaseAuthentication):
    """OAuth access tokens (flo_…) on the REST API, only for grants that include the "api" scope
    (Flowly's own clients, e.g. the desktop tray app). Workspace-bound grants are refused here."""

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode()
        parts = header.split()
        if len(parts) != 2 or parts[0] != "Bearer" or not parts[1].startswith("flo_"):
            return None
        from mcpauth.models import OAuthGrant, digest
        from mcpauth.views import resource_workspace

        grant = OAuthGrant.objects.select_related("user").filter(access_hash=digest(parts[1]), revoked=False).first()
        if grant is None or grant.access_expires_at < timezone.now() or not grant.user.is_active:
            raise exceptions.AuthenticationFailed("The access token is invalid or expired.")
        if "api" not in grant.scope.split() or (grant.resource and resource_workspace(grant.resource)):
            raise exceptions.AuthenticationFailed("This token isn't allowed to use the API.")
        return grant.user, grant

    def authenticate_header(self, request):
        return 'Bearer realm="flowly"'
