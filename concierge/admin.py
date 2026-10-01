from django.contrib import admin

from .models import AIUsage, ConciergeProfile, Notification


@admin.register(ConciergeProfile)
class ConciergeProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "enabled", "telegram_username", "timezone", "brief_time", "alerts")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "kind", "title", "created_at", "read_at", "telegram")
    list_filter = ("kind", "telegram")


@admin.register(AIUsage)
class AIUsageAdmin(admin.ModelAdmin):
    list_display = ("user", "date", "provider", "requests", "prompt_tokens", "completion_tokens")
    list_filter = ("provider",)
