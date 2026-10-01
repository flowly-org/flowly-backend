"""Italian e-invoice XML (FatturaPA 1.2.2, "FPR12" business-to-business) for a Flowly invoice.

The file is ready to upload to the Agenzia delle Entrate portal or to send through an intermediary (SDI). It isn't
signed: FPR12 doesn't require a signature.
"""

import re
from decimal import Decimal
from xml.etree import ElementTree as ET

from .models import Invoice, ProjectBilling, WorkspaceBilling, project_billing

NS = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"
STAMP_THRESHOLD = Decimal("77.47")
STAMP_AMOUNT = Decimal("2.00")

ISSUER_FIELDS = ["vat", "vat_country", "tax_code", "regime", "address", "zip", "city", "province", "country", "iban",
                 "vat_nature", "stamp_duty"]
CLIENT_FIELDS = ["vat", "vat_country", "tax_code", "address", "zip", "city", "province", "country", "sdi_code", "pec"]


def snapshot(billing: WorkspaceBilling, pb: ProjectBilling | None) -> dict:
    issuer = {f: getattr(billing, f if f in ("vat_nature", "stamp_duty") else f"issuer_{f}") for f in ISSUER_FIELDS}
    client = {f: getattr(pb, f"client_{f}") if pb else "" for f in CLIENT_FIELDS}
    return {"issuer": issuer, "client": client}


def current(invoice: Invoice) -> dict:
    return snapshot(WorkspaceBilling.for_workspace(invoice.workspace), project_billing(invoice.project))


def data_for(invoice: Invoice) -> dict:
    """Drafts follow the current billing settings; sent invoices keep what they were sent with."""
    if invoice.status == Invoice.Status.DRAFT or not invoice.einvoice.get("issuer"):
        return current(invoice)
    return invoice.einvoice


def problems(invoice: Invoice) -> list[str]:
    """What's missing for a valid e-invoice, in plain words (empty when it can be generated)."""
    d = data_for(invoice)
    i, c = d["issuer"], d["client"]
    out = []
    if invoice.currency != "EUR":
        out.append("E-invoices must be in EUR.")
    if not invoice.issuer_name:
        out.append("Issuer name (billing settings).")
    if not i["vat"]:
        out.append("Issuer Partita IVA (billing settings).")
    for key, label in [("address", "address"), ("zip", "CAP"), ("city", "city")]:
        if not i[key]:
            out.append(f"Issuer {label} (billing settings).")
    if i["zip"] and not re.fullmatch(r"\d{5}", i["zip"]):
        out.append("Issuer CAP must be 5 digits.")
    if not invoice.client_name:
        out.append("Client name (project billing).")
    if not (c["vat"] or c["tax_code"]):
        out.append("Client Partita IVA or codice fiscale (project billing).")
    for key, label in [("address", "address"), ("zip", "CAP"), ("city", "city")]:
        if not c[key]:
            out.append(f"Client {label} (project billing).")
    if c["zip"] and c["country"] == "IT" and not re.fullmatch(r"\d{5}", c["zip"]):
        out.append("Client CAP must be 5 digits.")
    if c["sdi_code"] and not re.fullmatch(r"[A-Z0-9]{6,7}", c["sdi_code"].upper()):
        out.append("Client codice destinatario must be 6–7 letters or digits.")
    if not invoice.lines.exists():
        out.append("The invoice has no lines.")
    return out


def _sub(parent, tag, text=None):
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def _amount(value) -> str:
    return f"{Decimal(value):.2f}"


def _address(parent, tag, who):
    sede = _sub(parent, tag)
    _sub(sede, "Indirizzo", who["address"][:60])
    _sub(sede, "CAP", who["zip"] if who["country"] == "IT" else "00000")
    _sub(sede, "Comune", who["city"][:60])
    if who.get("province") and who["country"] == "IT":
        _sub(sede, "Provincia", who["province"].upper()[:2])
    _sub(sede, "Nazione", (who["country"] or "IT").upper())


def progressive(invoice: Invoice) -> str:
    """ProgressivoInvio: unique per sender, up to 10 characters [A-Za-z0-9]."""
    n, digits, out = invoice.pk, "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return (out or "0").rjust(5, "0")


def filename(invoice: Invoice) -> str:
    i = data_for(invoice)["issuer"]
    return f"{(i['vat_country'] or 'IT').upper()}{i['vat']}_{progressive(invoice)}.xml"


def build(invoice: Invoice) -> bytes:
    missing = problems(invoice)
    if missing:
        raise ValueError(missing)
    d = data_for(invoice)
    i, c = d["issuer"], d["client"]
    ET.register_namespace("p", NS)
    root = ET.Element(f"{{{NS}}}FatturaElettronica", {"versione": "FPR12"})

    # --- header ---------------------------------------------------------------
    header = _sub(root, "FatturaElettronicaHeader")
    tx = _sub(header, "DatiTrasmissione")
    sender = _sub(tx, "IdTrasmittente")
    _sub(sender, "IdPaese", (i["vat_country"] or "IT").upper())
    _sub(sender, "IdCodice", i["tax_code"] or i["vat"])
    _sub(tx, "ProgressivoInvio", progressive(invoice))
    _sub(tx, "FormatoTrasmissione", "FPR12")
    foreign = (c["country"] or "IT").upper() != "IT"
    code = (c["sdi_code"] or "").upper() or ("XXXXXXX" if foreign else "0000000")
    _sub(tx, "CodiceDestinatario", code)
    if c["pec"] and code == "0000000":
        _sub(tx, "PECDestinatario", c["pec"])

    seller = _sub(header, "CedentePrestatore")
    anag = _sub(seller, "DatiAnagrafici")
    vat = _sub(anag, "IdFiscaleIVA")
    _sub(vat, "IdPaese", (i["vat_country"] or "IT").upper())
    _sub(vat, "IdCodice", i["vat"])
    if i["tax_code"]:
        _sub(anag, "CodiceFiscale", i["tax_code"].upper())
    _sub(_sub(anag, "Anagrafica"), "Denominazione", invoice.issuer_name[:80])
    _sub(anag, "RegimeFiscale", i["regime"] or "RF01")
    _address(seller, "Sede", i)

    buyer = _sub(header, "CessionarioCommittente")
    anag = _sub(buyer, "DatiAnagrafici")
    if c["vat"]:
        vat = _sub(anag, "IdFiscaleIVA")
        _sub(vat, "IdPaese", (c["vat_country"] or "IT").upper())
        _sub(vat, "IdCodice", c["vat"])
    if c["tax_code"]:
        _sub(anag, "CodiceFiscale", c["tax_code"].upper())
    _sub(_sub(anag, "Anagrafica"), "Denominazione", invoice.client_name[:80])
    _address(buyer, "Sede", c)

    # --- body -----------------------------------------------------------------
    body = _sub(root, "FatturaElettronicaBody")
    general = _sub(_sub(body, "DatiGenerali"), "DatiGeneraliDocumento")
    _sub(general, "TipoDocumento", "TD01")
    _sub(general, "Divisa", "EUR")
    _sub(general, "Data", invoice.issue_date.isoformat())
    _sub(general, "Numero", invoice.number[:20])
    exempt = Decimal(invoice.tax_rate) == 0
    stamp = bool(i.get("stamp_duty")) and exempt and Decimal(invoice.subtotal) > STAMP_THRESHOLD
    if stamp:
        bollo = _sub(general, "DatiBollo")
        _sub(bollo, "BolloVirtuale", "SI")
        _sub(bollo, "ImportoBollo", _amount(STAMP_AMOUNT))
    _sub(general, "ImportoTotaleDocumento", _amount(invoice.total))
    if invoice.notes.strip():
        _sub(general, "Causale", invoice.notes.strip()[:200])

    goods = _sub(body, "DatiBeniServizi")
    rate = _amount(invoice.tax_rate)
    for n, line in enumerate(invoice.lines.all(), start=1):
        row = _sub(goods, "DettaglioLinee")
        _sub(row, "NumeroLinea", n)
        _sub(row, "Descrizione", line.description[:1000] or "Servizi")
        _sub(row, "Quantita", f"{Decimal(line.hours):.2f}")
        _sub(row, "UnitaMisura", "ore")
        _sub(row, "PrezzoUnitario", _amount(line.rate))
        _sub(row, "PrezzoTotale", _amount(line.amount))
        _sub(row, "AliquotaIVA", rate)
        if exempt:
            _sub(row, "Natura", i["vat_nature"] or "N2.2")
    summary = _sub(goods, "DatiRiepilogo")
    _sub(summary, "AliquotaIVA", rate)
    if exempt:
        _sub(summary, "Natura", i["vat_nature"] or "N2.2")
    _sub(summary, "ImponibileImporto", _amount(invoice.subtotal))
    _sub(summary, "Imposta", _amount(invoice.tax_amount))
    if not exempt:
        _sub(summary, "EsigibilitaIVA", "I")
    if exempt and i["regime"] == "RF19":
        _sub(summary, "RiferimentoNormativo", "Operazione in franchigia da IVA ai sensi della L. 190/2014")

    payment = _sub(body, "DatiPagamento")
    _sub(payment, "CondizioniPagamento", "TP02")
    detail = _sub(payment, "DettaglioPagamento")
    _sub(detail, "ModalitaPagamento", "MP05")
    _sub(detail, "DataScadenzaPagamento", invoice.due_date.isoformat())
    _sub(detail, "ImportoPagamento", _amount(invoice.total))
    if i["iban"]:
        _sub(detail, "IBAN", re.sub(r"\s+", "", i["iban"]).upper())

    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
