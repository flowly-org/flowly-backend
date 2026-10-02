from collections import OrderedDict
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from tasks.access import member_visible_q
from tasks.models import Task
from workspaces.models import Workspace

from . import einvoice
from .models import Invoice, InvoiceLine, InvoicingProfile, ProjectBilling, TimeEntry, WorkspaceBilling, money, project_billing, rate_for, rounded_seconds


def ensure_tracking(project, workspace=None):
    workspace = workspace or (project.workspace if project is not None else None)
    if workspace is not None and not workspace.time_tracking:
        raise ValidationError({"detail": f"Time tracking is turned off in “{workspace.name}”."})
    if project is not None and not project.time_tracking:
        raise ValidationError({"detail": f"Time tracking is turned off for the project “{project.name}”."})


def default_billable(workspace, project) -> bool:
    """Whether new time is billable. Independent of billing being enabled, so enabling it later
    bills time already tracked (amounts are only computed while billing is on)."""
    billing = WorkspaceBilling.for_workspace(workspace)
    pb = project_billing(project)
    return billing.billable_by_default and not (pb and pb.mode == ProjectBilling.Mode.OFF)


def running_entry(user):
    return TimeEntry.objects.filter(user=user, ended_at__isnull=True).select_related("task", "project", "workspace").first()


def resolve_task(user, workspace, task_id):
    if task_id in (None, ""):
        return None
    task = workspace.tasks.filter(member_visible_q(user)).filter(pk=int(task_id)).first()
    if task is None:
        raise ValidationError({"task": "No such task in this workspace."})
    return task


@transaction.atomic
def stop_timer(user, at=None):
    entry = TimeEntry.objects.select_for_update().filter(user=user, ended_at__isnull=True).first()
    if entry is None:
        return None
    entry.ended_at = at or timezone.now()
    entry.duration_seconds = max(0, int((entry.ended_at - entry.started_at).total_seconds()))
    entry.save(update_fields=["ended_at", "duration_seconds", "updated_at"])
    return entry


@transaction.atomic
def start_timer(user, workspace: Workspace, task: Task | None = None, description="", project=None):
    project = task.project if task is not None else project
    ensure_tracking(project, workspace)
    stop_timer(user)
    return TimeEntry.objects.create(
        workspace=workspace, user=user, task=task, project=project, description=description[:500],
        started_at=timezone.now(), billable=default_billable(workspace, project),
    )


def entry_rate_and_hours(entry: TimeEntry, billing: WorkspaceBilling):
    rate = rate_for(billing, entry.project) if entry.billable else None
    seconds = rounded_seconds(entry.duration_seconds, billing.rounding_minutes)
    return rate, Decimal(seconds) / Decimal(3600)


def report(workspace, entries, group: str):
    """Totals per project / user / task / day."""
    billing = WorkspaceBilling.for_workspace(workspace)
    rows: "OrderedDict[str, dict]" = OrderedDict()
    totals = {"seconds": 0, "billable_seconds": 0, "amount": Decimal(0)}
    for e in entries.select_related("project", "user", "task"):
        seconds = e.elapsed_seconds()
        if group == "user":
            key, label = f"u{e.user_id}", e.user.full_name or e.user.email
        elif group == "task":
            key, label = (f"t{e.task_id}", e.task.title) if e.task_id else ("t0", "No task")
        elif group == "day":
            key = label = timezone.localtime(e.started_at).date().isoformat()
        else:
            key, label = (f"p{e.project_id}", e.project.name) if e.project_id else ("p0", "No project")
        row = rows.setdefault(key, {"key": key, "label": label, "seconds": 0, "billable_seconds": 0, "amount": Decimal(0)})
        row["seconds"] += seconds
        totals["seconds"] += seconds
        rate = rate_for(billing, e.project) if e.billable else None
        if rate is not None and not e.running:
            billed = rounded_seconds(seconds, billing.rounding_minutes)
            amount = Decimal(billed) / 3600 * rate
            row["billable_seconds"] += billed
            row["amount"] += amount
            totals["billable_seconds"] += billed
            totals["amount"] += amount
    out = list(rows.values())
    for row in out:
        row["amount"] = str(money(row["amount"]))
    totals["amount"] = str(money(totals["amount"]))
    return {"currency": billing.currency, "billing_enabled": billing.enabled, "rows": out, "totals": totals}


@transaction.atomic
def profile_details(profile) -> str:
    """Issuer block printed on invoices: the free text, or one built from the structured fields."""
    if profile.details.strip():
        return profile.details.strip()
    lines = [profile.address, " ".join(filter(None, [profile.zip, profile.city, profile.province])).strip()]
    if profile.vat_id:
        lines.append(f"VAT {profile.country}{profile.vat_id}")
    if profile.tax_code:
        lines.append(f"Tax code {profile.tax_code}")
    if profile.iban:
        lines.append(f"IBAN {profile.iban}")
    return "\n".join(l for l in lines if l)


def create_invoice(workspace, user, *, project, period_start: date, period_end: date, group_by="task"):
    billing = WorkspaceBilling.objects.select_for_update().get_or_create(workspace=workspace)[0]
    if not billing.enabled or not workspace.time_tracking:
        raise ValidationError({"detail": "Billing isn't enabled for this workspace."})
    entries = (
        TimeEntry.objects.select_for_update(of=("self",))
        .filter(workspace=workspace, invoice__isnull=True, billable=True, ended_at__isnull=False,
                started_at__date__gte=period_start, started_at__date__lte=period_end)
        .select_related("task", "project", "user")
        .order_by("started_at")
    )
    if project is not None:
        entries = entries.filter(project=project)
    lines: "OrderedDict[tuple, dict]" = OrderedDict()
    used = []
    for e in entries:
        rate, hours = entry_rate_and_hours(e, billing)
        if rate is None or hours <= 0:
            continue
        if group_by == "day":
            key = (timezone.localtime(e.started_at).date().isoformat(), rate)
            label = f"{key[0]}" + (f" · {e.project.name}" if project is None and e.project_id else "")
        elif group_by == "entry":
            key = (e.id, rate)
            label = f"{timezone.localtime(e.started_at):%Y-%m-%d} · {e.task.title if e.task_id else e.description or 'Work'}"
        else:
            key = (e.task_id or f"d:{e.description}", rate)
            label = e.task.title if e.task_id else (e.description or "General work")
            if project is None and e.project_id:
                label = f"{e.project.name}: {label}"
        line = lines.setdefault(key, {"description": label[:500], "hours": Decimal(0), "rate": rate})
        line["hours"] += hours
        used.append(e.pk)
    if not lines:
        raise ValidationError({"detail": "No unbilled billable time in that period."})

    pb = project_billing(project)
    profile = InvoicingProfile.for_user(user)
    number = f"{billing.invoice_prefix}{billing.next_invoice_number:04d}"
    billing.next_invoice_number += 1
    billing.save(update_fields=["next_invoice_number"])
    today = timezone.localdate()
    invoice = Invoice.objects.create(
        workspace=workspace, project=project, number=number, issue_date=today,
        due_date=today + timedelta(days=billing.payment_terms_days),
        period_start=period_start, period_end=period_end, currency=billing.currency, tax_rate=billing.tax_rate,
        issuer_name=profile.name or user.full_name or user.email, issuer_details=profile_details(profile),
        client_name=(pb.client_name if pb else "") or (project.name if project else ""),
        client_details=pb.client_details if pb else "", notes=billing.invoice_notes, created_by=user,
        einvoice=einvoice.snapshot(user, pb),
    )
    for i, line in enumerate(lines.values()):
        # The amount is computed from the hours as printed, so every line adds up on paper.
        hours = line["hours"].quantize(Decimal("0.01"))
        InvoiceLine.objects.create(invoice=invoice, description=line["description"], hours=hours,
                                   rate=line["rate"], amount=money(hours * line["rate"]), position=i)
    TimeEntry.objects.filter(pk__in=used).update(invoice=invoice)
    invoice.recompute()
    invoice.save()
    return invoice
