"""When people were last active, for activation stats (the JWT sign-in never sets `last_login`)."""

from datetime import timedelta

from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication

from .tokens import ApiTokenAuthentication, OAuthApiAuthentication

EVERY = timedelta(minutes=5)


def touch(user):
    if user is None or not getattr(user, "pk", None):
        return
    now = timezone.now()
    if user.last_seen_at is None or now - user.last_seen_at > EVERY:
        type(user).objects.filter(pk=user.pk).update(last_seen_at=now)
        user.last_seen_at = now


class Seen:
    def authenticate(self, request):
        result = super().authenticate(request)
        if result:
            touch(result[0])
        return result


class SeenJWTAuthentication(Seen, JWTAuthentication):
    pass


class SeenApiTokenAuthentication(Seen, ApiTokenAuthentication):
    pass


class SeenOAuthApiAuthentication(Seen, OAuthApiAuthentication):
    pass
