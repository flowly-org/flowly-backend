"""Task links (/t/<ref>), sharing with people, and bulk creation."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import NotAuthenticated, NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from workspaces.permissions import WorkspaceScopedMixin

from .access import link_access
from .discussion import CommentSerializer, TaskChildMixin
from .models import Attachment, Task, TaskShare

User = get_user_model()


class TaskShareSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    registered = serializers.SerializerMethodField()

    class Meta:
        model = TaskShare
        fields = ["id", "email", "name", "registered", "created_at"]

    def get_name(self, obj):
        return obj.user.full_name if obj.user_id else ""

    def get_registered(self, obj):
        return obj.user_id is not None


class TaskSharesView(TaskChildMixin, APIView):
    """GET/POST /workspaces/<ws>/tasks/<id>/shares/  ·  DELETE …/shares/<share_id>/"""

    def get(self, request, workspace_pk, task_pk):
        return Response(TaskShareSerializer(self.task.shares.select_related("user"), many=True).data)

    def post(self, request, workspace_pk, task_pk):
        email = serializers.EmailField().to_internal_value(request.data.get("email", "")).lower()
        task = self.task
        user = User.objects.filter(email__iexact=email).first()
        share, created = TaskShare.objects.get_or_create(task=task, email=email, defaults={"user": user, "created_by": request.user})
        if task.visibility != Task.Visibility.SHAREABLE and task.visibility != Task.Visibility.PUBLIC:
            task.visibility = Task.Visibility.SHAREABLE
            task.save(update_fields=["visibility"])
        if created and user is not None and user.id != request.user.id:
            from concierge import notify

            notify.create(
                user, "shared", f"{request.user.full_name or request.user.email} shared “{task.title}” with you",
                task.description[:200], task=task, data={"ref": task.ref},
            )
        return Response(TaskShareSerializer(share).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def delete(self, request, workspace_pk, task_pk, share_id):
        get_object_or_404(self.task.shares, pk=share_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class BulkCreateView(WorkspaceScopedMixin, APIView):
    """POST /workspaces/<ws>/tasks/bulk/ {"tasks": [{...}, ...]}: all or nothing."""

    def post(self, request, workspace_pk):
        from .bulk import create_many
        from .serializers import TaskSerializer

        items = request.data.get("tasks")
        if not isinstance(items, list) or not items:
            raise ValidationError({"tasks": "Send a non-empty list of tasks."})
        if len(items) > 200:
            raise ValidationError({"tasks": "At most 200 tasks at once."})
        created = create_many(request.user, self.workspace, items, request=request)
        context = {"workspace": self.workspace, "request": request}
        return Response(TaskSerializer(created, many=True, context=context).data, status=status.HTTP_201_CREATED)


# --- /t/<ref> -------------------------------------------------------------------


def link_payload(task: Task, access: str, user) -> dict:
    from integrations.events import task_payload

    payload = task_payload(task)
    payload.pop("url", None)
    if access == "public":
        payload["assignee"] = {"name": task.assignee.full_name or "Someone"} if task.assignee_id else None
    attachments = [
        {"id": a.id, "kind": a.kind, "name": a.name, "url": a.url if a.kind != Attachment.Kind.FILE else "", "size": a.size}
        for a in task.attachments.all()
    ]
    comments = CommentSerializer(task.comments.select_related("author"), many=True).data
    if access == "public":
        for c in comments:  # don't publish emails
            if c["author"]:
                c["author"] = {"id": None, "email": "", "full_name": c["author"]["full_name"] or "Someone"}
    return {
        "access": access,
        "task": payload,
        "workspace": {"id": task.workspace_id, "name": task.workspace.name, "color": task.workspace.color} if access == "member" else {"name": task.workspace.name, "color": task.workspace.color},
        "comments": comments,
        "attachments": attachments,
        "can_comment": access in ("member", "shared"),
        "can_edit": access == "member",
    }


class TaskLinkMixin:
    permission_classes = [permissions.AllowAny]

    def resolve(self, request, ref):
        task = Task.objects.select_related("workspace", "project", "status", "priority", "assignee").filter(ref=ref).first()
        user = request.user if request.user.is_authenticated else None
        access = link_access(task, user) if task else None
        if access is None:
            if user is None and task is not None:
                raise NotAuthenticated("Sign in to open this task.")
            raise NotFound("This task doesn't exist or isn't shared with you.")
        return task, access


class TaskLinkView(TaskLinkMixin, APIView):
    def get(self, request, ref):
        task, access = self.resolve(request, ref)
        return Response(link_payload(task, access, request.user))


class TaskLinkCommentView(TaskLinkMixin, APIView):
    def post(self, request, ref):
        task, access = self.resolve(request, ref)
        if not request.user.is_authenticated:
            raise NotAuthenticated("Sign in to comment.")
        if access not in ("member", "shared"):
            raise PermissionDenied("Sign in with an account this task is shared with to comment.")
        serializer = CommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.save(task=task, author=request.user)
        from concierge import notify
        from integrations.events import emit_comment

        emit_comment(comment)
        notify.comment_added(comment)
        return Response(CommentSerializer(comment).data, status=status.HTTP_201_CREATED)


class TaskLinkAttachmentView(TaskLinkMixin, APIView):
    def get(self, request, ref, attachment_id):
        task, _ = self.resolve(request, ref)
        attachment = get_object_or_404(task.attachments, pk=attachment_id)
        return Response({"url": attachment.file.url if attachment.kind == Attachment.Kind.FILE else attachment.url})


class SharedWithMeView(APIView):
    def get(self, request):
        user = request.user
        tasks = (
            Task.objects.filter(visibility__in=[Task.Visibility.SHAREABLE, Task.Visibility.PUBLIC])
            .filter(shares__user=user)
            .select_related("workspace", "status", "project")
            .distinct()
            .order_by("-updated_at")[:100]
        )
        return Response([
            {"ref": t.ref, "title": t.title, "workspace": t.workspace.name, "status": t.status.name,
             "status_done": t.status.is_done, "project": t.project.name if t.project_id else None,
             "planned_date": t.planned_date, "due_date": t.due_date, "updated_at": t.updated_at}
            for t in tasks
        ])
