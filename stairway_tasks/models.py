"""A task handed to an AI agent on a Stairway server: the experiment it works in, and how it's going."""

import secrets

from django.conf import settings
from django.db import models


def new_token():
    return secrets.token_urlsafe(18)


def new_secret():
    return secrets.token_hex(24)


class StairwayRun(models.Model):
    class Status(models.TextChoices):
        WORKING = "working", "Working"
        WAITING = "waiting", "Waiting for your OK"
        REVIEW = "review", "Ready for review"
        FAILED = "failed", "Failed"
        STOPPED = "stopped", "Stopped"

    task = models.ForeignKey("tasks.Task", on_delete=models.CASCADE, related_name="stairway_runs")
    integration = models.ForeignKey("integrations.Integration", null=True, on_delete=models.SET_NULL, related_name="+")
    experiment = models.CharField(max_length=63)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.WORKING)
    #: Public id in the callback URL; the secret signs Stairway's callbacks (HMAC-SHA256).
    token = models.CharField(max_length=32, unique=True, default=new_token)
    secret = models.CharField(max_length=64, default=new_secret)
    session_url = models.URLField(max_length=500, blank=True)
    last_reply = models.TextField(blank=True)
    #: What the agent asks permission for (its screen), while status is WAITING.
    waiting_for = models.TextField(blank=True)
    replies = models.PositiveIntegerField(default=0)
    #: Attachments made from the deliverables of the latest reply (replaced by the next one).
    attachment_ids = models.JSONField(default=list, blank=True)
    error = models.CharField(max_length=500, blank=True)
    #: A reply arrived: the worker fetches its deliverables.
    collect = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
