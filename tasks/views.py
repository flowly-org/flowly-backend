from django.db.models import Count, Max, Prefetch, Q
from drf_spectacular.utils import extend_schema, inline_serializer
from django.db import transaction
from rest_framework import generics, serializers, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from concierge import notify
from integrations.events import emit, has_outgoing, snapshot
from workspaces.models import Membership
from workspaces.permissions import WorkspaceScopedMixin
from workspaces.serializers import MembershipSerializer

from .access import member_visible_q
from .filters import TaskFilter
from .models import Reminder, Tag, TagGroup, Task
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
        return with_counts(
            self.workspace.tasks.filter(member_visible_q(self.request.user)).prefetch_related("tags").select_related("status")
        ).order_by("position", "id")

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


class TaskRemindersView(WorkspaceScopedMixin, APIView):
    """GET/POST /api/workspaces/<ws>/tasks/<pk>/reminders/ — my reminders on a task; DELETE …/reminders/<id>/."""

    def task(self, pk):
        task = self.workspace.tasks.filter(member_visible_q(self.request.user)).filter(pk=pk).first()
        if task is None:
            from django.http import Http404

            raise Http404
        return task

    @staticmethod
    def rows(task, user):
        return [{"id": r.id, "remind_at": r.remind_at, "sent": r.sent_at is not None}
                for r in Reminder.objects.filter(task=task, user=user).order_by("remind_at")]

    def get(self, request, workspace_pk=None, task_pk=None):
        return Response(self.rows(self.task(task_pk), request.user))

    def post(self, request, workspace_pk=None, task_pk=None):
        from django.utils import timezone
        from django.utils.dateparse import parse_datetime

        task = self.task(task_pk)
        when = parse_datetime(str(request.data.get("remind_at") or ""))
        if when is None:
            raise ValidationError({"remind_at": "Give a date and time, e.g. 2026-10-06T15:00."})
        if timezone.is_naive(when):
            from calendars.sync import user_tz

            when = when.replace(tzinfo=user_tz(request.user))
        if when < timezone.now() - timezone.timedelta(minutes=1):
            raise ValidationError({"remind_at": "That moment has already passed."})
        if Reminder.objects.filter(user=request.user, sent_at__isnull=True).count() >= 500:
            raise ValidationError({"remind_at": "Too many pending reminders."})
        Reminder.objects.get_or_create(task=task, user=request.user, remind_at=when)
        return Response(self.rows(task, request.user), status=201)

    def delete(self, request, workspace_pk=None, task_pk=None, rid=None):
        task = self.task(task_pk)
        Reminder.objects.filter(task=task, user=request.user, pk=rid).delete()
        return Response(self.rows(task, request.user))


class DayLoadView(APIView):
    """GET /api/my/day-load/?date=YYYY-MM-DD — estimated work planned vs free time (capacity minus calendar)."""

    def get(self, request):
        from datetime import date as date_cls

        from .load import day_load

        try:
            day = date_cls.fromisoformat(request.query_params.get("date", ""))
        except ValueError:
            day = None
        return Response(day_load(request.user, day))


class MyTaskListView(generics.ListAPIView):
    """Tasks from every workspace the user belongs to (same filters as the workspace task list).

    Writes still go through /workspaces/<workspace>/tasks/<id>/.
    """

    serializer_class = TaskSerializer
    filterset_class = TaskFilter

    def get_queryset(self):
        user = self.request.user
        return with_counts(
            Task.objects.filter(workspace__memberships__user=user).filter(member_visible_q(user)).prefetch_related("tags")
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
                    "time_tracking": ws.time_tracking,
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



class QuickAddView(APIView):
    """POST {"text", "workspace"?, "day"?: "today"|"tomorrow"|"backlog"|YYYY-MM-DD}: one task or a whole list in the
    bulk-add format (bullets, [x], indented notes, !priority #tag +Project @date). Used by the desktop and mobile apps.
    The tasks are assigned to you; without a workspace they go to Personal (or your first workspace)."""

    def post(self, request):
        from concierge.notify import local_today
        from concierge.tools import parse_day
        from workspaces.models import Workspace

        from .bulk import create_from_text

        text = (request.data.get("text") or "").strip()
        if not text:
            return Response({"detail": "Write at least one task."}, status=400)
        if len(text) > 20000:
            return Response({"detail": "That list is too long."}, status=400)
        mine = Workspace.objects.filter(memberships__user=request.user)
        ws_id = request.data.get("workspace")
        workspace = (mine.filter(pk=ws_id).first() if ws_id else None) or (
            None if ws_id else (mine.filter(name__iexact="Personal").first() or mine.order_by("id").first()))
        if workspace is None:
            return Response({"detail": "Workspace not found."}, status=404)
        today = local_today(request.user)
        day = (request.data.get("day") or "today").strip().lower()
        default = parse_day(day, today) if day else today
        tasks = create_from_text(request.user, workspace, text, today, default_date=default)
        if not tasks:
            return Response({"detail": "No tasks found in that text."}, status=400)
        return Response({
            "workspace": {"id": workspace.id, "name": workspace.name},
            "created": [{"id": t.id, "ref": t.ref, "title": t.title, "planned_date": t.planned_date, "done": t.status.is_done}
                        for t in tasks],
        }, status=201)
