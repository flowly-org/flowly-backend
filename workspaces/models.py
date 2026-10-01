from django.conf import settings
from django.db import models


class Workspace(models.Model):
    name = models.CharField(max_length=120)
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


class Invitation(models.Model):
    """Pending invite for an email that has no account yet; consumed on sign-up."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(
        max_length=10, choices=Membership.Role.choices, default=Membership.Role.MEMBER
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "email"], name="unique_invitation")
        ]
        ordering = ["created_at", "id"]
