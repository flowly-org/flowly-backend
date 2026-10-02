from django.utils import timezone
from rest_framework import serializers

from accounts.serializers import UserSerializer
from tasks.models import Project

from .models import Invoice, InvoiceLine, InvoicingProfile, ProjectBilling, TimeEntry, WorkspaceBilling


class TimeEntrySerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    task_title = serializers.CharField(source="task.title", read_only=True, default=None)
    project_name = serializers.CharField(source="project.name", read_only=True, default=None)
    running = serializers.BooleanField(read_only=True)
    seconds = serializers.SerializerMethodField()
    invoiced = serializers.SerializerMethodField()

    class Meta:
        model = TimeEntry
        fields = ["id", "workspace", "user", "task", "task_title", "project", "project_name", "description",
                  "started_at", "ended_at", "duration_seconds", "seconds", "billable", "running", "invoice", "invoiced"]
        read_only_fields = ["id", "workspace", "user", "invoice", "duration_seconds"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ws = self.context.get("workspace")
        if ws is not None:
            self.fields["project"].queryset = Project.objects.filter(workspace=ws)
            self.fields["task"].queryset = ws.tasks.all()

    def get_seconds(self, obj):
        return obj.elapsed_seconds()

    def get_invoiced(self, obj):
        return obj.invoice.number if obj.invoice_id else None

    def validate(self, attrs):
        instance = self.instance
        if instance is not None and instance.invoice_id:
            raise serializers.ValidationError("This time is on an invoice and can't be changed.")
        started = attrs.get("started_at", instance.started_at if instance else None)
        ended = attrs.get("ended_at", instance.ended_at if instance else None)
        duration = self.initial_data.get("duration_seconds")
        if started is None:
            raise serializers.ValidationError({"started_at": "Required."})
        if ended is None and duration not in (None, ""):
            ended = started + timezone.timedelta(seconds=int(duration))
            attrs["ended_at"] = ended
        if ended is None and (instance is None or not instance.running):
            raise serializers.ValidationError({"ended_at": "Give an end time or a duration (use the timer for running time)."})
        if ended is not None:
            if ended <= started:
                raise serializers.ValidationError({"ended_at": "Must be after the start."})
            if (ended - started).total_seconds() > 24 * 3600:
                raise serializers.ValidationError({"ended_at": "An entry can be at most 24 hours."})
            attrs["duration_seconds"] = int((ended - started).total_seconds())
        task = attrs.get("task")
        if task is not None and "project" not in attrs:
            attrs["project"] = task.project
        project = attrs.get("project", instance.project if instance else None)
        ws = self.context.get("workspace")
        if ws is not None and not ws.time_tracking:
            raise serializers.ValidationError({"detail": f"Time tracking is turned off in “{ws.name}”."})
        if project is not None and not project.time_tracking:
            raise serializers.ValidationError({"detail": f"Time tracking is turned off for the project “{project.name}”."})
        return attrs


class WorkspaceBillingSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkspaceBilling
        exclude = ["id", "workspace"]

    def validate_currency(self, value):
        value = value.upper()
        if len(value) != 3 or not value.isalpha():
            raise serializers.ValidationError("Use a 3-letter currency code, e.g. EUR.")
        return value

    def validate_rounding_minutes(self, value):
        if value not in (0, 1, 5, 6, 10, 15, 30, 60):
            raise serializers.ValidationError("Choose 0, 1, 5, 6, 10, 15, 30 or 60.")
        return value


class InvoicingProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoicingProfile
        exclude = ["id", "user"]
        read_only_fields = ["updated_at"]

    def validate_country(self, value):
        value = (value or "").upper()
        if len(value) != 2 or not value.isalpha():
            raise serializers.ValidationError("Use a 2-letter country code, e.g. IT.")
        return value

    def validate_iban(self, value):
        return "".join(value.split()).upper()

    def validate_format_settings(self, value):
        from . import einvoice

        if not isinstance(value, dict):
            raise serializers.ValidationError("Must be an object.")
        clean = {}
        for key, settings in value.items():
            fmt = einvoice.FORMATS.get(key)
            if fmt is None or not isinstance(settings, dict):
                continue
            spec = {f["key"]: f for f in fmt.settings}
            out = {}
            for k, v in settings.items():
                f = spec.get(k)
                if f is None:
                    continue
                if f["type"] == "bool":
                    v = bool(v)
                elif f["type"] == "choice" and v not in [c[0] for c in f["choices"]]:
                    raise serializers.ValidationError(f"{fmt.label}: invalid {f['label']}.")
                out[k] = v
            clean[key] = out
        # Keep settings of other formats that weren't sent.
        existing = self.instance.format_settings if self.instance else {}
        return {**existing, **clean}


class ProjectBillingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectBilling
        exclude = ["id", "project"]


class InvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLine
        fields = ["id", "description", "hours", "rate", "amount"]


class InvoiceSerializer(serializers.ModelSerializer):
    lines = InvoiceLineSerializer(many=True, read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True, default=None)
    entry_count = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        exclude = ["workspace"]
        read_only_fields = ["number", "project", "period_start", "period_end", "currency", "subtotal", "tax_amount",
                            "total", "created_by", "created_at"]

    def get_entry_count(self, obj):
        return obj.entries.count()

    def validate(self, attrs):
        if self.instance and self.instance.status != Invoice.Status.DRAFT:
            allowed = {"status", "notes"}
            if set(attrs) - allowed:
                raise serializers.ValidationError("Only the status and notes of a sent invoice can change.")
        return attrs
