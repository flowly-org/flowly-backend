from django.conf import settings
from django.db import models


class ImportRun(models.Model):
    """One import of a Trello board or an Asana project into a workspace (re-imports skip what's already there)."""

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="imports")
    source = models.CharField(max_length=20)  # trello | asana
    #: The board / project id in the source.
    source_id = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    project = models.ForeignKey("tasks.Project", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    #: External id → Flowly task id.
    items = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
