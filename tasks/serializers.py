from django.contrib.auth import get_user_model
from django.db.models import Max, Q
from django.utils import timezone
from rest_framework import serializers

from .models import Project, Tag, TagGroup, Task
from .services import default_status

User = get_user_model()


class WorkspaceScopedSerializer(serializers.ModelSerializer):
    """Restricts related-field querysets to the workspace in the serializer context."""

    scoped_fields = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        workspace = self.context.get("workspace")
        for field_name, queryset_fn in self.scoped_fields.items():
            field = self.fields.get(field_name)
            if field is None:
                continue
            target = field.child_relation if isinstance(field, serializers.ManyRelatedField) else field
            target.queryset = queryset_fn(workspace) if workspace else target.queryset.none()


class ProjectSerializer(serializers.ModelSerializer):
    open_task_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Project
        fields = ["id", "name", "description", "color", "archived", "position", "open_task_count", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_name(self, value):
        qs = Project.objects.filter(workspace=self.context["workspace"], name__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A project with this name already exists.")
        return value


class TagSerializer(WorkspaceScopedSerializer):
    scoped_fields = {"group": lambda ws: TagGroup.objects.filter(workspace=ws)}

    class Meta:
        model = Tag
        fields = ["id", "name", "color", "group", "position", "is_done"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        group = attrs.get("group", self.instance.group if self.instance else None)
        if self.instance and "group" in attrs and attrs["group"] != self.instance.group:
            old = self.instance.group
            if (old and old.is_system) or (group and group.is_system):
                raise serializers.ValidationError({"group": "Status and priority tags can't change group."})
        if attrs.get("is_done") and not (group and group.kind == TagGroup.Kind.STATUS):
            attrs["is_done"] = False
        name = attrs.get("name", self.instance.name if self.instance else None)
        qs = Tag.objects.filter(workspace=self.context["workspace"], group=group, name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({"name": "This tag already exists in that group."})
        return attrs


class TagGroupSerializer(serializers.ModelSerializer):
    tags = TagSerializer(many=True, read_only=True)

    class Meta:
        model = TagGroup
        fields = ["id", "name", "kind", "position", "tags"]
        read_only_fields = ["id", "kind"]

    def validate_name(self, value):
        qs = TagGroup.objects.filter(workspace=self.context["workspace"], name__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A tag group with this name already exists.")
        return value


class TaskSerializer(WorkspaceScopedSerializer):
    scoped_fields = {
        "project": lambda ws: Project.objects.filter(workspace=ws),
        "assignee": lambda ws: User.objects.filter(memberships__workspace=ws),
        "tags": lambda ws: Tag.objects.filter(workspace=ws).filter(
            Q(group__isnull=True) | Q(group__kind=TagGroup.Kind.CUSTOM)
        ),
        "status": lambda ws: Tag.objects.filter(workspace=ws, group__kind=TagGroup.Kind.STATUS),
        "priority": lambda ws: Tag.objects.filter(workspace=ws, group__kind=TagGroup.Kind.PRIORITY),
    }

    status = serializers.PrimaryKeyRelatedField(
        queryset=Tag.objects.all(), required=False, help_text="Id of a tag in the workspace's Status group."
    )
    priority = serializers.PrimaryKeyRelatedField(
        queryset=Tag.objects.all(),
        allow_null=True,
        required=False,
        help_text="Id of a tag in the workspace's Priority group, or null.",
    )

    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all(), allow_null=True, required=False)
    assignee = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True, required=False)
    tags = serializers.PrimaryKeyRelatedField(queryset=Tag.objects.all(), many=True, required=False)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    comment_count = serializers.SerializerMethodField()
    attachment_count = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            "id",
            "workspace",
            "title",
            "description",
            "status",
            "priority",
            "project",
            "assignee",
            "tags",
            "planned_date",
            "due_date",
            "position",
            "created_by",
            "created_at",
            "updated_at",
            "completed_at",
            "comment_count",
            "attachment_count",
        ]
        read_only_fields = ["id", "workspace", "created_by", "created_at", "updated_at", "completed_at"]

    def get_comment_count(self, obj):
        value = getattr(obj, "comment_count", None)
        return obj.comments.count() if value is None else value

    def get_attachment_count(self, obj):
        value = getattr(obj, "attachment_count", None)
        return obj.attachments.count() if value is None else value

    def validate_tags(self, tags):
        seen = {}
        for tag in tags:
            if tag.group_id is None:
                continue
            if tag.group_id in seen:
                raise serializers.ValidationError(
                    f'Only one tag per group: "{seen[tag.group_id].name}" and "{tag.name}" '
                    f'are both in "{tag.group.name}".'
                )
            seen[tag.group_id] = tag
        return tags

    def _apply_status(self, validated_data, instance=None):
        status = validated_data.get("status")
        if status is None:
            return
        if status.is_done and (instance is None or not instance.status.is_done):
            validated_data["completed_at"] = timezone.now()
        elif not status.is_done:
            validated_data["completed_at"] = None

    def create(self, validated_data):
        workspace = self.context["workspace"]
        if "status" not in validated_data:
            status = default_status(workspace)
            if status is None:
                raise serializers.ValidationError({"status": "This workspace has no statuses."})
            validated_data["status"] = status
        if "position" not in validated_data:
            top = Task.objects.filter(workspace=workspace).aggregate(m=Max("position"))["m"] or 0
            validated_data["position"] = top + 1024
        self._apply_status(validated_data)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        self._apply_status(validated_data, instance)
        return super().update(instance, validated_data)
