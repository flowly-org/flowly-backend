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
