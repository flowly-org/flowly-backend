"""Germany: XRechnung 3.0 in UBL 2.1 syntax (CIUS of EN 16931). Required by German public bodies; accepted
for business-to-business e-invoices (mandatory to receive since 2025)."""

from xml.etree import ElementTree as ET

from . import Format
from .en16931 import amount, common_problems, quantity, semantic

UBL = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CUSTOMIZATION = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
PROFILE = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
REASONS = {
    "E": "Kein Ausweis von Umsatzsteuer, da Kleinunternehmer gemäß § 19 UStG",
    "AE": "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge)",
}
WHERE_ISSUER = "Settings → Invoicing"
WHERE_CLIENT = "project billing"


def cac(parent, tag):
    return ET.SubElement(parent, f"{{{CAC}}}{tag}")


def cbc(parent, tag, text, **attrs):
    el = ET.SubElement(parent, f"{{{CBC}}}{tag}", {k: str(v) for k, v in attrs.items()})
    el.text = str(text)
    return el


def _address(party, who):
    addr = cac(party, "PostalAddress")
    if who["address"]:
        cbc(addr, "StreetName", who["address"])
    cbc(addr, "CityName", who["city"])
    cbc(addr, "PostalZone", who["zip"])
    if who.get("region"):
        cbc(addr, "CountrySubentity", who["region"])
    cbc(cac(addr, "Country"), "IdentificationCode", who["country"] or "DE")


def _tax_category(parent, tag, tax, with_reason):
    cat = cac(parent, tag)
    cbc(cat, "ID", tax["code"])
    cbc(cat, "Percent", amount(tax["rate"]))
    if with_reason and tax["reason"]:
        cbc(cat, "TaxExemptionReasonCode", tax["reason_code"])
        cbc(cat, "TaxExemptionReason", tax["reason"])
    cbc(cac(cat, "TaxScheme"), "ID", "VAT")


def build(m: dict) -> bytes:
    ET.register_namespace("", UBL)
    ET.register_namespace("cac", CAC)
    ET.register_namespace("cbc", CBC)
    cur = m["currency"]
    root = ET.Element(f"{{{UBL}}}Invoice")
    cbc(root, "CustomizationID", CUSTOMIZATION)
    cbc(root, "ProfileID", PROFILE)
    cbc(root, "ID", m["number"])
    cbc(root, "IssueDate", m["issue_date"].isoformat())
    cbc(root, "DueDate", m["due_date"].isoformat())
    cbc(root, "InvoiceTypeCode", "380")
    if m["note"]:
        cbc(root, "Note", m["note"])
    cbc(root, "DocumentCurrencyCode", cur)
    cbc(root, "BuyerReference", m["buyer_reference"] or m["number"])
    period = cac(root, "InvoicePeriod")
    cbc(period, "StartDate", m["period"][0].isoformat())
    cbc(period, "EndDate", m["period"][1].isoformat())

    s, b = m["seller"], m["buyer"]
    party = cac(cac(root, "AccountingSupplierParty"), "Party")
    cbc(party, "EndpointID", s["email"], schemeID="EM")
    cbc(cac(party, "PartyName"), "Name", s["name"])
    _address(party, s)
    if s["vat_id"]:
        scheme = cac(party, "PartyTaxScheme")
        cbc(scheme, "CompanyID", s["vat_id"])
        cbc(cac(scheme, "TaxScheme"), "ID", "VAT")
    if s["tax_number"]:
        scheme = cac(party, "PartyTaxScheme")
        cbc(scheme, "CompanyID", s["tax_number"])
        cbc(cac(scheme, "TaxScheme"), "ID", "FC")
    cbc(cac(party, "PartyLegalEntity"), "RegistrationName", s["name"])
    contact = cac(party, "Contact")
    cbc(contact, "Name", s["contact"])
    cbc(contact, "Telephone", s["phone"])
    cbc(contact, "ElectronicMail", s["email"])

    party = cac(cac(root, "AccountingCustomerParty"), "Party")
    cbc(party, "EndpointID", b["email"], schemeID="EM")
    cbc(cac(party, "PartyName"), "Name", b["name"])
    _address(party, b)
    if b["vat_id"]:
        scheme = cac(party, "PartyTaxScheme")
        cbc(scheme, "CompanyID", b["vat_id"])
        cbc(cac(scheme, "TaxScheme"), "ID", "VAT")
    cbc(cac(party, "PartyLegalEntity"), "RegistrationName", b["name"])

    means = cac(root, "PaymentMeans")
    cbc(means, "PaymentMeansCode", "58")
    cbc(cac(means, "PayeeFinancialAccount"), "ID", m["iban"])
    cbc(cac(root, "PaymentTerms"), "Note", f"Zahlbar bis {m['due_date']:%d.%m.%Y}")

    totals, tax = m["totals"], m["tax"]
    tax_total = cac(root, "TaxTotal")
    cbc(tax_total, "TaxAmount", amount(totals["tax"]), currencyID=cur)
    sub = cac(tax_total, "TaxSubtotal")
    cbc(sub, "TaxableAmount", amount(totals["tax_basis"]), currencyID=cur)
    cbc(sub, "TaxAmount", amount(totals["tax"]), currencyID=cur)
    _tax_category(sub, "TaxCategory", tax, with_reason=True)

    money = cac(root, "LegalMonetaryTotal")
    cbc(money, "LineExtensionAmount", amount(totals["lines"]), currencyID=cur)
    cbc(money, "TaxExclusiveAmount", amount(totals["tax_basis"]), currencyID=cur)
    cbc(money, "TaxInclusiveAmount", amount(totals["grand"]), currencyID=cur)
    cbc(money, "PayableAmount", amount(totals["due"]), currencyID=cur)

    for line in m["lines"]:
        row = cac(root, "InvoiceLine")
        cbc(row, "ID", line["id"])
        cbc(row, "InvoicedQuantity", quantity(line["quantity"]), unitCode="HUR")
        cbc(row, "LineExtensionAmount", amount(line["net"]), currencyID=cur)
        item = cac(row, "Item")
        cbc(item, "Name", line["name"])
        _tax_category(item, "ClassifiedTaxCategory", tax, with_reason=False)
        cbc(cac(row, "Price"), "PriceAmount", amount(line["price"]), currencyID=cur)

    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


class XRechnung(Format):
    key = "xrechnung"
    label = "XRechnung 3.0 (Germany, UBL)"
    countries = ("DE",)
    settings = [
        {"key": "zero_vat", "label": "Invoices without VAT", "type": "choice", "default": "E",
         "help": "Used when the tax rate is 0%", "choices": [
            ["E", "Small business (§ 19 UStG)"], ["AE", "Reverse charge (§ 13b UStG)"]]},
    ]

    def problems(self, invoice, data):
        i, c = data["issuer"], data["client"]
        out = common_problems(invoice, data, WHERE_ISSUER, WHERE_CLIENT, i.get("settings") or {})
        if not (i.get("vat_id") or i.get("tax_code")):
            out.append(f"Your USt-IdNr. or Steuernummer ({WHERE_ISSUER}).")
        if not i.get("email"):
            out.append(f"Your billing email ({WHERE_ISSUER}).")
        if not i.get("phone"):
            out.append(f"Your phone number ({WHERE_ISSUER}).")
        if not i.get("iban"):
            out.append(f"Your IBAN ({WHERE_ISSUER}).")
        if not c.get("email"):
            out.append(f"Client e-invoice email ({WHERE_CLIENT}).")
        return out

    def build(self, invoice, data):
        return build(semantic(invoice, data, REASONS))

    def filename(self, invoice, data):
        return f"{invoice.number}_xrechnung.xml"
