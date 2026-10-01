from django.db.models import Count, Max, Prefetch, Q
from rest_framework import viewsets

from workspaces.permissions import WorkspaceScopedMixin

from .filters import TaskFilter
from .models import Project, Tag, TagGroup, Task
from .serializers import ProjectSerializer, TagGroupSerializer, TagSerializer, TaskSerializer


def save_appended(serializer, queryset, **extra):
    """Save a new ordered item, placing it last unless the client chose a position."""
    if "position" not in serializer.validated_data:
        top = queryset.aggregate(m=Max("position"))["m"]
        extra["position"] = 0 if top is None else top + 1
    serializer.save(**extra)


class ProjectViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    serializer_class = ProjectSerializer
    filterset_fields = ["archived"]

    def get_queryset(self):
        return self.workspace.projects.annotate(
            open_task_count=Count("tasks", filter=~Q(tasks__status=Task.Status.DONE))
        )

    def perform_create(self, serializer):
        save_appended(serializer, self.workspace.projects.all(), workspace=self.workspace)


class TagGroupViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    serializer_class = TagGroupSerializer

    def get_queryset(self):
        return self.workspace.tag_groups.prefetch_related(
            Prefetch("tags", queryset=Tag.objects.order_by("position", "name", "id"))
        )

    def perform_create(self, serializer):
        save_appended(serializer, self.workspace.tag_groups.all(), workspace=self.workspace)


class TagViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    serializer_class = TagSerializer
    filterset_fields = ["group"]

    def get_queryset(self):
        return self.workspace.tags.select_related("group")

    def perform_create(self, serializer):
        siblings = self.workspace.tags.filter(group=serializer.validated_data.get("group"))
        save_appended(serializer, siblings, workspace=self.workspace)


class TaskViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    serializer_class = TaskSerializer
    filterset_class = TaskFilter

    def get_queryset(self):
        return self.workspace.tasks.prefetch_related("tags").order_by("position", "id")

    def perform_create(self, serializer):
        serializer.save(workspace=self.workspace, created_by=self.request.user)
