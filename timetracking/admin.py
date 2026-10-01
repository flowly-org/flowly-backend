from django.contrib import admin

from .models import Invoice, InvoiceLine, ProjectBilling, TimeEntry, WorkspaceBilling


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "workspace", "client_name", "status", "total", "currency", "issue_date")
    list_filter = ("status",)
    inlines = [InvoiceLineInline]


@admin.register(TimeEntry)
class TimeEntryAdmin(admin.ModelAdmin):
    list_display = ("user", "workspace", "task", "started_at", "duration_seconds", "billable", "invoice")
    list_filter = ("billable",)


admin.site.register(WorkspaceBilling)
admin.site.register(ProjectBilling)
