"""OAuth 2.1 authorization server for MCP clients (ChatGPT, claude.ai, Cursor, …). Secrets are stored hashed."""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=60)
CODE_TTL = timedelta(minutes=5)


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class OAuthClient(models.Model):
    """A client registered through dynamic client registration (RFC 7591)."""

    client_id = models.CharField(max_length=64, unique=True)
    #: Hash of the secret for confidential clients; empty for public (PKCE-only) clients.
    secret_hash = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=200, blank=True)
    uri = models.URLField(max_length=500, blank=True)
    logo_uri = models.URLField(max_length=500, blank=True)
    redirect_uris = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name or self.client_id


class OAuthCode(models.Model):
    code_hash = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    redirect_uri = models.URLField(max_length=500)
    code_challenge = models.CharField(max_length=128)
    scope = models.CharField(max_length=200, default="mcp")
    resource = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    @classmethod
    def issue(cls, **fields):
        raw = secrets.token_urlsafe(32)
        cls.objects.create(code_hash=digest(raw), expires_at=timezone.now() + CODE_TTL, **fields)
        return raw


class OAuthGrant(models.Model):
    """A user's authorization of a client: the access/refresh token pair (rotated on refresh)."""

    ACCESS_PREFIX = "flo_"

    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="oauth_grants")
    scope = models.CharField(max_length=200, default="mcp")
    resource = models.CharField(max_length=500, blank=True)
    access_hash = models.CharField(max_length=64, unique=True)
    access_expires_at = models.DateTimeField()
    refresh_hash = models.CharField(max_length=64, unique=True)
    refresh_expires_at = models.DateTimeField()
    revoked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def rotate(self):
        """New access + refresh tokens; returns the raw values."""
        access = self.ACCESS_PREFIX + secrets.token_urlsafe(32)
        refresh = "flr_" + secrets.token_urlsafe(40)
        now = timezone.now()
        self.access_hash, self.access_expires_at = digest(access), now + ACCESS_TTL
        self.refresh_hash, self.refresh_expires_at = digest(refresh), now + REFRESH_TTL
        return access, refresh


class WorkspaceToken(models.Model):
    """Service token for a workspace's MCP endpoint. It acts as a bot member of the workspace."""

    PREFIX = "flws_"

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="mcp_tokens")
    bot = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="workspace_token")
    name = models.CharField(max_length=80)
    hint = models.CharField(max_length=16)
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, workspace, name, created_by):
        from django.contrib.auth import get_user_model

        from workspaces.models import Membership

        raw = cls.PREFIX + secrets.token_urlsafe(32)
        bot = get_user_model().objects.create(
            email=f"bot-{secrets.token_hex(6)}@bots.flowly.invalid", full_name=f"Bot: {name}"[:150], is_bot=True,
        )
        bot.set_unusable_password()
        bot.save(update_fields=["password"])
        Membership.objects.create(workspace=workspace, user=bot, role=Membership.Role.MEMBER)
        token = cls.objects.create(workspace=workspace, bot=bot, name=name, hint=raw[:11], token_hash=digest(raw), created_by=created_by)
        return token, raw

    def revoke(self):
        # Deactivate rather than delete the bot, so its tasks and comments keep their author.
        bot = self.bot
        bot.is_active = False
        bot.save(update_fields=["is_active"])
        bot.memberships.all().delete()
        self.delete()
