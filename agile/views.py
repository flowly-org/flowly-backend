from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from tasks.models import Project, Task
from tasks.serializers import TaskSerializer
from workspaces.models import Membership

from . import services
from .models import AgileSetup, RetroItem, Sprint, StandupEntry


class SetupSerializer(serializers.ModelSerializer):
    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.none(), allow_null=True, required=False)

    class Meta:
        model = AgileSetup
        fields = ["id", "project", "enabled", "sprint_weeks", "sprint_start", "carry_over", "standup", "standup_time",
                  "standup_days", "retro", "timezone"]
        read_only_fields = ["id"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ws = self.context.get("workspace")
        self.fields["project"].queryset = Project.objects.filter(workspace=ws) if ws else Project.objects.none()

    def validate_sprint_weeks(self, v):
        if not 1 <= v <= 4:
            raise ValidationError("Sprints last 1 to 4 weeks.")
        return v

    def validate_sprint_start(self, v):
        if not 0 <= v <= 6:
            raise ValidationError("0 (Monday) to 6 (Sunday).")
        return v

    def validate_standup_days(self, v):
        if not isinstance(v, list) or any(not isinstance(d, int) or d < 0 or d > 6 for d in v):
            raise ValidationError("A list of weekdays, 0 (Monday) to 6.")
        return sorted(set(v))

    def validate_timezone(self, v):
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(v)
        except Exception:
            raise ValidationError("Unknown time zone.") from None
        return v


def sprint_brief(s):
    return {"id": s.id, "number": s.number, "name": s.name, "goal": s.goal, "start": s.start, "end": s.end, "status": s.status,
            "review": s.review, **({"numbers": services.numbers(s)} if s.status != Sprint.Status.CLOSED else {})}


def setup_data(setup):
    sprint = services.current(setup)
    return {**SetupSerializer(setup).data, "label": setup.label, "sprint": sprint_brief(sprint)}


class Scoped(APIView):
    def membership(self, ws):
        m = Membership.objects.filter(workspace_id=ws, user=self.request.user).select_related("workspace").first()
        if m is None:
            raise PermissionDenied("Not a member of this workspace.")
        return m

    def manager(self, ws):
        m = self.membership(ws)
        if not m.can_manage:
            raise PermissionDenied("Only owners and admins can change the agile setup.")
        return m

    def get_setup(self, ws, pk):
        self.membership(ws)
        return get_object_or_404(AgileSetup.objects.select_related("workspace", "project"), pk=pk, workspace_id=ws)

    def sprint(self, ws, pk):
        self.membership(ws)
        return get_object_or_404(Sprint.objects.select_related("setup__workspace", "setup__project"), pk=pk, setup__workspace_id=ws)


class SetupsView(Scoped):
    """GET the workspace's agile setups (with their current sprint); POST one (owners/admins)."""

    def get(self, request, ws):
        self.membership(ws)
        return Response([setup_data(s) for s in AgileSetup.objects.filter(workspace_id=ws, enabled=True).select_related("workspace", "project")])

    def post(self, request, ws):
        m = self.manager(ws)
        s = SetupSerializer(data=request.data, context={"workspace": m.workspace})
        s.is_valid(raise_exception=True)
        project = s.validated_data.get("project")
        existing = AgileSetup.objects.filter(workspace=m.workspace, project=project).first()
        if existing:
            s = SetupSerializer(existing, data={**request.data, "enabled": True}, partial=True, context={"workspace": m.workspace})
            s.is_valid(raise_exception=True)
        setup = s.save(workspace=m.workspace, created_by=request.user) if not existing else s.save()
        return Response(setup_data(setup), status=status.HTTP_201_CREATED)


class SetupView(Scoped):
    def patch(self, request, ws, pk):
        m = self.manager(ws)
        setup = self.get_setup(ws, pk)
        s = SetupSerializer(setup, data=request.data, partial=True, context={"workspace": m.workspace})
        s.is_valid(raise_exception=True)
        return Response(setup_data(s.save()))

    def delete(self, request, ws, pk):
        """Switch the rituals off (sprints and history are kept)."""
        self.manager(ws)
        setup = self.get_setup(ws, pk)
        setup.enabled = False
        setup.save(update_fields=["enabled"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class SprintsView(Scoped):
    def get(self, request, ws, pk):
        setup = self.get_setup(ws, pk)
        services.current(setup)
        return Response([sprint_brief(s) for s in setup.sprints.all()[:30]])

    def post(self, request, ws, pk):
        """Plan the next sprint."""
        setup = self.get_setup(ws, pk)
        return Response(sprint_brief(services.next_sprint(services.current(setup))), status=201)


class SprintView(Scoped):
    """The sprint page: tasks, burndown, capacity, today's standups, retro."""

    def get(self, request, ws, pk):
        sprint = self.sprint(ws, pk)
        setup = sprint.setup
        today = services.local_now(setup).date()
        ctx = {"request": request, "workspace": setup.workspace}
        tasks = sprint.tasks.prefetch_related("tags", "blocking_links", "blocked_by_links__depends_on__status").select_related("status")
        backlog = (services.scope_tasks(setup).filter(status__is_done=False).filter(sprint__isnull=True) |
                   services.scope_tasks(setup).filter(status__is_done=False, sprint__status=Sprint.Status.CLOSED))
        entries = StandupEntry.objects.filter(sprint=sprint, date=today).select_related("user")
        mine = next((e for e in entries if e.user_id == request.user.id), None)
        return Response({
            **sprint_brief(sprint), "setup": SetupSerializer(setup).data, "label": setup.label,
            "tasks": TaskSerializer(tasks, many=True, context=ctx).data,
            "backlog": TaskSerializer(backlog.distinct().order_by("due_date", "position")[:200], many=True, context=ctx).data,
            "burndown": services.burndown(sprint), "capacity": services.capacity(sprint),
            "standup": {
                "date": today,
                "entries": [{"user": e.user_id, "name": e.user.full_name or e.user.email, "yesterday": e.yesterday, "today": e.today,
                             "blockers": e.blockers, "updated_at": e.updated_at} for e in entries],
                "mine": {"yesterday": mine.yesterday, "today": mine.today, "blockers": mine.blockers} if mine else services.draft(setup, request.user, today),
                "answered": mine is not None,
            },
            "retro": [{"id": r.id, "kind": r.kind, "text": r.text, "votes": len(r.votes), "voted": request.user.id in r.votes,
                       "mine": r.author_id == request.user.id, "task": r.task_id} for r in sprint.retro_items.all()],
        })

    def patch(self, request, ws, pk):
        sprint = self.sprint(ws, pk)
        if "goal" in request.data:
            sprint.goal = str(request.data["goal"])[:300]
            sprint.save(update_fields=["goal"])
        return Response(sprint_brief(sprint))


class SprintTasksView(Scoped):
    """POST {add: [ids], remove: [ids]}."""

    def post(self, request, ws, pk):
        sprint = self.sprint(ws, pk)
        if sprint.status == Sprint.Status.CLOSED:
            raise ValidationError({"detail": "This sprint is closed."})
        scope = services.scope_tasks(sprint.setup)
        add = [int(i) for i in request.data.get("add") or []]
        remove = [int(i) for i in request.data.get("remove") or []]
        scope.filter(pk__in=add).update(sprint=sprint)
        sprint.tasks.filter(pk__in=remove).update(sprint=None)
        return Response(services.numbers(sprint))


class SprintCloseView(Scoped):
    def post(self, request, ws, pk):
        self.manager(ws)
        sprint = self.sprint(ws, pk)
        services.close(sprint)
        return Response(sprint_brief(sprint))


class StandupView(Scoped):
    """POST {yesterday, today, blockers} → my standup for today in this setup."""

    def post(self, request, ws, pk):
        setup = self.get_setup(ws, pk)
        today = services.local_now(setup).date()
        e = services.save_standup(setup, request.user, today, str(request.data.get("yesterday") or ""),
                                  str(request.data.get("today") or ""), str(request.data.get("blockers") or ""))
        return Response({"date": e.date, "yesterday": e.yesterday, "today": e.today, "blockers": e.blockers}, status=201)


class RetroView(Scoped):
    """POST {kind, text} on a sprint; PATCH item {vote: true|false}; DELETE item (author or managers);
    POST item/action {assignee?} → a task in the next sprint."""

    def post(self, request, ws, pk):
        sprint = self.sprint(ws, pk)
        kind = request.data.get("kind")
        text = str(request.data.get("text") or "").strip()
        if kind not in RetroItem.Kind.values or not text:
            raise ValidationError({"detail": "Pick a column and write something."})
        item = RetroItem.objects.create(sprint=sprint, kind=kind, text=text[:500], author=request.user)
        return Response({"id": item.id}, status=201)


class RetroItemView(Scoped):
    def item(self, ws, pk):
        self.membership(ws)
        return get_object_or_404(RetroItem.objects.select_related("sprint__setup"), pk=pk, sprint__setup__workspace_id=ws)

    def patch(self, request, ws, pk):
        item = self.item(ws, pk)
        votes = [v for v in item.votes if v != request.user.id]
        if request.data.get("vote"):
            votes.append(request.user.id)
        item.votes = votes
        item.save(update_fields=["votes"])
        return Response({"votes": len(votes), "voted": request.user.id in votes})

    def delete(self, request, ws, pk):
        item = self.item(ws, pk)
        if item.author_id != request.user.id and not self.membership(ws).can_manage:
            raise PermissionDenied("Only its author (or an admin) can remove it.")
        item.delete()
        return Response(status=204)

    def post(self, request, ws, pk):
        """Turn it into an action item for the next sprint."""
        from tasks.bulk import create_many

        item = self.item(ws, pk)
        setup = item.sprint.setup
        target = services.next_sprint(item.sprint) if item.sprint.status != Sprint.Status.CLOSED else services.current(setup)
        assignee = request.data.get("assignee") or request.user.id
        task = create_many(request.user, setup.workspace, [{
            "title": item.text[:255], "description": f"Action item from the {item.sprint.name} retrospective.",
            "project": setup.project_id, "assignee": assignee, "planned_date": None}], request=request)[0]
        Task.objects.filter(pk=task.pk).update(sprint=target)
        item.task = task
        item.save(update_fields=["task"])
        return Response({"task": task.id, "sprint": target.id}, status=201)
