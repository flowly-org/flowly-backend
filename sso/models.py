"""Single sign-on for an Enterprise workspace: one OpenID Connect provider, the email domains it vouches for, and the
sign-ins in flight. SAML can be added as a second kind of connection later."""

import secrets

from django.conf import settings
from django.db import models


def new_token():
    return secrets.token_hex(16)


class SsoConnection(models.Model):
    workspace = models.OneToOneField("workspaces.Workspace", on_delete=models.CASCADE, related_name="sso")
    #: OpenID Connect issuer, e.g. https://accounts.google.com or https://login.microsoftonline.com/<tenant>/v2.0.
    issuer = models.CharField(max_length=500)
    client_id = models.CharField(max_length=300)
    #: Encrypted (integrations.crypto); never returned by the API.
    client_secret = models.TextField(blank=True)
    #: Text of the sign-in button, e.g. "Acme SSO".
    label = models.CharField(max_length=60, blank=True)
    enabled = models.BooleanField(default=False)
    #: People on the domains must sign in with SSO (no password login or sign-up). Needs a successful test first.
    required = models.BooleanField(default=False)
    #: New and existing people who sign in with SSO join the workspace as members.
    auto_join = models.BooleanField(default=True)
    tested_at = models.DateTimeField(null=True, blank=True)
    tested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"SSO for {self.workspace}"


class SsoDomain(models.Model):
    """An email domain the connection vouches for, proven with a DNS TXT record."""

    connection = models.ForeignKey(SsoConnection, on_delete=models.CASCADE, related_name="domains")
    domain = models.CharField(max_length=253)
    token = models.CharField(max_length=40, default=new_token)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["domain"]
        constraints = [
            models.UniqueConstraint(fields=["connection", "domain"], name="unique_sso_domain_per_connection"),
            # A domain can be claimed by one workspace only.
            models.UniqueConstraint(fields=["domain"], condition=models.Q(verified_at__isnull=False), name="unique_verified_sso_domain"),
        ]

    @property
    def record_name(self):
        return f"_flowly-verification.{self.domain}"

    @property
    def record_value(self):
        return f"flowly-verification={self.token}"


class SsoLogin(models.Model):
    """A sign-in on its way through the identity provider (state, nonce and PKCE verifier live here, not in the URL)."""

    state = models.CharField(max_length=64, unique=True)
    connection = models.ForeignKey(SsoConnection, on_delete=models.CASCADE, related_name="+")
    nonce = models.CharField(max_length=64)
    verifier = models.CharField(max_length=128)
    next = models.CharField(max_length=300, blank=True)
    #: Started by a workspace admin from the settings, to check the setup (works before SSO is enabled).
    test = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
