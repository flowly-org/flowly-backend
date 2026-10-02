"""The European e-invoice model (EN 16931), shared by XRechnung (UBL syntax) and Factur-X (CII syntax).

`semantic()` turns a Flowly invoice into the business terms of the standard (BT-…); the syntax modules only
serialize it. Flowly invoices have one VAT rate, hours as quantity (unit HUR) and payment by SEPA transfer.
"""

import re
from decimal import Decimal

from ..models import Invoice

#: Zero-VAT categories offered to users, with the standard exemption reason codes.
ZERO_VAT = {"E": "VATEX-EU-132", "AE": "VATEX-EU-AE"}


def amount(value) -> str:
    return f"{Decimal(value):.2f}"


def quantity(value) -> str:
    return f"{Decimal(value):.2f}"


def vat_id(country: str, value: str) -> str:
    """'DE', '123456789' → 'DE123456789' (keeps an existing prefix)."""
    value = re.sub(r"[\s.-]", "", value or "").upper()
    country = (country or "").upper()
    return value if not value or value[:2].isalpha() else f"{country}{value}"


def tax_category(invoice: Invoice, settings: dict, reasons: dict) -> dict:
    rate = Decimal(invoice.tax_rate)
    if rate > 0:
        return {"code": "S", "rate": rate, "reason": "", "reason_code": ""}
    code = settings.get("zero_vat") or "E"
    return {"code": code, "rate": Decimal(0), "reason": reasons.get(code, ""), "reason_code": ZERO_VAT.get(code, "")}


def semantic(invoice: Invoice, data: dict, reasons: dict) -> dict:
    i, c = data["issuer"], data["client"]
    settings = i.get("settings") or {}
    tax = tax_category(invoice, settings, reasons)
    lines = [
        {"id": str(n), "name": (line.description or "Services")[:500], "quantity": line.hours, "price": line.rate,
         "net": line.amount}
        for n, line in enumerate(invoice.lines.all(), start=1)
    ]
    client_country = (c.get("country") or i.get("country") or "").upper()
    return {
        "number": invoice.number,
        "issue_date": invoice.issue_date,
        "due_date": invoice.due_date,
        "period": (invoice.period_start, invoice.period_end),
        "currency": invoice.currency,
        "note": invoice.notes.strip(),
        "buyer_reference": (c.get("reference") or "").strip(),
        "seller": {
            "name": i.get("name") or invoice.issuer_name,
            "vat_id": vat_id(i.get("country"), i.get("vat_id")),
            "tax_number": (i.get("tax_code") or "").strip(),
            "address": i.get("address", ""), "zip": i.get("zip", ""), "city": i.get("city", ""),
            "region": i.get("province", ""), "country": (i.get("country") or "").upper(),
            "email": i.get("email", ""), "phone": i.get("phone", ""),
            "contact": i.get("name") or invoice.issuer_name,
        },
        "buyer": {
            "name": invoice.client_name,
            "vat_id": vat_id(c.get("vat_country") or client_country, c.get("vat")),
            "legal_id": (c.get("tax_code") or "").strip(),
            "address": c.get("address", ""), "zip": c.get("zip", ""), "city": c.get("city", ""),
            "region": c.get("province", ""), "country": client_country,
            "email": c.get("email", ""),
        },
        "iban": re.sub(r"\s+", "", i.get("iban") or "").upper(),
        "tax": tax,
        "lines": lines,
        "totals": {
            "lines": invoice.subtotal, "tax_basis": invoice.subtotal, "tax": invoice.tax_amount,
            "grand": invoice.total, "due": invoice.total,
        },
    }


def common_problems(invoice: Invoice, data: dict, where_issuer: str, where_client: str, settings: dict) -> list[str]:
    i, c = data["issuer"], data["client"]
    out = []
    if not (i.get("name") or invoice.issuer_name):
        out.append(f"Your name or business name ({where_issuer}).")
    for key, label in [("address", "address"), ("zip", "postcode"), ("city", "city")]:
        if not i.get(key):
            out.append(f"Your {label} ({where_issuer}).")
    if not invoice.client_name:
        out.append(f"Client name ({where_client}).")
    for key, label in [("address", "address"), ("zip", "postcode"), ("city", "city")]:
        if not c.get(key):
            out.append(f"Client {label} ({where_client}).")
    zero = Decimal(invoice.tax_rate) == 0
    if zero and (settings.get("zero_vat") or "E") == "AE":
        if not i.get("vat_id"):
            out.append(f"Your VAT number: needed for reverse charge ({where_issuer}).")
        if not c.get("vat"):
            out.append(f"Client VAT number: needed for reverse charge ({where_client}).")
    if not invoice.lines.exists():
        out.append("The invoice has no lines.")
    return out
