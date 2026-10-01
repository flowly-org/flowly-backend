from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

from workspaces.models import Workspace

hex_color = RegexValidator(r"^#[0-9a-fA-F]{6}$", "Use a hex color like #4f46e5.")


class Project(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    color = models.CharField(max_length=7, default="#4f46e5", validators=[hex_color])
    archived = models.BooleanField(default=False)
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="unique_project_name")
        ]

    def __str__(self):
        return self.name


class TagGroup(models.Model):
    """A dimension (e.g. "Context", "Energy") whose tags can become swimlanes.

    A task carries at most one tag from each group. Every workspace also has two
    built-in groups: STATUS (its tags are the board columns, stored on Task.status)
    and PRIORITY (stored on Task.priority). They're edited like any other group but
    can't be deleted.
    """

    class Kind(models.TextChoices):
        CUSTOM = "custom", "Custom"
        STATUS = "status", "Status"
        PRIORITY = "priority", "Priority"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="tag_groups")
    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.CUSTOM)
    position = models.IntegerField(default=0)

    class Meta:
        ordering = ["position", "name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="unique_tag_group_name"),
            models.UniqueConstraint(
                fields=["workspace", "kind"],
                condition=~models.Q(kind="custom"),
                name="unique_system_tag_group",
            ),
        ]

    @property
    def is_system(self):
        return self.kind != self.Kind.CUSTOM

    def __str__(self):
        return self.name


class Tag(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="tags")
    group = models.ForeignKey(
        TagGroup, null=True, blank=True, on_delete=models.CASCADE, related_name="tags"
    )
    name = models.CharField(max_length=60)
    color = models.CharField(max_length=7, default="#64748b", validators=[hex_color])
    position = models.IntegerField(default=0)
    is_done = models.BooleanField(
        default=False, help_text="Status tags only: tasks in this status count as finished."
    )

    class Meta:
        ordering = ["position", "name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "group", "name"], name="unique_tag_name")
        ]

    def __str__(self):
        return f"{self.group}: {self.name}" if self.group_id else self.name


class Task(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="tasks")
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    status = models.ForeignKey(
        Tag, on_delete=models.PROTECT, related_name="status_tasks", help_text="A tag of the STATUS group."
    )
    priority = models.ForeignKey(
        Tag,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="priority_tasks",
        help_text="A tag of the PRIORITY group; empty = no priority.",
    )
    planned_date = models.DateField(
        null=True, blank=True, help_text="The day this task is planned for; empty = backlog."
    )
    due_date = models.DateField(null=True, blank=True)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_tasks",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name="tasks")
    position = models.FloatField(default=0, help_text="Sort key within a board cell.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position", "id"]
        indexes = [
            models.Index(fields=["workspace", "planned_date"]),
            models.Index(fields=["workspace", "assignee", "planned_date"]),
        ]

    def __str__(self):
        return self.title
