# Digital factura example analysis

Analysis date: 2026-09-06. Scope: the two PDFs currently in `erpnext_moldova_efactura/examples/`. These observations now underpin the Orange/ARAX PF importer. They are not a full signature-trust validation.

## Documents and extracted values

Both samples are one-page A4 fiscal invoices for services supplied to **HOTEL LIFE SRL**, recipient IDNO **1024600026571**, VAT code **0211775**. Both have extractable text and an embedded PDF signature. Neither contains an embedded XML invoice or another attached file. The document data must therefore be extracted from the page content rather than an attached structured invoice.

| Field | Orange | ARAX |
| --- | --- | --- |
| File | [74085315_1_FiscalInvoice.pdf](../erpnext_moldova_efactura/examples/74085315_1_FiscalInvoice.pdf) | [AAY9977940.signed.pdf](../erpnext_moldova_efactura/examples/AAY9977940.signed.pdf) |
| Issuer | I.M. Orange Moldova S.A. | ARAX-IMPEX SRL |
| Issuer IDNO | 1003600106115 | 1002600041697 |
| Issuer VAT code | 7800044 | 0501989 |
| Original reference | AAX 8280597, shown in separate series/number fields | AAY9977940, shown as one combined value |
| Proposed series / number split | AAX / 8280597 | AAY / 9977940; retain combined original text |
| Issue date | 2026-08-24 | 2026-08-31 |
| Delivery date | 2026-08-24 | 2026-08-31 |
| Service | Servicii telefonie mobila | Access internet prin linie dedicata |
| Additional service heading | None | PACHET SERVICII INTERNET |
| Original UOM | Empty | GB |
| Quantity | 1 | 1.00 |
| Unit rate excluding VAT | 183.33 | 225.00 |
| Net amount | 183.33 | 225.00 |
| VAT rate | 20% | 20% |
| VAT amount | 36.67 | 45.00 |
| Gross amount | 220.00 | 270.00 |
| Currency presentation | Amount columns labelled lei | Amount columns labelled lei |
| Proposed ERP currency | MDL, based on the Moldovan document and lei labels; no explicit ISO currency field found | MDL on the same basis |
| Other references | Reference number 149640538; provider account 6990591; contract DW56270250 | Act No. 365411 dated 2026-08-31 |
| Explicit service period | Not found | Not found |

Arithmetic was checked with decimal rounding to two places: `183.33 + 36.67 = 220.00`, `225.00 + 45.00 = 270.00`, and both VAT amounts match 20% of the net amount after rounding. The Orange VAT calculation is `36.666 -> 36.67`; the importer must tolerate this ordinary rounding without changing the original totals.

The ARAX act is referenced on the page but is not included as an embedded attachment or as a separate sample in the current folder. Issue/delivery dates alone do not establish a service period, even for recurring monthly services. The original email, receipt time, and delivery channel cannot be established from these PDFs alone. Presence or absence of these invoices in SFS was not checked.

## Extraction findings

### Orange

- Text extraction works with both pypdf and PyMuPDF, but some Romanian characters decode incorrectly, for example `Factura fiscalÅ` and `NumÅrul`. The rendered page displays the proper glyphs. This is a text-mapping issue in the sample, not evidence that the page requires full OCR.
- The numeric table places column **10.6 (VAT rate) before 10.5 (net amount)**. The sequence is quantity, unit rate, VAT rate, net amount, VAT amount, gross amount. Positional parsing based on the ARAX layout would misread it.
- The UOM cell is empty. Preserve that empty source value and require an approved supplier/service mapping to an ERP UOM. Do not silently invent an original UOM.
- The upper mailing address differs from the address in the buyer requisites block. Select buyer fiscal details from section 2 and match the Company by IDNO; do not overwrite master addresses from an import without explicit review.
- The filename, provider reference, provider account, contract, and fiscal series/number are different identifiers. Use `AAX` + `8280597` for fiscal identity; retain the others as separate references.

### ARAX

- Romanian and Russian page text extract correctly. The form contains a standard-looking bilingual numbered table, including empty rows represented by `---`.
- `PACHET SERVICII INTERNET` is a descriptive heading with no amounts; the actual billable row is `Access internet prin linie dedicata`. Do not create a second item from the heading or from the placeholder rows.
- Original UOM is `GB` and quantity is `1.00`. Preserve both. Mapping this billed service to an ERP service Item/UOM is a business rule, not proof of actual measured internet traffic.
- The printed fiscal reference is combined. Preserve `AAY9977940` and derive separate series/number only with a validated pattern.
- The page references an act; support related-document type, number, date, and optional file independently of the factura original.
- The date of the PDF signature is later than the issue/delivery date. These dates must have separate fields.

### Common extraction strategy

Use PDF text and word coordinates first, with a provider profile selected by issuer IDNO and validated layout anchors. Map numbered columns semantically, not by one fixed numeric sequence. Normalize known label variations for parsing while preserving the raw extracted text and original bytes. Apply OCR selectively only when the text layer is missing or required fields cannot be recovered reliably.

Always confirm the recipient IDNO, original fiscal number, dates, item amounts, VAT and document total. Do not count the line total, page total, and invoice total as separate charges. These samples justify two initial provider profiles, not a universal parser for every Moldovan factura.

## Embedded signature observations

| Property | Orange | ARAX |
| --- | --- | --- |
| Signature field | Argint Dana | sig |
| PDF SubFilter | ETSI.CAdES.detached | adbe.pkcs7.detached |
| Signer certificate subject name observed | Argint Dana | Moraru Veronica |
| Certificate organization | Orange Moldova, with issuer IDNO matching the factura | ARAX-IMPEX, with issuer IDNO matching the factura |
| Certificate issuer name | MDQSign | MDQSign |
| PDF signature dictionary time | 2026-08-24 03:49:49 UTC | 2026-09-03 12:29:44 +03:00 |
| Local CMS integrity check | Passed | Passed |
| Signed ByteRange | [0, 122577, 1264639, 446] | [0, 141, 33295, 185103] |
| Signed revision endpoint | 1,265,085 bytes | 218,398 bytes |
| Current file size | 1,686,780 bytes | 218,398 bytes |
| Bytes after signed revision | 421,695 bytes, all zero | None |

The declared times above come from each PDF signature dictionary; they were not established as trusted timestamp-authority times. The Orange certificate material includes intermediate/root certificates as well as the leaf certificate (with repeated entries); the ARAX signature contains one certificate. Embedded certificates alone do not establish trust.

For each file, the bytes identified by `/ByteRange` were concatenated and the embedded CMS signature checked locally with OpenSSL using:

```text
openssl cms -verify -binary -inform DER -in signature.der -content signed-bytes.bin -noverify -out /dev/null
```

Both returned `CMS Verification successful`. This verifies CMS integrity over the selected bytes, while `-noverify` skips verification of the signer certificate, as described in the [OpenSSL documentation](https://docs.openssl.org/master/man1/openssl-cms/#verification-options). Trusted certification paths, revocation, trusted timestamps, and complete PDF signature-policy validation were not checked. Do not convert this result directly into an overall `Valid` status.

The extra Orange bytes are entirely zero padding after the signed PDF, not a newly observed page revision. The signed prefix and the complete file both parse as one page; their extracted text and rendered pixels match in the local comparison. This is useful diagnostic evidence, but not a complete PDF-validator judgment. Preserve the exact received file, including the padding; do not trim or rewrite it during import.

Neither a visible stamp nor the `.signed.pdf` suffix is the signature check. Orange has a real embedded signature despite its ordinary filename. PDF parsers should locate signature fields and support both observed SubFilter variants.

## Consequences for the SF/PF design

1. **Start with PF for services.** Both examples naturally map to a Purchase Factura and a non-stock Purchase Invoice, subject to the existing accounting setup. Nothing in these examples requires a PR or stock movement. They do not validate outgoing SF or return workflows.
2. **Keep identifiers separate.** Original series/number, provider reference, customer account, contract, and related act need distinct storage. Supplier profiles should be scoped by Company and issuer IDNO, with contract/account refinements when needed.
3. **Separate original and ERP UOM.** Orange lacks a UOM and ARAX bills `1 GB` as shown. Preserve source values while mapping to an approved ERP service Item/UOM; flag missing or ambiguous mappings.
4. **Separate all dates.** Issue, delivery, service period, signature time, email receipt and PI posting dates have different meanings. Do not derive a monthly period or change posting dates merely from signing time.
5. **Keep signature results in components.** Store signature presence/format, signed-content integrity, signed-byte coverage and trailing-data observations, certificate trust, revocation, timestamp verification, and overall verification result. Until the required checks are complete, a passed integrity check must remain distinguishable from full verification.
6. **Support related documents.** PF must be able to retain an act reference even before the act file is available; represent referenced and attached documents distinctly.
7. **Preserve accounting mappings as reviewed defaults.** Suggest separate service Items for mobile telephony and dedicated internet, and use configured expense accounts, cost centers and purchase tax templates. The samples do not determine exact account names, account codes or cost centers.
8. **Check duplicates before PI creation.** Match Company, issuer IDNO and original series/number across PF/PEF, then inspect existing PI references. Filename or attachment hash alone is insufficient.

## Suggested first acceptance fixtures

- Orange: read AAX/8280597 rather than the filename/reference number; identify both party IDNOs; extract one item with empty source UOM; correctly map the 10.6/10.5 column order; reproduce 183.33/36.67/220.00.
- ARAX: preserve AAY9977940; extract one charged item, source UOM GB and quantity 1; ignore heading/placeholder rows; retain act 365411; reproduce 225.00/45.00/270.00.
- Date handling: ARAX remains issued/delivered on 2026-08-31 despite the signature time of 2026-09-03; service period remains unknown in both samples unless independently supplied.
- Signature handling: detect both embedded formats; expose local integrity success separately from unchecked trust/revocation; report Orange padding without modifying the original.
- Repeat import: the same file, a renamed copy, or a matching factura fetched through SFS cannot create a second accounting transaction or duplicate coverage.

Further samples are needed for multiple charged rows, discounts, other VAT rates, multi-page documents, credit/return documents, stock transactions, outgoing facturas, scans, and separate signature containers. Analyze original email/attachment bundles separately when defining mailbox intake.

## File identity and method

| File | SHA-256 |
| --- | --- |
| 74085315_1_FiscalInvoice.pdf | a3d9f25d1fb33d1d08143721a3353d8cf52a208753f8ccb6e3b713fd01d5f8db |
| AAY9977940.signed.pdf | 97375cef33f5c6cc6389bd7d50322245483726e22e9f941011cb8b0f61bb1bd2 |

Method: local text extraction with pypdf 6.17.0 and PyMuPDF 1.28.2; rendered-page inspection; PDF field, attachment and ByteRange inspection; certificate inspection; local OpenSSL CMS integrity checks; decimal total/VAT reconciliation. PDF contents were not uploaded to a remote extraction or signature service. Original example files were not modified.
