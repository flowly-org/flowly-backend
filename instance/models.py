from django.conf import settings
from django.db import models


class InstanceSetting(models.Model):
    """An admin-set override of a runtime setting (see instance/conf.py). Secrets are stored encrypted."""

    key = models.CharField(max_length=64, primary_key=True)
    value = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    def __str__(self):
        return self.key
