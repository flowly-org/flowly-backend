from django.contrib import admin

from .models import OAuthClient, OAuthGrant


@admin.register(OAuthClient)
class OAuthClientAdmin(admin.ModelAdmin):
    list_display = ("name", "client_id", "created_at")
    readonly_fields = ("secret_hash",)


@admin.register(OAuthGrant)
class OAuthGrantAdmin(admin.ModelAdmin):
    list_display = ("user", "client", "created_at", "last_used_at", "revoked")
    readonly_fields = ("access_hash", "refresh_hash")
