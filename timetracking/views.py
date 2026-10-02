import csv
from datetime import date

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from workspaces.models import Workspace
from workspaces.permissions import WorkspaceScopedMixin

from . import services
from .models import Invoice, InvoicingProfile, ProjectBilling, TimeEntry, WorkspaceBilling, rate_for
from .serializers import InvoiceSerializer, InvoicingProfileSerializer, ProjectBillingSerializer, TimeEntrySerializer, WorkspaceBillingSerializer


def parse_date(value, field):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError as exc:
        raise ValidationError({field: "Use YYYY-MM-DD."}) from exc


class TimeEntryViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    """Time in a workspace. Members see and edit their own entries; owners/admins see everyone's."""

    serializer_class = TimeEntrySerializer

    def get_queryset(self):
        qs = self.workspace.time_entries.select_related("user", "task", "project", "invoice")
        p = self.request.query_params
        if not self.membership.can_manage or p.get("user") == "me":
            qs = qs.filter(user=self.request.user)
        elif p.get("user", "").isdigit():
            qs = qs.filter(user_id=int(p["user"]))
        if p.get("from"):
            qs = qs.filter(started_at__date__gte=parse_date(p["from"], "from"))
        if p.get("to"):
            qs = qs.filter(started_at__date__lte=parse_date(p["to"], "to"))
        if p.get("project", "").isdigit():
            qs = qs.filter(project_id=int(p["project"]))
        if p.get("task", "").isdigit():
            qs = qs.filter(task_id=int(p["task"]))
        if p.get("unbilled"):
            qs = qs.filter(invoice__isnull=True, billable=True)
        return qs

    def perform_create(self, serializer):
        project = serializer.validated_data.get("project")
        billable = serializer.validated_data.get("billable", services.default_billable(self.workspace, project))
        serializer.save(workspace=self.workspace, user=self.request.user, billable=billable)

    def _own(self, entry):
        if entry.user_id != self.request.user.id and not self.membership.can_manage:
            raise PermissionDenied("You can only change your own time.")
        if entry.invoice_id:
            raise ValidationError({"detail": "This time is on an invoice and can't be changed."})

    def perform_update(self, serializer):
        self._own(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._own(instance)
        instance.delete()

    @action(detail=False, methods=["get"])
    def report(self, request, workspace_pk=None):
        group = request.query_params.get("group", "project")
        if group not in ("project", "user", "task", "day"):
            raise ValidationError({"group": "project, user, task or day."})
        return Response(services.report(self.workspace, self.get_queryset(), group))


class TimerView(APIView):
    """GET /api/my/timer/ · POST {"action": "start", "workspace", "task"?, "description"?} or {"action": "stop"}."""

    def get(self, request):
        entry = services.running_entry(request.user)
        return Response({"entry": TimeEntrySerializer(entry).data if entry else None})

    def post(self, request):
        act = request.data.get("action")
        if act == "stop":
            entry = services.stop_timer(request.user)
            return Response({"entry": None, "stopped": TimeEntrySerializer(entry).data if entry else None})
        if act != "start":
            raise ValidationError({"action": 'Use "start" or "stop".'})
        ws = get_object_or_404(Workspace.objects.filter(memberships__user=request.user), pk=request.data.get("workspace"))
        task = services.resolve_task(request.user, ws, request.data.get("task"))
        project = None
        if task is None and str(request.data.get("project", "")).isdigit():
            project = get_object_or_404(ws.projects, pk=int(request.data["project"]))
        entry = services.start_timer(request.user, ws, task, request.data.get("description") or "", project=project)
        return Response({"entry": TimeEntrySerializer(entry).data}, status=status.HTTP_201_CREATED)


class BillingView(WorkspaceScopedMixin, APIView):
    """Workspace billing settings (readable by members, editable by owners/admins) and per-project config."""

    def get(self, request, workspace_pk):
        billing = WorkspaceBilling.for_workspace(self.workspace)
        projects = []
        for project in self.workspace.projects.select_related("billing").order_by("position", "name"):
            try:
                pb = project.billing
            except ProjectBilling.DoesNotExist:
                pb = None
            unbilled = TimeEntry.objects.filter(project=project, invoice__isnull=True, billable=True, ended_at__isnull=False)
            seconds = sum(e.duration_seconds for e in unbilled)
            total_seconds = sum(e.duration_seconds for e in project.time_entries.filter(ended_at__isnull=False))
            rate = rate_for(billing, project)
            projects.append({
                "project": project.id, "name": project.name, "color": project.color, "archived": project.archived,
                **ProjectBillingSerializer(pb or ProjectBilling(project=project)).data,
                "effective_rate": str(rate) if rate is not None else None,
                "unbilled_hours": round(seconds / 3600, 2),
                "tracked_hours": round(total_seconds / 3600, 2),
            })
        return Response({"settings": WorkspaceBillingSerializer(billing).data, "projects": projects,
                         "can_manage": self.membership.can_manage})

    def patch(self, request, workspace_pk):
        self.require_manager()
        billing = WorkspaceBilling.for_workspace(self.workspace)
        serializer = WorkspaceBillingSerializer(billing, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return self.get(request, workspace_pk)


class ProjectBillingView(WorkspaceScopedMixin, APIView):
    def patch(self, request, workspace_pk, project_pk):
        self.require_manager()
        project = get_object_or_404(self.workspace.projects, pk=project_pk)
        pb, _ = ProjectBilling.objects.get_or_create(project=project)
        old_name, old_details = pb.client_name, pb.client_details
        serializer = ProjectBillingSerializer(pb, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        extra = {}
        if serializer.validated_data.get("mode") == ProjectBilling.Mode.HOURLY and serializer.validated_data.get(
            "hourly_rate", pb.hourly_rate
        ) is None:
            # Start from the workspace rate; the admin adjusts it afterwards.
            extra["hourly_rate"] = WorkspaceBilling.for_workspace(self.workspace).default_rate
        serializer.save(**extra)
        # Drafts that still show the old client follow the change (sent invoices never do).
        drafts = Invoice.objects.filter(project=project, status=Invoice.Status.DRAFT)
        if pb.client_name != old_name:
            drafts.filter(client_name__in=[old_name, project.name]).update(client_name=pb.client_name or project.name)
        if pb.client_details != old_details:
            drafts.filter(client_details=old_details).update(client_details=pb.client_details)
        if pb.mode == ProjectBilling.Mode.OFF:
            # Opting a project out makes its unbilled time non-billable.
            TimeEntry.objects.filter(project=project, invoice__isnull=True).update(billable=False)
        return Response(ProjectBillingSerializer(pb).data)


class InvoiceViewSet(WorkspaceScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                     mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    serializer_class = InvoiceSerializer

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.require_manager()

    def get_queryset(self):
        return self.workspace.invoices.prefetch_related("lines").select_related("project")

    def create(self, request, workspace_pk=None):
        if not request.user.has_pro:
            return Response({"detail": "Creating invoices is part of Flowly Pro. Time tracking and reports stay free."},
                            status=status.HTTP_402_PAYMENT_REQUIRED)
        start = parse_date(request.data.get("period_start"), "period_start")
        end = parse_date(request.data.get("period_end"), "period_end")
        if not start or not end or end < start:
            raise ValidationError({"period_end": "Pick a valid period."})
        project = None
        if request.data.get("project"):
            project = get_object_or_404(self.workspace.projects, pk=request.data["project"])
        group_by = request.data.get("group_by", "task")
        if group_by not in ("task", "day", "entry"):
            raise ValidationError({"group_by": "task, day or entry."})
        invoice = services.create_invoice(self.workspace, request.user, project=project, period_start=start,
                                          period_end=end, group_by=group_by)
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @transaction.atomic
    def perform_destroy(self, instance):
        if instance.status != Invoice.Status.DRAFT:
            raise ValidationError({"detail": "Only drafts can be deleted; void a sent invoice instead."})
        instance.entries.update(invoice=None)
        instance.delete()

    def perform_update(self, serializer):
        from . import einvoice

        was_draft = serializer.instance.status == Invoice.Status.DRAFT
        invoice = serializer.save()
        if was_draft and invoice.status != Invoice.Status.DRAFT:
            invoice.einvoice = einvoice.current(invoice)  # freeze the e-invoice data as sent
            invoice.save(update_fields=["einvoice"])
        if invoice.status == Invoice.Status.VOID:
            invoice.entries.update(invoice=None)  # voided time can be invoiced again

    @action(detail=True, methods=["get"])
    def csv(self, request, workspace_pk=None, pk=None):
        invoice = self.get_object()
        res = HttpResponse(content_type="text/csv")
        res["Content-Disposition"] = f'attachment; filename="{invoice.number}.csv"'
        writer = csv.writer(res)
        writer.writerow(["Invoice", invoice.number, "Issued", invoice.issue_date, "Due", invoice.due_date])
        writer.writerow(["Client", invoice.client_name])
        writer.writerow([])
        writer.writerow(["Description", "Hours", f"Rate ({invoice.currency})", f"Amount ({invoice.currency})"])
        for line in invoice.lines.all():
            writer.writerow([line.description, line.hours, line.rate, line.amount])
        writer.writerow([])
        writer.writerow(["Subtotal", "", "", invoice.subtotal])
        writer.writerow([f"Tax {invoice.tax_rate}%", "", "", invoice.tax_amount])
        writer.writerow(["Total", "", "", invoice.total])
        return res

    @action(detail=True, methods=["get"])
    def einvoice(self, request, workspace_pk=None, pk=None):
        """National e-invoice file for the issuer's country (FatturaPA for Italy). ?check=1 lists what's missing."""
        from . import einvoice

        invoice = self.get_object()
        if not request.user.has_pro:
            return Response({"detail": "E-invoices are part of Flowly Pro."}, status=status.HTTP_402_PAYMENT_REQUIRED)
        fmt, data, missing = einvoice.export(invoice)
        if request.query_params.get("check"):
            return Response({
                "ok": not missing, "missing": missing, "format": fmt.key if fmt else None,
                "label": fmt.label if fmt else None, "filename": fmt.filename(invoice, data) if fmt else None,
            })
        if missing:
            return Response({"detail": "Missing data for the e-invoice.", "missing": missing}, status=status.HTTP_400_BAD_REQUEST)
        res = HttpResponse(fmt.build(invoice, data), content_type=fmt.content_type)
        res["Content-Disposition"] = f'attachment; filename="{fmt.filename(invoice, data)}"'
        return res

    @action(detail=True, methods=["get"])
    def entries(self, request, workspace_pk=None, pk=None):
        invoice = self.get_object()
        return Response(TimeEntrySerializer(invoice.entries.select_related("user", "task", "project"), many=True).data)




class InvoicingProfileView(APIView):
    """GET/PATCH /api/my/invoicing/: the user's invoicing identity, used on every invoice they issue."""

    def get(self, request):
        from . import einvoice

        profile = InvoicingProfile.for_user(request.user)
        fmt = einvoice.format_for(profile.country)
        return Response({
            "profile": InvoicingProfileSerializer(profile).data,
            "format": fmt.key if fmt else None,
            "formats": [f.describe() for f in einvoice.FORMATS.values()],
        })

    def patch(self, request):
        profile = InvoicingProfile.for_user(request.user)
        serializer = InvoicingProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return self.get(request)
