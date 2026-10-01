from django.contrib import admin

from .models import Calendar, CalendarAccount, CalendarEvent


@admin.register(CalendarAccount)
class CalendarAccountAdmin(admin.ModelAdmin):
    list_display = ["user", "provider", "email", "status", "last_synced_at"]
    exclude = ["refresh_token", "access_token"]


@admin.register(Calendar)
class CalendarAdmin(admin.ModelAdmin):
    list_display = ["name", "account", "selected"]


@admin.register(CalendarEvent)
class CalendarEventAdmin(admin.ModelAdmin):
    list_display = ["title", "user", "start", "location", "auto_reason", "prep"]
    list_filter = ["prep", "auto_reason"]
    raw_id_fields = ["tasks"]
