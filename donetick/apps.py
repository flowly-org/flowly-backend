from django.apps import AppConfig


class DonetickConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "donetick"

    def ready(self):
        from . import signals  # noqa: F401
