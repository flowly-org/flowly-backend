from rest_framework import serializers

from tasks.models import Project

from .connectors import REGISTRY
from .models import Delivery, EmailAlias, Integration


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
    api_key = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="AI providers: stored encrypted; blank keeps the current key.")
    last_delivery = serializers.SerializerMethodField()
    aliases = serializers.ListField(
        child=serializers.CharField(max_length=60), required=False, write_only=True,
        help_text="Email in: readable addresses, e.g. [\"mensa\"] for mensa@<inbound domain>.",
    )

    class Meta:
        model = Integration
        fields = [
            "id", "kind", "name", "project", "enabled", "config", "api_key", "token", "secret",
            "aliases", "info", "last_delivery", "created_at",
        ]
        read_only_fields = ["id", "token", "secret", "info", "last_delivery", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        workspace = self.context.get("workspace")
        self.fields["project"].queryset = Project.objects.filter(workspace=workspace) if workspace else Project.objects.none()
        if self.instance is not None and not isinstance(self.instance, (list, tuple)) and hasattr(self.instance, "pk"):
            self.fields["kind"].read_only = True

    def to_representation(self, obj):
        data = super().to_representation(obj)
        data["aliases"] = [a.name for a in obj.aliases.all()] if obj.kind == "email" else []
        return data

    def get_info(self, obj):
        return REGISTRY[obj.kind].info(obj)

    def get_last_delivery(self, obj):
        d = obj.deliveries.order_by("-created_at", "-id").first()
        return {"status": d.status, "event": d.event, "created_at": d.created_at} if d else None

    def validate(self, attrs):
        kind = attrs.get("kind") or (self.instance.kind if self.instance else None)
        if kind not in REGISTRY:
            raise serializers.ValidationError({"kind": "Unknown integration kind."})
        workspace = self.context.get("workspace") or (self.instance.workspace if self.instance else None)
        if self.instance is None and REGISTRY[kind].enterprise and not (workspace and workspace.is_enterprise):
            raise serializers.ValidationError({"kind": "This integration needs the Enterprise plan for the workspace."})
        config = attrs.get("config", self.instance.config if self.instance else {}) or {}
        if not isinstance(config, dict):
            raise serializers.ValidationError({"config": "Must be an object."})
        attrs["config"] = REGISTRY[kind].clean_config(config, self.instance)
        if "aliases" in attrs:
            attrs["aliases"] = self._clean_aliases(kind, attrs["aliases"])
        api_key = attrs.pop("api_key", None)
        if api_key:
            from .crypto import encrypt

            attrs["credentials"] = {**(self.instance.credentials if self.instance else {}), "api_key": encrypt(api_key.strip())}
        return attrs

    def _clean_aliases(self, kind, values):
        from .connectors.email import MAX_ALIASES, clean_alias

        if kind != "email":
            raise serializers.ValidationError({"aliases": "Only Email-in integrations have aliases."})
        try:
            names = list(dict.fromkeys(clean_alias(v) for v in values if v and v.strip()))
        except ValueError as exc:
            raise serializers.ValidationError({"aliases": str(exc)}) from exc
        if len(names) > MAX_ALIASES:
            raise serializers.ValidationError({"aliases": f"At most {MAX_ALIASES} aliases."})
        taken = EmailAlias.objects.filter(name__in=names)
        if self.instance is not None:
            taken = taken.exclude(integration=self.instance)
        if taken.exists():
            raise serializers.ValidationError({"aliases": f"“{taken.first().name}” is already taken."})
        return names

    def _sync_aliases(self, integration, names):
        if names is None:
            return
        integration.aliases.exclude(name__in=names).delete()
        have = set(integration.aliases.values_list("name", flat=True))
        EmailAlias.objects.bulk_create([EmailAlias(integration=integration, name=n) for n in names if n not in have])

    def create(self, validated_data):
        names = validated_data.pop("aliases", None)
        integration = super().create(validated_data)
        self._sync_aliases(integration, names)
        return integration

    def update(self, instance, validated_data):
        names = validated_data.pop("aliases", None)
        integration = super().update(instance, validated_data)
        self._sync_aliases(integration, names)
        return integration
