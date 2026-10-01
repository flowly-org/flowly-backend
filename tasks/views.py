from django.db.models import Count, Max, Prefetch, Q
from drf_spectacular.utils import extend_schema, inline_serializer
from django.db import transaction
from rest_framework import generics, serializers, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from concierge import notify
from integrations.events import emit, has_outgoing, snapshot
from workspaces.models import Membership
from workspaces.permissions import WorkspaceScopedMixin
from workspaces.serializers import MembershipSerializer

from .filters import TaskFilter
from .models import Project, Tag, TagGroup, Task
from .serializers import ProjectSerializer, TagGroupSerializer, TagSerializer, TaskSerializer


def with_counts(queryset):
    # Subqueries rather than joins: two to-many Counts in one query would multiply each other.
    from django.db.models import IntegerField, OuterRef, Subquery
    from django.db.models.functions import Coalesce

    from .models import Attachment, Comment

    def count(model):
        sub = model.objects.filter(task=OuterRef("pk")).order_by().values("task").annotate(n=Count("id")).values("n")
        return Coalesce(Subquery(sub, output_field=IntegerField()), 0)

    return queryset.annotate(comment_count=count(Comment), attachment_count=count(Attachment))


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
            open_task_count=Count("tasks", filter=Q(tasks__status__is_done=False))
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

    def perform_destroy(self, instance):
        if instance.is_system:
            raise ValidationError({"detail": f'The built-in "{instance.name}" group can\'t be deleted.'})
        instance.delete()


class TagViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    serializer_class = TagSerializer
    filterset_fields = ["group"]

    def get_queryset(self):
        return self.workspace.tags.select_related("group")

    def perform_create(self, serializer):
        siblings = self.workspace.tags.filter(group=serializer.validated_data.get("group"))
        save_appended(serializer, siblings, workspace=self.workspace)

    @transaction.atomic
    def perform_destroy(self, instance):
        if instance.group and instance.group.kind == TagGroup.Kind.STATUS:
            others = instance.group.tags.exclude(pk=instance.pk)
            if not others.exists():
                raise ValidationError({"detail": "A workspace needs at least one status."})
            # Tasks in the deleted status move to the closest equivalent.
            target = others.filter(is_done=instance.is_done).first() or others.first()
            instance.status_tasks.update(status=target)
        instance.delete()


class TaskViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    serializer_class = TaskSerializer
    filterset_class = TaskFilter

    def get_queryset(self):
        return with_counts(self.workspace.tasks.prefetch_related("tags").select_related("status")).order_by("position", "id")

    # Task changes feed the workspace's outgoing integrations (webhooks, Slack, GitHub).

    def perform_create(self, serializer):
        task = serializer.save(workspace=self.workspace, created_by=self.request.user)
        if has_outgoing(self.workspace.id):
            emit(None, task)
        notify.task_changed(None, task, actor=self.request.user)

    def perform_update(self, serializer):
        tracked = has_outgoing(self.workspace.id)
        before = snapshot(serializer.instance) if tracked else None
        before_light = notify.light(serializer.instance)
        task = serializer.save()
        if tracked:
            emit(before, task)
        notify.task_changed(before_light, task, actor=self.request.user)

    def perform_destroy(self, instance):
        before = snapshot(instance) if has_outgoing(self.workspace.id) else None
        instance.delete()
        if before:
            emit(before, None)


class MyTaskListView(generics.ListAPIView):
    """Tasks from every workspace the user belongs to (same filters as the workspace task list).

    Writes still go through /workspaces/<workspace>/tasks/<id>/.
    """

    serializer_class = TaskSerializer
    filterset_class = TaskFilter

    def get_queryset(self):
        return with_counts(
            Task.objects.filter(workspace__memberships__user=self.request.user).prefetch_related("tags")
        ).order_by("position", "id")


class MyContextView(generics.GenericAPIView):
    """Projects, tag groups, tags and members of all the user's workspaces, in one call."""

    @extend_schema(
        responses=inline_serializer(
            "WorkspaceContext",
            {
                "id": serializers.IntegerField(),
                "name": serializers.CharField(),
                "color": serializers.CharField(),
                "my_role": serializers.CharField(),
                "projects": ProjectSerializer(many=True),
                "tag_groups": TagGroupSerializer(many=True),
                "tags": TagSerializer(many=True),
                "members": MembershipSerializer(many=True),
            },
            many=True,
        )
    )
    def get(self, request):
        memberships = (
            Membership.objects.filter(user=request.user)
            .select_related("workspace")
            .order_by("workspace__name", "workspace_id")
        )
        result = []
        for membership in memberships:
            ws = membership.workspace
            context = {"workspace": ws, "request": request}
            projects = ws.projects.annotate(
                open_task_count=Count("tasks", filter=Q(tasks__status__is_done=False))
            )
            groups = ws.tag_groups.prefetch_related(
                Prefetch("tags", queryset=Tag.objects.order_by("position", "name", "id"))
            )
            result.append(
                {
                    "id": ws.id,
                    "name": ws.name,
                    "color": ws.color,
                    "my_role": membership.role,
                    "projects": ProjectSerializer(projects, many=True, context=context).data,
                    "tag_groups": TagGroupSerializer(groups, many=True, context=context).data,
                    "tags": TagSerializer(ws.tags.all(), many=True, context=context).data,
                    "members": MembershipSerializer(
                        ws.memberships.select_related("user"), many=True
                    ).data,
                }
            )
        return Response(result)
