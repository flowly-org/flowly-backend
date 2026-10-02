Official XML schema of Italian e-invoices (FatturaPA 1.2.2, from fatturapa.gov.it) and the W3C xmldsig schema it
imports (DOCTYPE removed, import pointed at the local copy). Used by the tests to validate the generated XML.

`ubl/` (UBL 2.1, OASIS) and `cii/` (UN/CEFACT CII D16B) come from the KoSIT XRechnung validator configuration;
the tests use them for XRechnung and Factur-X. Business rules (EN 16931 / XRechnung schematron) are checked with
the official validators: see scripts/validate-einvoices.sh.
