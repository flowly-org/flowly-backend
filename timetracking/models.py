from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models

from tasks.models import Project, Task
from workspaces.models import Workspace

CENT = Decimal("0.01")


class WorkspaceBilling(models.Model):
    """Billing switch and defaults for a workspace. Off unless an admin enables it."""

    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE, related_name="billing")
    enabled = models.BooleanField(default=False)
    currency = models.CharField(max_length=3, default="EUR")
    default_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Per hour.")
    #: Each entry is rounded up to this many minutes when invoiced (0 = exact).
    rounding_minutes = models.PositiveSmallIntegerField(default=0)
    billable_by_default = models.BooleanField(default=True)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="Percent, e.g. 22.00.")
    invoice_prefix = models.CharField(max_length=20, default="INV-")
    next_invoice_number = models.PositiveIntegerField(default=1)
    payment_terms_days = models.PositiveSmallIntegerField(default=30)
    invoice_notes = models.TextField(blank=True)

    @classmethod
    def for_workspace(cls, workspace):
        return cls.objects.get_or_create(workspace=workspace)[0]


class InvoicingProfile(models.Model):
    """Who issues the invoices: a person's (or their company's) invoicing identity, used in every workspace.

    `country` decides the national e-invoice format (see timetracking.einvoice); format-specific settings live in
    `format_settings`, e.g. {"fatturapa": {"regime": "RF19", "vat_nature": "N2.2", "stamp_duty": true}}.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="invoicing")
    name = models.CharField(max_length=200, blank=True, help_text="Business or full name on invoices.")
    details = models.TextField(blank=True, help_text="Free text printed under the name (contacts, registration…).")
    country = models.CharField(max_length=2, default="IT")
    vat_id = models.CharField(max_length=28, blank=True, help_text="VAT number without the country prefix.")
    tax_code = models.CharField(max_length=20, blank=True, help_text="National tax id (e.g. codice fiscale).")
    address = models.CharField(max_length=120, blank=True)
    zip = models.CharField(max_length=12, blank=True)
    city = models.CharField(max_length=80, blank=True)
    province = models.CharField(max_length=40, blank=True, help_text="Province / region / state.")
    iban = models.CharField(max_length=34, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    format_settings = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_user(cls, user):
        return cls.objects.get_or_create(user=user)[0]


class ProjectBilling(models.Model):
    class Mode(models.TextChoices):
        INHERIT = "inherit", "Workspace default rate"
        HOURLY = "hourly", "Own hourly rate"
        OFF = "off", "Not billable"

    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name="billing")
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.INHERIT)
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    client_name = models.CharField(max_length=200, blank=True)
    client_details = models.TextField(blank=True)
    # Italian e-invoicing (FatturaPA): the client ("cessionario/committente").
    client_vat = models.CharField("Partita IVA", max_length=28, blank=True)
    client_vat_country = models.CharField(max_length=2, default="IT")
    client_tax_code = models.CharField("Codice fiscale", max_length=16, blank=True)
    client_address = models.CharField(max_length=60, blank=True)
    client_zip = models.CharField(max_length=5, blank=True)
    client_city = models.CharField(max_length=60, blank=True)
    client_province = models.CharField(max_length=2, blank=True)
    client_country = models.CharField(max_length=2, default="IT")
    #: Codice destinatario (7 characters; 0000000 when the invoice goes to a PEC address or to a private person).
    client_sdi_code = models.CharField(max_length=7, blank=True)
    client_pec = models.EmailField(blank=True)
    #: Where the client receives e-invoices (EN 16931 electronic address, used by XRechnung / Factur-X).
    client_email = models.EmailField(blank=True)
    #: Buyer reference (BT-10): the client's order number or, for German public bodies, the Leitweg-ID.
    client_reference = models.CharField(max_length=100, blank=True)
    budget_hours = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)


def project_billing(project) -> ProjectBilling | None:
    if project is None:
        return None
    try:
        return project.billing
    except ProjectBilling.DoesNotExist:
        return None


def rate_for(workspace_billing: WorkspaceBilling, project) -> Decimal | None:
    """Hourly rate for time on `project`, or None if that time can't be billed."""
    if not workspace_billing.enabled:
        return None
    pb = project_billing(project)
    if pb and pb.mode == ProjectBilling.Mode.OFF:
        return None
    if pb and pb.mode == ProjectBilling.Mode.HOURLY and pb.hourly_rate is not None:
        return pb.hourly_rate
    return workspace_billing.default_rate


def rounded_seconds(seconds: int, rounding_minutes: int) -> int:
    if not rounding_minutes or seconds <= 0:
        return max(seconds, 0)
    step = rounding_minutes * 60
    return -(-seconds // step) * step


def money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        PAID = "paid", "Paid"
        VOID = "void", "Void"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invoices")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices")
    number = models.CharField(max_length=40)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    issue_date = models.DateField()
    due_date = models.DateField()
    period_start = models.DateField()
    period_end = models.DateField()
    currency = models.CharField(max_length=3)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    issuer_name = models.CharField(max_length=200, blank=True)
    issuer_details = models.TextField(blank=True)
    client_name = models.CharField(max_length=200, blank=True)
    client_details = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    #: Issuer and client data for FatturaPA, copied when the invoice is created (see fatturapa.snapshot).
    einvoice = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issue_date", "-id"]
        constraints = [models.UniqueConstraint(fields=["workspace", "number"], name="unique_invoice_number")]

    def recompute(self):
        self.subtotal = money(sum((line.amount for line in self.lines.all()), Decimal(0)))
        self.tax_amount = money(self.subtotal * self.tax_rate / 100)
        self.total = money(self.subtotal + self.tax_amount)


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=500)
    hours = models.DecimalField(max_digits=10, decimal_places=2)
    rate = models.DecimalField(max_digits=10, decimal_places=2)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class TimeEntry(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="time_entries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="time_entries")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="time_entries")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="time_entries")
    description = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField()
    #: Empty while the timer is running.
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    billable = models.BooleanField(default=True)
    invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.SET_NULL, related_name="entries")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [models.Index(fields=["workspace", "started_at"]), models.Index(fields=["user", "ended_at"])]
        constraints = [
            # At most one running timer per person.
            models.UniqueConstraint(fields=["user"], condition=models.Q(ended_at__isnull=True), name="one_running_timer"),
        ]

    @property
    def running(self):
        return self.ended_at is None

    def elapsed_seconds(self, now=None):
        from django.utils import timezone

        if self.ended_at is None:
            return int(((now or timezone.now()) - self.started_at).total_seconds())
        return self.duration_seconds
