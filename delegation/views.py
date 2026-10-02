from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import engine
from .models import Delegation


def data(d: Delegation | None):
    if d is None:
        return None
    return {"id": d.id, "run": d.run, "status": d.status, "verdict": d.verdict, "plan": d.plan, "reason": d.reason,
            "summary": d.summary, "left_for_you": d.left_for_you, "steps": d.steps, "created_at": d.created_at,
            "finished_at": d.finished_at, "error": bool(d.error)}


class TaskDelegationView(APIView):
    """GET: the latest delegation of this task (yours) and your monthly allowance.
    POST {"action": "assess" | "run"}: check whether the AI can do it, or have it done."""

    def _task(self, request, ws, pk):
        from tasks.access import member_visible_q
        from tasks.models import Task

        return (Task.objects.filter(workspace_id=ws, pk=pk, workspace__memberships__user=request.user)
                .filter(member_visible_q(request.user)).select_related("workspace").first())

    def get(self, request, ws, pk):
        task = self._task(request, ws, pk)
        if task is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        latest = Delegation.objects.filter(task=task, user=request.user).first()
        return Response({"delegation": data(latest), "quota": engine.quota(request.user), "available": request.user.has_pro_plus})

    def post(self, request, ws, pk):
        task = self._task(request, ws, pk)
        if task is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            d = engine.request(task, request.user, run=request.data.get("action", "run") == "run")
        except PermissionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        return Response({"delegation": data(d), "quota": engine.quota(request.user), "available": True}, status=201)
