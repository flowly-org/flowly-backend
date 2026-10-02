"""Issuer details move from workspace billing to the workspace owner's invoicing profile."""

from django.db import migrations


def forwards(apps, schema_editor):
    WorkspaceBilling = apps.get_model("timetracking", "WorkspaceBilling")
    InvoicingProfile = apps.get_model("timetracking", "InvoicingProfile")
    Membership = apps.get_model("workspaces", "Membership")
    for billing in WorkspaceBilling.objects.all():
        if not (billing.issuer_name or billing.issuer_details or billing.issuer_vat):
            continue
        owner = Membership.objects.filter(workspace_id=billing.workspace_id, role="owner").order_by("id").first()
        if owner is None:
            continue
        profile, _ = InvoicingProfile.objects.get_or_create(user_id=owner.user_id)
        if profile.name or profile.vat_id:
            continue  # already filled from another workspace: keep the first
        profile.name = billing.issuer_name
        profile.details = billing.issuer_details
        profile.country = billing.issuer_country or "IT"
        profile.vat_id = billing.issuer_vat
        profile.tax_code = billing.issuer_tax_code
        profile.address = billing.issuer_address
        profile.zip = billing.issuer_zip
        profile.city = billing.issuer_city
        profile.province = billing.issuer_province
        profile.iban = billing.issuer_iban
        profile.format_settings = {
            "fatturapa": {"regime": billing.issuer_regime or "RF01", "vat_nature": billing.vat_nature or "N2.2",
                          "stamp_duty": billing.stamp_duty}
        }
        profile.save()


class Migration(migrations.Migration):
    dependencies = [("timetracking", "0003_invoicing_profile"), ("workspaces", "0005_workspace_time_tracking")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
