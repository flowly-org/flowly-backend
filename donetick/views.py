from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.connectors.donetick import clean_base_url
from integrations.crypto import encrypt, mask
from integrations.http import DeliveryError
from tasks.models import Project
from workspaces.models import Workspace

from . import engine
from .models import DonetickAccount, DonetickRoute


class HookView(APIView):
    """POST /api/hooks/donetick/<token>/ — Donetick's circle webhook (the secret is the URL)."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request, token):
        src = engine.by_hook_token(token)
        if src is None or not src.enabled:
            return Response({"detail": "Unknown or disabled."}, status=status.HTTP_404_NOT_FOUND)
        payload = request.data if isinstance(request.data, dict) else {}
        return Response({"handled": engine.handle_hook(src, payload)})


class RouteSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=100)
    workspace = serializers.IntegerField()
    project = serializers.IntegerField(required=False, allow_null=True)


class AccountSerializer(serializers.ModelSerializer):
    token = serializers.CharField(write_only=True, required=False, allow_blank=True)
    routes = RouteSerializer(many=True, required=False, write_only=True)
    info = serializers.SerializerMethodField()

    class Meta:
        model = DonetickAccount
        fields = ["id", "name", "base_url", "token", "enabled", "donetick_user_id", "only_mine", "default_workspace",
                  "default_project", "private", "write_back", "routes", "info", "created_at"]
        read_only_fields = ["id", "info", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = self.context["request"].user
        self.fields["default_workspace"].queryset = Workspace.objects.filter(memberships__user=user)
        self.fields["default_project"].queryset = Project.objects.filter(workspace__memberships__user=user)

    def get_info(self, obj):
        state = getattr(obj, "sync", None)
        key = engine.decrypt(obj.token) if obj.token else ""
        return {
            "webhook_url": engine.webhook_url(obj.hook_token), "token": mask(key) if key else None,
            "synced_at": state.synced_at if state else None, "error": state.error if state else "",
            "chores": state.chores if state else 0, "members": state.members if state else [],
            "labels": state.labels if state else [],
        }

    def to_representation(self, obj):
        data = super().to_representation(obj)
        data["routes"] = [{"label": r.label, "workspace": r.workspace_id, "project": r.project_id} for r in obj.routes.all()]
        return data

    def validate_base_url(self, value):
        try:
            return clean_base_url(value)
        except serializers.ValidationError as exc:
            raise serializers.ValidationError(exc.detail["base_url"]) from None

    def validate(self, attrs):
        user = self.context["request"].user
        member = set(user.memberships.values_list("workspace_id", flat=True))
        routes = attrs.get("routes")
        if routes is not None:
            seen = set()
            for r in routes:
                key = r["label"].strip().lower()
                if key in seen:
                    raise serializers.ValidationError({"routes": f"“{r['label']}” is mapped twice."})
                seen.add(key)
                if r["workspace"] not in member:
                    raise serializers.ValidationError({"routes": "Pick workspaces you belong to."})
                if r.get("project") and not Project.objects.filter(pk=r["project"], workspace_id=r["workspace"]).exists():
                    raise serializers.ValidationError({"routes": "The project must be in the chosen workspace."})
        ws = attrs.get("default_workspace", getattr(self.instance, "default_workspace", None))
        project = attrs.get("default_project", getattr(self.instance, "default_project", None))
        if project and (ws is None or project.workspace_id != ws.id):
            raise serializers.ValidationError({"default_project": "The project must be in the default workspace."})
        token = attrs.pop("token", None)
        if token:
            attrs["token"] = encrypt(token.strip())
        elif self.instance is None:
            raise serializers.ValidationError({"token": "Paste a Donetick access token (Donetick → Settings → Access tokens)."})
        return attrs

    @transaction.atomic
    def save_routes(self, account, routes):
        if routes is None:
            return
        account.routes.all().delete()
        DonetickRoute.objects.bulk_create([
            DonetickRoute(account=account, label=r["label"].strip(), workspace_id=r["workspace"], project_id=r.get("project"), position=i)
            for i, r in enumerate(routes)
        ])

    def create(self, validated):
        routes = validated.pop("routes", None)
        account = DonetickAccount.objects.create(user=self.context["request"].user, **validated)
        self.save_routes(account, routes)
        return account

    def update(self, instance, validated):
        routes = validated.pop("routes", None)
        account = super().update(instance, validated)
        self.save_routes(account, routes)
        return account


class AccountsView(APIView):
    """GET/POST /api/my/donetick/ — personal Donetick connections."""

    def get(self, request):
        qs = DonetickAccount.objects.filter(user=request.user).prefetch_related("routes")
        return Response(AccountSerializer(qs, many=True, context={"request": request}).data)

    def post(self, request):
        s = AccountSerializer(data=request.data, context={"request": request})
        s.is_valid(raise_exception=True)
        account = s.save()
        try:
            engine.poll(engine.Source(account=account))
        except DeliveryError:
            pass  # shown as the connection's error
        return Response(AccountSerializer(account, context={"request": request}).data, status=status.HTTP_201_CREATED)


class AccountView(APIView):
    """PATCH/DELETE /api/my/donetick/<id>/; POST …/sync/ to sync now."""

    def get_object(self, request, pk):
        return get_object_or_404(DonetickAccount, pk=pk, user=request.user)

    def patch(self, request, pk):
        s = AccountSerializer(self.get_object(request, pk), data=request.data, partial=True, context={"request": request})
        s.is_valid(raise_exception=True)
        return Response(AccountSerializer(s.save(), context={"request": request}).data)

    def delete(self, request, pk):
        self.get_object(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def post(self, request, pk):
        account = self.get_object(request, pk)
        try:
            state = engine.poll(engine.Source(account=account))
            message = f"Synced {state.chores} chore(s)."
        except DeliveryError as exc:
            return Response({"detail": f"Donetick: {exc}"}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": message, "account": AccountSerializer(account, context={"request": request}).data})
