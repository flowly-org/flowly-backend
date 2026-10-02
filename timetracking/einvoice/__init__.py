"""National e-invoice formats.

Each format is a class registered in FORMATS. It names the countries it serves, describes its own settings (rendered
by the Settings page), lists what's missing for an invoice and builds the file. The issuer comes from the invoicing
profile of whoever created the invoice; the client from the project's billing.

Adding a format: subclass Format in a module here, list it in FORMATS.
"""

from ..models import Invoice, InvoicingProfile, project_billing

ISSUER_FIELDS = ["name", "details", "country", "vat_id", "tax_code", "address", "zip", "city", "province", "iban", "email", "phone"]
CLIENT_FIELDS = ["vat_country", "vat", "tax_code", "address", "zip", "city", "province", "country", "sdi_code", "pec",
                 "email", "reference"]


class Format:
    key = ""
    label = ""
    countries: tuple[str, ...] = ()
    extension = "xml"
    content_type = "application/xml"
    #: [{"key", "label", "type": "choice" | "bool" | "text", "choices": [[value, label]], "default", "help"}]
    settings: list[dict] = []

    def defaults(self) -> dict:
        return {f["key"]: f.get("default") for f in self.settings}

    def problems(self, invoice: Invoice, data: dict) -> list[str]:
        raise NotImplementedError

    def build(self, invoice: Invoice, data: dict) -> bytes:
        raise NotImplementedError

    def filename(self, invoice: Invoice, data: dict) -> str:
        return f"{invoice.number}.{self.extension}"

    def describe(self) -> dict:
        return {"key": self.key, "label": self.label, "countries": list(self.countries), "settings": self.settings}


from .facturx import FacturX  # noqa: E402
from .fatturapa import FatturaPA  # noqa: E402
from .xrechnung import XRechnung  # noqa: E402

FORMATS: dict[str, Format] = {f.key: f for f in (FatturaPA(), XRechnung(), FacturX())}


def format_for(country: str) -> Format | None:
    country = (country or "").upper()
    return next((f for f in FORMATS.values() if country in f.countries), None)


def snapshot(user, pb) -> dict:
    profile = InvoicingProfile.for_user(user) if user is not None else InvoicingProfile()
    fmt = format_for(profile.country)
    issuer = {f: getattr(profile, f) for f in ISSUER_FIELDS}
    issuer["settings"] = {**fmt.defaults(), **(profile.format_settings.get(fmt.key) or {})} if fmt else {}
    client = {f: getattr(pb, f"client_{f}") if pb else "" for f in CLIENT_FIELDS}
    return {"format": fmt.key if fmt else None, "issuer": issuer, "client": client}


def current(invoice: Invoice) -> dict:
    return snapshot(invoice.created_by, project_billing(invoice.project))


def data_for(invoice: Invoice) -> dict:
    """Drafts follow the creator's current details; sent invoices keep what they were sent with."""
    if invoice.status == Invoice.Status.DRAFT or not invoice.einvoice.get("issuer"):
        return current(invoice)
    return invoice.einvoice


def export(invoice: Invoice):
    """→ (format or None, data, missing list)"""
    data = data_for(invoice)
    fmt = FORMATS.get(data.get("format") or "")
    if fmt is None:
        return None, data, ["No e-invoice format for your country yet (Settings → Invoicing)."]
    return fmt, data, fmt.problems(invoice, data)
