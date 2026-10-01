from django.contrib import admin

from .models import Invitation, Membership, Workspace


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "created_by", "created_at")
    inlines = [MembershipInline]


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "workspace", "role", "created_at")
