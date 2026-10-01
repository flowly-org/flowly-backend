from django.contrib import admin

from .models import Project, Tag, TagGroup, Task


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "archived")
    list_filter = ("workspace", "archived")


@admin.register(TagGroup)
class TagGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "workspace")


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "group", "workspace")
    list_filter = ("workspace", "group")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "workspace", "project", "status", "assignee", "planned_date")
    list_filter = ("workspace", "status", "planned_date")
    search_fields = ("title",)
    filter_horizontal = ("tags",)
