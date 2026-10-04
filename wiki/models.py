from django.db import models


class WikiSource(models.Model):
    """Sync state of a wiki integration (an external wiki, e.g. a GitHub wiki, linked to a workspace or project)."""

    integration = models.OneToOneField("integrations.Integration", on_delete=models.CASCADE, related_name="wiki")
    commit = models.CharField(max_length=64, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    pages = models.PositiveIntegerField(default=0)


class WikiPage(models.Model):
    """One page of a synced wiki, kept as text so Flo and the delegation agent can read it."""

    integration = models.ForeignKey("integrations.Integration", on_delete=models.CASCADE, related_name="wiki_pages")
    path = models.CharField(max_length=500)
    title = models.CharField(max_length=300)
    content = models.TextField()
    url = models.URLField(max_length=700, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title", "id"]
        constraints = [models.UniqueConstraint(fields=["integration", "path"], name="unique_wiki_page")]

    def __str__(self):
        return self.title
