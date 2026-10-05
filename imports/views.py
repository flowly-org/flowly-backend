import json

from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.http import DeliveryError
from workspaces.models import Membership

from . import asana, engine, trello
from .models import ImportRun

MAX_BYTES = 25 * 1024 * 1024


class ImportView(APIView):
    """POST /api/workspaces/<ws>/import/ — multipart {source: trello|asana_csv, file} or JSON {source: asana_api, token,
    project}; optional {project_id} to import into an existing project (default: a project named after the board).

    GET lists the past imports of the workspace."""

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def membership(self, request, ws):
        return Membership.objects.filter(workspace_id=ws, user=request.user).select_related("workspace").first()

    def get(self, request, ws):
        if self.membership(request, ws) is None:
            return Response(status=404)
        return Response([{"id": i.id, "source": i.source, "name": i.name, "project": i.project_id, "count": len(i.items),
                          "created_at": i.created_at} for i in ImportRun.objects.filter(workspace_id=ws)[:50]])

    def post(self, request, ws):
        m = self.membership(request, ws)
        if m is None:
            return Response(status=404)
        source = request.data.get("source")
        try:
            if source in ("trello", "asana_csv"):
                upload = request.FILES.get("file")
                if upload is None:
                    return Response({"detail": "Choose the export file."}, status=400)
                if upload.size > MAX_BYTES:
                    return Response({"detail": "The file is too large (max 25 MB)."}, status=400)
                raw = upload.read().decode("utf-8", errors="replace")
                if source == "trello":
                    try:
                        board_id, name, items = trello.read(json.loads(raw))
                    except json.JSONDecodeError:
                        return Response({"detail": "That isn't a Trello board export (JSON)."}, status=400)
                else:
                    board_id, name, items = asana.read_csv(raw, upload.name.rsplit(".", 1)[0])
                origin = "trello" if source == "trello" else "asana"
            elif source == "asana_api":
                board_id, name, items = asana.read_api(str(request.data.get("token") or "").strip(), str(request.data.get("project") or ""))
                origin = "asana"
            else:
                return Response({"detail": "Unknown source."}, status=400)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        except DeliveryError as exc:
            return Response({"detail": f"Couldn't read from the source: {exc}"}, status=502)
        if not items:
            return Response({"detail": "Nothing to import."}, status=400)
        project_id = request.data.get("project_id") or None
        try:
            result = engine.run(request.user, m.workspace, source=origin, source_id=board_id, name=name, items=items,
                                project_id=int(project_id) if project_id else None)
        except Exception as exc:  # noqa: BLE001 — report instead of a 500 on odd exports
            if exc.__class__.__name__ == "DoesNotExist":
                return Response({"detail": "That project isn't in this workspace."}, status=400)
            raise
        return Response(result, status=status.HTTP_201_CREATED)
