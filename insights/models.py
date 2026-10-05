from django.conf import settings
from django.db import models


class WeeklyReview(models.Model):
    """A person's week in numbers, with Flo's written review (Pro)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="weekly_reviews")
    week_start = models.DateField()
    stats = models.JSONField(default=dict)
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-week_start"]
        constraints = [models.UniqueConstraint(fields=["user", "week_start"], name="unique_weekly_review")]
