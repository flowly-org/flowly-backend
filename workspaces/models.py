from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models


WORKSPACE_COLORS = ["#4f46e5", "#0ea5e9", "#14b8a6", "#22c55e", "#eab308", "#f97316", "#ef4444", "#ec4899", "#a855f7", "#64748b"]


class Workspace(models.Model):
    name = models.CharField(max_length=120)
    #: Brand colour of the workspace; tints the Flowly logo and favicon while it's open.
    color = models.CharField(
        max_length=7, default="#4f46e5", validators=[RegexValidator(r"^#[0-9a-fA-F]{6}$", "Use a hex color like #4f46e5.")]
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "user"], name="unique_membership")
        ]
        ordering = ["joined_at", "id"]

    @property
    def can_manage(self):
        return self.role in (self.Role.OWNER, self.Role.ADMIN)

    def __str__(self):
        return f"{self.user} @ {self.workspace} ({self.role})"


def new_invite_token():
    import secrets

    return secrets.token_urlsafe(24)


class Invitation(models.Model):
    """Pending invite for an email that has no account yet; consumed on sign-up or through its link."""

    LINK_DAYS = 30

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(
        max_length=10, choices=Membership.Role.choices, default=Membership.Role.MEMBER
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: Secret for the shareable join link (/join/<token>).
    token = models.CharField(max_length=64, unique=True, default=new_invite_token, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def link_expired(self):
        from datetime import timedelta

        from django.utils import timezone

        return self.created_at < timezone.now() - timedelta(days=self.LINK_DAYS)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "email"], name="unique_invitation")
        ]
        ordering = ["created_at", "id"]
