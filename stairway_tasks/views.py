import json

from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from tasks.access import member_visible_q
from tasks.models import Task
from workspaces.permissions import WorkspaceScopedMixin

from . import services
from .models import StairwayRun


class TaskRunsView(WorkspaceScopedMixin, APIView):
    """GET: whether Stairway is available for the task, and its runs · POST {"instructions"?}: hand the task over."""

    def task(self, pk):
        return get_object_or_404(Task.objects.filter(workspace=self.workspace).filter(member_visible_q(self.request.user)), pk=pk)

    def get(self, request, workspace_pk, pk):
        task = self.task(pk)
        return Response({"available": services.integration_for(task) is not None,
                         "runs": [services.data(r) for r in services.runs_for(task)]})

    def post(self, request, workspace_pk, pk):
        task = self.task(pk)
        try:
            run = services.start(task, request.user, (request.data.get("instructions") or "").strip()[:4000])
        except services.RunError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response(services.data(run), status=status.HTTP_201_CREATED)


class RunActionView(TaskRunsView):
    """POST .../stairway/<run>/prompt/ {"text"} · .../stop/ · .../approve/ · .../deny/"""

    def post(self, request, workspace_pk, pk, run_pk, action):
        task = self.task(pk)
        run = get_object_or_404(StairwayRun.objects.select_related("integration"), pk=run_pk, task=task)
        try:
            if action == "prompt":
                run = services.follow_up(run, request.user, request.data.get("text"))
            elif action == "stop":
                run = services.stop(run)
            elif action in ("approve", "deny"):
                run = services.answer(run, request.user, action == "approve")
            else:
                return Response(status=status.HTTP_404_NOT_FOUND)
        except services.RunError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response(services.data(run))


class CallbackView(APIView):
    """Stairway's signed callback with each finished reply of a run's experiment."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request, token):
        run = StairwayRun.objects.filter(token=token).select_related("task").first()
        body = request.body
        if run is None or not services.verify(run, body, request.headers.get("X-Stairway-Signature", "")):
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            payload = json.loads(body)
        except ValueError:
            return Response(status=status.HTTP_400_BAD_REQUEST)
        if payload.get("experiment") == run.experiment:
            if payload.get("event") == "reply":
                services.receive(run, payload)
            elif payload.get("event") == "waiting":
                services.waiting(run, payload)
        return Response(status=status.HTTP_204_NO_CONTENT)
