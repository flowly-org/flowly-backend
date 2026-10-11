from django.contrib.auth import get_user_model
from django.db.models import Max, Q
from django.utils import timezone
from rest_framework import serializers

from .models import Attribute, Project, Tag, TagGroup, Task, TaskDependency
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
    #: False when the project opted out of billing (ProjectBilling.mode == "off"). Owners/admins can change it.
    # None default: a form-encoded request without the field must not opt the project out of billing.
    billable = serializers.BooleanField(required=False, allow_null=True, default=None)
    # Explicit default: form-encoded requests that omit a boolean would otherwise read it as False.
    time_tracking = serializers.BooleanField(required=False, default=True)

    def to_representation(self, obj):
        data = super().to_representation(obj)
        try:
            data["billable"] = obj.billing.mode != "off"
        except Exception:
            data["billable"] = True
        return data

    def save(self, **kwargs):
        billable = self.validated_data.pop("billable", None)
        project = super().save(**kwargs)
        if billable is not None:
            from timetracking.models import ProjectBilling, TimeEntry

            membership = self.context.get("membership")
            if membership is not None and not membership.can_manage:
                from rest_framework.exceptions import PermissionDenied

                raise PermissionDenied("Only workspace owners and admins can change billing.")
            pb, _ = ProjectBilling.objects.get_or_create(project=project)
            if not billable:
                pb.mode = ProjectBilling.Mode.OFF
                TimeEntry.objects.filter(project=project, invoice__isnull=True).update(billable=False)
            elif pb.mode == ProjectBilling.Mode.OFF:
                pb.mode = ProjectBilling.Mode.INHERIT
            pb.save()
        return project

    class Meta:
        model = Project
        fields = ["id", "name", "description", "color", "archived", "time_tracking", "billable", "position",
                  "open_task_count", "created_at"]
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
        "blocked_by": lambda ws: Task.objects.filter(workspace=ws),
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
    blocked_by = serializers.PrimaryKeyRelatedField(queryset=Task.objects.none(), many=True, required=False, write_only=True,
                                                    help_text="Ids of tasks (same workspace) this one waits for.")
    blocking = serializers.SerializerMethodField()
    blocked = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            "id",
            "ref",
            "visibility",
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
            "checklist",
            "estimate_minutes",
            "recurrence",
            "series",
            "sprint",
            "blocked_by",
            "blocking",
            "blocked",
            "comment_count",
            "attachment_count",
        ]
        read_only_fields = ["id", "ref", "workspace", "created_by", "created_at", "updated_at", "completed_at", "series", "sprint"]

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

    def _links(self, obj, attr, field):
        cache = getattr(obj, "_prefetched_objects_cache", {})
        rows = cache[attr] if attr in cache else getattr(obj, attr).all()
        return rows, field

    def get_blocking(self, obj):
        if not obj.pk:
            return []
        rows, _ = self._links(obj, "blocking_links", "task_id")
        return [r.task_id for r in rows]

    def get_blocked(self, obj):
        if not obj.pk:
            return False
        rows, _ = self._links(obj, "blocked_by_links", "depends_on")
        return any(not r.depends_on.status.is_done for r in rows)

    def validate_blocked_by(self, blockers):
        from .dependencies import would_loop

        me = self.instance.pk if self.instance else None
        for b in blockers:
            if me and b.pk == me:
                raise serializers.ValidationError("A task can't wait for itself.")
            if me and would_loop(me, b.pk):
                raise serializers.ValidationError(f"“{b.title}” already waits for this task (that would be a loop).")
        if len(blockers) > 50:
            raise serializers.ValidationError("At most 50 blockers.")
        return blockers

    def validate_checklist(self, items):
        import secrets

        if items is None:
            return []
        if not isinstance(items, list) or len(items) > 100:
            raise serializers.ValidationError("A list of at most 100 items.")
        out, seen = [], set()
        for item in items:
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                raise serializers.ValidationError("Each item needs a text.")
            key = str(item.get("id") or "")[:12] or secrets.token_hex(4)
            if key in seen:
                key = secrets.token_hex(4)
            seen.add(key)
            out.append({"id": key, "text": str(item["text"]).strip()[:300], "done": bool(item.get("done"))})
        return out

    def validate_estimate_minutes(self, value):
        if value is not None and not 1 <= value <= 7 * 24 * 60:
            raise serializers.ValidationError("Between 1 minute and a week.")
        return value

    def validate_recurrence(self, value):
        from .recurrence import clean

        try:
            return clean(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from None

    def to_representation(self, obj):
        from .recurrence import is_rule

        data = super().to_representation(obj)
        if not is_rule(data.get("recurrence")):
            data["recurrence"] = None  # internal marker of an occurrence already repeated
        if obj.pk:
            rows, _ = self._links(obj, "blocked_by_links", "depends_on_id")
            data["blocked_by"] = [r.depends_on_id for r in rows]
            data["blocked_by_info"] = [{"id": r.depends_on_id, "ref": r.depends_on.ref, "title": r.depends_on.title,
                                        "done": r.depends_on.status.is_done} for r in rows]
        else:
            data["blocked_by"], data["blocked_by_info"] = [], []
        nxt = getattr(obj, "_next_occurrence", None)
        if nxt is not None:
            data["next_occurrence"] = {"id": nxt.id, "planned_date": nxt.planned_date, "due_date": nxt.due_date}
        self._attributes(obj, data)
        return data

    def _attributes(self, obj, data):
        """{{key}} placeholders shown with their values (the raw text in *_template, for editing), @key mentions listed."""
        from . import attributes

        text = f"{obj.title}\n{obj.description}"
        if "{{" not in text and "@" not in text:
            return
        cache = self.context.setdefault("_attribute_values", attributes.Values())
        values = cache.for_task(obj.workspace_id, obj.project_id)
        if not values:
            return
        if "{{" in obj.title:
            data["title_template"], data["title"] = obj.title, attributes.render(obj.title, values, attributes.TITLE_CHARS)
        if "{{" in obj.description:
            data["description_template"], data["description"] = obj.description, attributes.render(obj.description, values)
        used = attributes.mentioned(text, values)
        if used:
            data["mentions"] = [{"key": k, "value": v} for k, v in used.items()]

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
        blockers = validated_data.pop("blocked_by", None)
        task = super().create(validated_data)
        if blockers:
            from .dependencies import set_blockers

            set_blockers(task, blockers)
        return task

    def update(self, instance, validated_data):
        from . import recurrence

        was_done = instance.status.is_done
        self._apply_status(validated_data, instance)
        if "recurrence" in validated_data and validated_data["recurrence"] is None and not recurrence.is_rule(instance.recurrence):
            validated_data.pop("recurrence")  # keep the "already repeated" marker when a form sends no rule
        blockers = validated_data.pop("blocked_by", None)
        task = super().update(instance, validated_data)
        if blockers is not None:
            from .dependencies import set_blockers

            set_blockers(task, blockers)
            getattr(task, "_prefetched_objects_cache", {}).pop("blocked_by_links", None)
        if task.status.is_done and not was_done:
            from .dependencies import notify_unblocked

            request = self.context.get("request")
            notify_unblocked(task, getattr(request, "user", None))
        if task.status.is_done and not was_done and recurrence.is_rule(task.recurrence):
            request = self.context.get("request")
            task._next_occurrence = recurrence.spawn_next(task, getattr(request, "user", None))
        elif was_done and not task.status.is_done:
            recurrence.undo_spawn(task)
        return task


class AttributeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Attribute
        fields = ["id", "project", "key", "value", "updated_at"]
        read_only_fields = ["id", "updated_at"]

    def validate_key(self, value):
        from .attributes import clean_key

        try:
            return clean_key(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from None

    def validate_project(self, value):
        workspace = self.context["workspace"]
        if value is not None and value.workspace_id != workspace.id:
            raise serializers.ValidationError("Pick a project of this workspace.")
        return value

    def validate(self, attrs):
        workspace = self.context["workspace"]
        key = attrs.get("key", getattr(self.instance, "key", None))
        project = attrs.get("project", getattr(self.instance, "project", None))
        clash = Attribute.objects.filter(workspace=workspace, project=project, key=key)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError({"key": f"“{key}” already exists here."})
        return attrs
