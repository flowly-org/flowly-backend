"""Tasks delegated to an AI agent (Pro Plus). A delegation is also its own job queue entry."""

from django.conf import settings
from django.db import models


class Delegation(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        ASSESSING = "assessing", "Checking"
        ASSESSED = "assessed", "Checked"
        RUNNING = "running", "Working"
        DONE = "done", "Done"
        PARTIAL = "partial", "Partly done"
        DECLINED = "declined", "Not for the AI"
        FAILED = "failed", "Failed"

    class Verdict(models.TextChoices):
        FULL = "full", "Can do it"
        PARTIAL = "partial", "Can do part of it"
        NO = "no", "Can't do it"

    task = models.ForeignKey("tasks.Task", on_delete=models.CASCADE, related_name="delegations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="delegations")
    #: False = only check whether the AI could do it (suggestions); True = do it.
    run = models.BooleanField(default=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True)
    verdict = models.CharField(max_length=8, choices=Verdict.choices, blank=True)
    plan = models.TextField(blank=True)
    reason = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    left_for_you = models.TextField(blank=True)
    steps = models.PositiveSmallIntegerField(default=0)
    tokens = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    ACTIVE = (Status.QUEUED, Status.ASSESSING, Status.RUNNING)
