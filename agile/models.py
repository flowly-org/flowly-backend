"""Agile rituals, opt-in for a workspace or a single project: sprints, async daily standups, retrospectives."""

from datetime import time

from django.conf import settings
from django.db import models


def weekdays_default():
    return [0, 1, 2, 3, 4]


class AgileSetup(models.Model):
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="agile_setups")
    #: Empty = the whole workspace.
    project = models.ForeignKey("tasks.Project", null=True, blank=True, on_delete=models.CASCADE, related_name="agile_setups")
    enabled = models.BooleanField(default=True)
    sprint_weeks = models.PositiveSmallIntegerField(default=2)
    #: Monday = 0.
    sprint_start = models.PositiveSmallIntegerField(default=0)
    #: Unfinished tasks move to the next sprint when one closes.
    carry_over = models.BooleanField(default=True)
    standup = models.BooleanField(default=True)
    standup_time = models.TimeField(default=time(9, 30))
    standup_days = models.JSONField(default=weekdays_default)
    retro = models.BooleanField(default=True)
    timezone = models.CharField(max_length=64, default="Europe/Rome")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "project"], name="unique_agile_setup")]

    @property
    def label(self):
        return self.project.name if self.project_id else self.workspace.name


class Sprint(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        ACTIVE = "active", "Active"
        CLOSED = "closed", "Closed"

    setup = models.ForeignKey(AgileSetup, on_delete=models.CASCADE, related_name="sprints")
    number = models.PositiveIntegerField()
    goal = models.CharField(max_length=300, blank=True)
    start = models.DateField()
    end = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PLANNED)
    #: Sprint review: numbers at closing (committed, done, carried over, velocity…).
    review = models.JSONField(default=dict, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-start"]
        constraints = [models.UniqueConstraint(fields=["setup", "number"], name="unique_sprint_number")]

    @property
    def name(self):
        return f"Sprint {self.number}"


class StandupEntry(models.Model):
    sprint = models.ForeignKey(Sprint, on_delete=models.CASCADE, related_name="standups")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    date = models.DateField()
    yesterday = models.TextField(blank=True)
    today = models.TextField(blank=True)
    blockers = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "id"]
        constraints = [models.UniqueConstraint(fields=["sprint", "user", "date"], name="unique_standup_entry")]


class StandupRound(models.Model):
    """The day's standup for a setup: asked (members were prompted) and summarised (posted to the team)."""

    setup = models.ForeignKey(AgileSetup, on_delete=models.CASCADE, related_name="rounds")
    date = models.DateField()
    asked_at = models.DateTimeField(null=True, blank=True)
    summarised_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["setup", "date"], name="unique_standup_round")]


class RetroItem(models.Model):
    class Kind(models.TextChoices):
        WENT_WELL = "went_well", "Went well"
        IMPROVE = "improve", "To improve"
        IDEA = "idea", "Idea"

    sprint = models.ForeignKey(Sprint, on_delete=models.CASCADE, related_name="retro_items")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    text = models.CharField(max_length=500)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    votes = models.JSONField(default=list, blank=True)
    #: Turned into an action item.
    task = models.ForeignKey("tasks.Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
