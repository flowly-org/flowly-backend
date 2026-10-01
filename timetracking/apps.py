from django.apps import AppConfig


class TimeTrackingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "timetracking"

    def ready(self):
        from . import mcp  # noqa: F401  registers the MCP time tools
