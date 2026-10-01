"""Task discussion (comments) and attachments (uploaded files, links, cloud documents)."""

from urllib.parse import urlparse

from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import UserSerializer
from workspaces.permissions import WorkspaceScopedMixin

from instance import conf

from .models import Attachment, Comment, uploads_enabled


class CommentSerializer(serializers.ModelSerializer):
    author = UserSerializer(read_only=True)

    class Meta:
        model = Comment
        fields = ["id", "task", "author", "body", "source", "external_author", "external_url", "created_at", "edited_at"]
        read_only_fields = ["id", "task", "author", "source", "external_author", "external_url", "created_at", "edited_at"]

    def validate_body(self, value):
        if not value.strip():
            raise serializers.ValidationError("Write something first.")
        return value.strip()


class AttachmentSerializer(serializers.ModelSerializer):
    added_by = UserSerializer(read_only=True)
    file = serializers.FileField(write_only=True, required=False)
    name = serializers.CharField(required=False, allow_blank=True, max_length=255)

    class Meta:
        model = Attachment
        fields = ["id", "task", "kind", "name", "url", "file", "size", "content_type", "meta", "added_by", "created_at"]
        read_only_fields = ["id", "task", "size", "content_type", "added_by", "created_at"]

    def validate(self, attrs):
        kind = attrs.get("kind")
        if kind == Attachment.Kind.FILE:
            upload = attrs.get("file")
            if not uploads_enabled():
                raise serializers.ValidationError({"file": "File uploads aren't configured on this server."})
            if upload is None:
                raise serializers.ValidationError({"file": "Choose a file."})
            if upload.size > conf.get("ATTACHMENT_MAX_MB") * 1024 * 1024:
                raise serializers.ValidationError({"file": f"Files can be at most {conf.get("ATTACHMENT_MAX_MB")} MB."})
            attrs["name"] = (attrs.get("name") or upload.name)[:255]
            attrs["size"] = upload.size
            attrs["content_type"] = (getattr(upload, "content_type", "") or "")[:150]
            attrs["url"] = ""
        else:
            attrs.pop("file", None)
            url = attrs.get("url", "")
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise serializers.ValidationError({"url": "Enter a link starting with http:// or https://."})
            attrs["name"] = (attrs.get("name") or "").strip() or parsed.netloc + parsed.path.rstrip("/")[:200]
        meta = attrs.get("meta") or {}
        attrs["meta"] = {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))} if isinstance(meta, dict) else {}
        return attrs


class TaskChildMixin(WorkspaceScopedMixin):
    """Views under /workspaces/<ws>/tasks/<task_pk>/…"""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.task = get_object_or_404(self.workspace.tasks, pk=kwargs["task_pk"])

    def can_moderate(self, author_id):
        return author_id == self.request.user.id or self.membership.can_manage


class CommentViewSet(TaskChildMixin, viewsets.ModelViewSet):
    serializer_class = CommentSerializer

    def get_queryset(self):
        return self.task.comments.select_related("author")

    def perform_create(self, serializer):
        from integrations.events import emit_comment

        from concierge import notify

        comment = serializer.save(task=self.task, author=self.request.user)
        emit_comment(comment)
        notify.comment_added(comment)

    def perform_update(self, serializer):
        comment = serializer.instance
        if comment.author_id != self.request.user.id or comment.source != Comment.Source.FLOWLY:
            raise PermissionDenied("You can only edit your own comments.")
        serializer.save(edited_at=timezone.now())

    def perform_destroy(self, instance):
        if not self.can_moderate(instance.author_id):
            raise PermissionDenied("You can only delete your own comments.")
        instance.delete()


class AttachmentViewSet(
    TaskChildMixin, mixins.ListModelMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet
):
    serializer_class = AttachmentSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return self.task.attachments.select_related("added_by")

    def perform_create(self, serializer):
        serializer.save(task=self.task, added_by=self.request.user)

    def perform_destroy(self, instance):
        if not self.can_moderate(instance.added_by_id):
            raise PermissionDenied("You can only remove attachments you added.")
        instance.delete()

    @action(detail=True, methods=["get"])
    def download(self, request, workspace_pk=None, task_pk=None, pk=None):
        """Short-lived URL for an uploaded file (or the link of a cloud document)."""
        attachment = self.get_object()
        if attachment.kind != Attachment.Kind.FILE:
            return Response({"url": attachment.url})
        return Response({"url": attachment.file.url})


@receiver(post_delete, sender=Attachment)
def delete_stored_file(sender, instance, **kwargs):
    if instance.file and uploads_enabled():
        instance.file.delete(save=False)


class PublicConfigView(APIView):
    """Client-side configuration: which attachment sources are available."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        google = (
            {"client_id": conf.get("GOOGLE_CLIENT_ID"), "api_key": conf.get("GOOGLE_API_KEY"), "app_id": conf.get("GOOGLE_APP_ID")}
            if conf.get("GOOGLE_CLIENT_ID") and conf.get("GOOGLE_API_KEY")
            else None
        )
        microsoft = {"client_id": conf.get("MICROSOFT_CLIENT_ID")} if conf.get("MICROSOFT_CLIENT_ID") else None
        return Response(
            {
                "uploads": {"enabled": uploads_enabled(), "max_mb": conf.get("ATTACHMENT_MAX_MB")},
                "google": google,
                "microsoft": microsoft,
            }
        )
