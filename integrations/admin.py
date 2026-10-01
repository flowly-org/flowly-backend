from django.contrib import admin

from .models import Delivery, ExternalLink, GitHubApp, Integration


@admin.register(Integration)
class IntegrationAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "workspace", "project", "enabled", "created_at")
    list_filter = ("kind", "enabled")


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ("id", "integration", "direction", "event", "status", "attempts", "created_at")
    list_filter = ("direction", "status")


admin.site.register(ExternalLink)
admin.site.register(GitHubApp)
