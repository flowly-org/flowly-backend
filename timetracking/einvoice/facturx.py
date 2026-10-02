"""France: Factur-X / ZUGFeRD XML, EN 16931 profile, in UN/CEFACT CII syntax. Accepted by Chorus Pro and the
platforms of the French e-invoicing reform; the same XML is ZUGFeRD in Germany. (The hybrid PDF/A-3 with this
XML embedded isn't generated yet: Flowly prints PDFs from the browser.)"""

import re
from xml.etree import ElementTree as ET

from . import Format
from .en16931 import amount, common_problems, quantity, semantic

RSM = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
RAM = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
UDT = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"
QDT = "urn:un:unece:uncefact:data:standard:QualifiedDataType:100"
GUIDELINE = "urn:cen.eu:en16931:2017"
REASONS = {"E": "TVA non applicable, art. 293 B du CGI", "AE": "Autoliquidation"}
#: Mandatory mentions on French invoices (Code de commerce L441-10), as EN 16931 notes with their subject codes.
MENTIONS = [
    ("PMD", "penalties", "En cas de retard de paiement, une pénalité égale à trois fois le taux d'intérêt légal sera exigible."),
    ("PMT", "recovery", "Indemnité forfaitaire pour frais de recouvrement en cas de retard de paiement : 40 €."),
    ("AAB", "discount", "Pas d'escompte pour paiement anticipé."),
]
WHERE_ISSUER = "Settings → Invoicing"
WHERE_CLIENT = "project billing"


def ram(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, f"{{{RAM}}}{tag}", {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def date(parent, tag, value):
    holder = ram(parent, tag)
    el = ET.SubElement(holder, f"{{{UDT}}}DateTimeString", {"format": "102"})
    el.text = value.strftime("%Y%m%d")


def legal_scheme(value: str) -> str | None:
    """SIRET (14 digits) → 0009, SIREN (9 digits) → 0002; None for anything else."""
    digits = re.sub(r"\s", "", value or "")
    return {14: "0009", 9: "0002"}.get(len(digits)) if digits.isdigit() else None


def _party(parent, tag, who, *, legal_id, email):
    party = ram(parent, tag)
    digits = re.sub(r"\s", "", legal_id or "")
    scheme = legal_scheme(digits)
    if scheme == "0009":
        ram(party, "GlobalID", digits, schemeID="0009")  # SIRET identifies the establishment (BT-29 / BT-46)
    ram(party, "Name", who["name"])
    if scheme:
        # The legal entity is the SIREN: the first 9 digits of a SIRET (BT-30 / BT-47, BR-FR-10).
        ram(ram(party, "SpecifiedLegalOrganization"), "ID", digits[:9], schemeID="0002")
    addr = ram(party, "PostalTradeAddress")
    ram(addr, "PostcodeCode", who["zip"])
    if who["address"]:
        ram(addr, "LineOne", who["address"])
    ram(addr, "CityName", who["city"])
    ram(addr, "CountryID", who["country"] or "FR")
    if email:
        ram(ram(party, "URIUniversalCommunication"), "URIID", email, schemeID="EM")
    if who["vat_id"]:
        ram(ram(party, "SpecifiedTaxRegistration"), "ID", who["vat_id"], schemeID="VA")
    return party


def _tax(parent, tax, *, calculated=None, basis=None):
    el = ram(parent, "ApplicableTradeTax")
    if calculated is not None:
        ram(el, "CalculatedAmount", amount(calculated))
    ram(el, "TypeCode", "VAT")
    if calculated is not None and tax["reason"]:
        ram(el, "ExemptionReason", tax["reason"])
    if basis is not None:
        ram(el, "BasisAmount", amount(basis))
    ram(el, "CategoryCode", tax["code"])
    if calculated is not None and tax["reason_code"]:
        ram(el, "ExemptionReasonCode", tax["reason_code"])
    ram(el, "RateApplicablePercent", amount(tax["rate"]))


def build(m: dict, mentions: list[tuple[str, str]] = ()) -> bytes:
    for prefix, ns in (("rsm", RSM), ("ram", RAM), ("udt", UDT), ("qdt", QDT)):
        ET.register_namespace(prefix, ns)
    root = ET.Element(f"{{{RSM}}}CrossIndustryInvoice")
    ctx = ET.SubElement(root, f"{{{RSM}}}ExchangedDocumentContext")
    ram(ram(ctx, "GuidelineSpecifiedDocumentContextParameter"), "ID", GUIDELINE)
    doc = ET.SubElement(root, f"{{{RSM}}}ExchangedDocument")
    ram(doc, "ID", m["number"])
    ram(doc, "TypeCode", "380")
    date(doc, "IssueDateTime", m["issue_date"])
    if m["note"]:
        ram(ram(doc, "IncludedNote"), "Content", m["note"])
    for code, text in mentions:
        note = ram(doc, "IncludedNote")
        ram(note, "Content", text)
        ram(note, "SubjectCode", code)

    trade = ET.SubElement(root, f"{{{RSM}}}SupplyChainTradeTransaction")
    tax = m["tax"]
    for line in m["lines"]:
        item = ram(trade, "IncludedSupplyChainTradeLineItem")
        ram(ram(item, "AssociatedDocumentLineDocument"), "LineID", line["id"])
        ram(ram(item, "SpecifiedTradeProduct"), "Name", line["name"])
        ram(ram(ram(item, "SpecifiedLineTradeAgreement"), "NetPriceProductTradePrice"), "ChargeAmount", amount(line["price"]))
        ram(ram(item, "SpecifiedLineTradeDelivery"), "BilledQuantity", quantity(line["quantity"]), unitCode="HUR")
        settle = ram(item, "SpecifiedLineTradeSettlement")
        _tax(settle, tax)
        ram(ram(settle, "SpecifiedTradeSettlementLineMonetarySummation"), "LineTotalAmount", amount(line["net"]))

    s, b = m["seller"], m["buyer"]
    agreement = ram(trade, "ApplicableHeaderTradeAgreement")
    if m["buyer_reference"]:
        ram(agreement, "BuyerReference", m["buyer_reference"])
    _party(agreement, "SellerTradeParty", s, legal_id=s["tax_number"], email=s["email"])
    _party(agreement, "BuyerTradeParty", b, legal_id=b["legal_id"], email=b["email"])
    delivery = ram(ram(trade, "ApplicableHeaderTradeDelivery"), "ActualDeliverySupplyChainEvent")
    date(delivery, "OccurrenceDateTime", m["period"][1])

    settlement = ram(trade, "ApplicableHeaderTradeSettlement")
    ram(settlement, "InvoiceCurrencyCode", m["currency"])
    if m["iban"]:
        means = ram(settlement, "SpecifiedTradeSettlementPaymentMeans")
        ram(means, "TypeCode", "58")
        ram(ram(means, "PayeePartyCreditorFinancialAccount"), "IBANID", m["iban"])
    totals = m["totals"]
    _tax(settlement, tax, calculated=totals["tax"], basis=totals["tax_basis"])
    period = ram(settlement, "BillingSpecifiedPeriod")
    date(period, "StartDateTime", m["period"][0])
    date(period, "EndDateTime", m["period"][1])
    terms = ram(settlement, "SpecifiedTradePaymentTerms")
    date(terms, "DueDateDateTime", m["due_date"])
    sums = ram(settlement, "SpecifiedTradeSettlementHeaderMonetarySummation")
    ram(sums, "LineTotalAmount", amount(totals["lines"]))
    ram(sums, "TaxBasisTotalAmount", amount(totals["tax_basis"]))
    ram(sums, "TaxTotalAmount", amount(totals["tax"]), currencyID=m["currency"])
    ram(sums, "GrandTotalAmount", amount(totals["grand"]))
    ram(sums, "DuePayableAmount", amount(totals["due"]))

    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


class FacturX(Format):
    key = "facturx"
    label = "Factur-X EN 16931 (France, CII)"
    countries = ("FR",)
    settings = [
        {"key": "zero_vat", "label": "Invoices without VAT", "type": "choice", "default": "E",
         "help": "Used when the tax rate is 0%", "choices": [
            ["E", "Franchise en base de TVA (art. 293 B du CGI)"], ["AE", "Autoliquidation (reverse charge)"]]},
        *[{"key": key, "label": label, "type": "text", "default": default}
          for (_, key, default), label in zip(MENTIONS, ["Late payment penalties", "Recovery costs", "Early payment discount"])],
    ]

    def problems(self, invoice, data):
        i, c = data["issuer"], data["client"]
        out = common_problems(invoice, data, WHERE_ISSUER, WHERE_CLIENT, i.get("settings") or {})
        if not legal_scheme(i.get("tax_code", "")):
            out.append(f"Your SIREN (9 digits) or SIRET (14 digits) ({WHERE_ISSUER}).")
        if has_vat(invoice) and not i.get("vat_id"):
            out.append(f"Your VAT number (numéro de TVA) ({WHERE_ISSUER}).")
        return out

    def build(self, invoice, data):
        settings = {**self.defaults(), **(data["issuer"].get("settings") or {})}
        mentions = [(code, (settings.get(key) or default).strip()) for code, key, default in MENTIONS]
        return build(semantic(invoice, data, REASONS), mentions)

    def filename(self, invoice, data):
        return f"{invoice.number}_factur-x.xml"


def has_vat(invoice) -> bool:
    from decimal import Decimal

    return Decimal(invoice.tax_rate) > 0
