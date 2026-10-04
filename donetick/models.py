"""Donetick (self-hosted chores app) sync.

Two ways to connect a Donetick server:
- a workspace (or project) integration: `integrations.Integration` of kind "donetick", managed by workspace admins;
  chores land in that workspace, some labels can map to projects;
- a personal connection (`DonetickAccount`): the user's own token; each chore's labels pick the workspace and
  project it lands in.
"""

import secrets

from django.conf import settings
from django.db import models


def new_hook_token():
    return secrets.token_hex(16)


class DonetickAccount(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="donetick_accounts")
    name = models.CharField(max_length=80, default="Donetick")
    base_url = models.CharField(max_length=300)
    #: Encrypted eAPI access token.
    token = models.TextField(blank=True)
    hook_token = models.CharField(max_length=40, unique=True, default=new_hook_token)
    enabled = models.BooleanField(default=True)
    #: The Donetick user id of this person (to keep only their chores and to complete as them).
    donetick_user_id = models.IntegerField(null=True, blank=True)
    only_mine = models.BooleanField(default=True, help_text="Only chores assigned to you.")
    #: Where chores without a matching label go; empty = they are skipped.
    default_workspace = models.ForeignKey("workspaces.Workspace", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    default_project = models.ForeignKey("tasks.Project", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    #: New tasks are private (only you see them) instead of visible to the team.
    private = models.BooleanField(default=True)
    #: Completing, renaming or rescheduling the task in Flowly updates the chore.
    write_back = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class DonetickRoute(models.Model):
    """Personal connection: chores with this label go to this workspace (and project)."""

    account = models.ForeignKey(DonetickAccount, on_delete=models.CASCADE, related_name="routes")
    label = models.CharField(max_length=100)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="+")
    project = models.ForeignKey("tasks.Project", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    position = models.IntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class DonetickSync(models.Model):
    """Sync state of one connection (workspace integration or personal account)."""

    integration = models.OneToOneField("integrations.Integration", null=True, blank=True, on_delete=models.CASCADE, related_name="donetick")
    account = models.OneToOneField(DonetickAccount, null=True, blank=True, on_delete=models.CASCADE, related_name="sync")
    synced_at = models.DateTimeField(null=True, blank=True)
    next_poll_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    #: Circle members as Donetick reports them: [{"userId", "username", "displayName"}].
    members = models.JSONField(default=list, blank=True)
    #: Label names seen on chores (to help mapping).
    labels = models.JSONField(default=list, blank=True)
    chores = models.PositiveIntegerField(default=0)


class DonetickLink(models.Model):
    """One occurrence of a chore ↔ one Flowly task. Recurring chores get a new link (and task) per occurrence."""

    integration = models.ForeignKey("integrations.Integration", null=True, blank=True, on_delete=models.CASCADE, related_name="donetick_links")
    account = models.ForeignKey(DonetickAccount, null=True, blank=True, on_delete=models.CASCADE, related_name="links")
    chore_id = models.IntegerField()
    #: Due date of this occurrence (local date, ISO) or "" for chores without one.
    due = models.CharField(max_length=10, blank=True)
    #: Empty when the task was deleted in Flowly: the occurrence is then left alone.
    task = models.ForeignKey("tasks.Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="donetick_links")
    done = models.BooleanField(default=False)
    #: Last values seen in Donetick (name, description, assignee) and flags (skipped).
    seen = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["chore_id", "done"])]


class DonetickOutbox(models.Model):
    """A change to send to Donetick (complete, update, create), retried by the worker."""

    link = models.ForeignKey(DonetickLink, null=True, blank=True, on_delete=models.CASCADE, related_name="outbox")
    integration = models.ForeignKey("integrations.Integration", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    task = models.ForeignKey("tasks.Task", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    action = models.CharField(max_length=10)  # complete | update | create
    payload = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    done = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["done", "next_attempt_at"])]
