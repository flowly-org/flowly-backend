"""Task templates: save a set of tasks once, create them all (with dates relative to a start day) in one go."""

from datetime import date

from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from workspaces.permissions import WorkspaceScopedMixin

from .access import member_visible_q
from .bulk import create_many, parse
from .models import Project, TaskTemplate
from .serializers import TaskSerializer


class TemplateSerializer(serializers.ModelSerializer):
    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.none(), allow_null=True, required=False)
    count = serializers.SerializerMethodField()

    class Meta:
        model = TaskTemplate
        fields = ["id", "name", "description", "body", "project", "count", "created_by", "created_at", "updated_at"]
        read_only_fields = ["id", "count", "created_by", "created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ws = self.context.get("workspace")
        self.fields["project"].queryset = Project.objects.filter(workspace=ws) if ws else Project.objects.none()

    def get_count(self, obj):
        return sum(1 for line in obj.body.splitlines() if line.strip() and not line.startswith((" ", "\t")))

    def validate_body(self, value):
        if not value.strip():
            raise ValidationError("Write at least one task.")
        if len(value) > 20000 or len(value.splitlines()) > 300:
            raise ValidationError("Too long: at most 300 lines.")
        return value


def text_from_tasks(tasks) -> str:
    """Turn existing tasks into template lines, with dates relative to the earliest one."""
    tasks = list(tasks)
    start = min((t.planned_date for t in tasks if t.planned_date), default=None)
    lines = []
    for t in tasks:
        bits = [t.title]
        if t.priority_id:
            bits.append("!" + t.priority.name.replace(" ", "-"))
        if t.project_id:
            bits.append("+" + t.project.name.replace(" ", "-"))
        if t.planned_date and start:
            bits.append(f"@+{(t.planned_date - start).days}")
        if t.estimate_minutes:
            h, m = divmod(t.estimate_minutes, 60)
            bits.append(f"~{h}h{m:02d}" if h and m else f"~{h}h" if h else f"~{m}m")
        lines.append("- " + " ".join(bits))
        for note in (t.description or "").splitlines():
            if note.strip():
                lines.append("  " + note)
        for item in t.checklist or []:
            lines.append("  [ ] " + item["text"])
    return "\n".join(lines)


class TemplateViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    """Templates of a workspace (every member can use and edit them). POST …/<id>/apply/ {start, project?, assignee?}."""

    serializer_class = TemplateSerializer

    def get_queryset(self):
        return self.workspace.task_templates.all()

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "workspace": self.workspace}

    def perform_create(self, serializer):
        serializer.save(workspace=self.workspace, created_by=self.request.user)

    @action(detail=False, methods=["post"], url_path="from-tasks")
    def from_tasks(self, request, workspace_pk=None):
        ids = request.data.get("tasks") or []
        tasks = (self.workspace.tasks.filter(member_visible_q(request.user)).filter(pk__in=ids)
                 .select_related("priority", "project").order_by("planned_date", "position"))
        if not tasks:
            raise ValidationError({"tasks": "Pick the tasks to save."})
        serializer = self.get_serializer(data={"name": request.data.get("name") or tasks[0].title, "body": text_from_tasks(tasks)})
        serializer.is_valid(raise_exception=True)
        serializer.save(workspace=self.workspace, created_by=request.user)
        return Response(serializer.data, status=201)

    @action(detail=True, methods=["post"])
    def apply(self, request, workspace_pk=None, pk=None):
        template = self.get_object()
        try:
            start = date.fromisoformat(str(request.data.get("start") or date.today().isoformat())[:10])
        except ValueError:
            raise ValidationError({"start": "Use YYYY-MM-DD."}) from None
        project = request.data.get("project", template.project_id)
        assignee = request.data.get("assignee", request.user.id)
        payloads = []
        for d in parse(template.body, self.workspace, start):
            when = start if d["date"] is ... else d["date"]
            payloads.append({
                "title": d["title"], "description": d["notes"], "assignee": assignee,
                "planned_date": when.isoformat() if when else None,
                "project": d["project"].id if d["project"] else project,
                "priority": d["priority"].id if d["priority"] else None,
                "tags": [t.id for t in d["tags"]],
                "estimate_minutes": d.get("estimate"), "checklist": d.get("checklist") or [],
            })
        if not payloads:
            raise ValidationError({"detail": "This template has no tasks."})
        created = create_many(request.user, self.workspace, payloads, request=request)
        return Response(TaskSerializer(created, many=True, context={"request": request, "workspace": self.workspace}).data, status=201)
