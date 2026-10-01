from django.apps import AppConfig


class McpServerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mcpserver"

    def ready(self):
        from . import search  # noqa: F401  registers search/fetch
