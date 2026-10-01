from rest_framework import serializers

from tasks.models import Project

from .connectors import REGISTRY
from .models import Delivery, Integration


class DeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = Delivery
        fields = [
            "id", "direction", "event", "status", "attempts", "task", "payload", "last_error",
            "response_status", "response_body", "next_attempt_at", "created_at", "finished_at",
        ]
        read_only_fields = fields


class IntegrationSerializer(serializers.ModelSerializer):
    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all(), allow_null=True, required=False)
    info = serializers.SerializerMethodField()
    last_delivery = serializers.SerializerMethodField()

    class Meta:
        model = Integration
        fields = [
            "id", "kind", "name", "project", "enabled", "config", "token", "secret",
            "info", "last_delivery", "created_at",
        ]
        read_only_fields = ["id", "token", "secret", "info", "last_delivery", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        workspace = self.context.get("workspace")
        self.fields["project"].queryset = Project.objects.filter(workspace=workspace) if workspace else Project.objects.none()
        if self.instance is not None and not isinstance(self.instance, (list, tuple)) and hasattr(self.instance, "pk"):
            self.fields["kind"].read_only = True

    def get_info(self, obj):
        return REGISTRY[obj.kind].info(obj)

    def get_last_delivery(self, obj):
        d = obj.deliveries.order_by("-created_at", "-id").first()
        return {"status": d.status, "event": d.event, "created_at": d.created_at} if d else None

    def validate(self, attrs):
        kind = attrs.get("kind") or (self.instance.kind if self.instance else None)
        if kind not in REGISTRY:
            raise serializers.ValidationError({"kind": "Unknown integration kind."})
        config = attrs.get("config", self.instance.config if self.instance else {}) or {}
        if not isinstance(config, dict):
            raise serializers.ValidationError({"config": "Must be an object."})
        attrs["config"] = REGISTRY[kind].clean_config(config, self.instance)
        return attrs
