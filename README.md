### ERPNext Moldova Efactura

ERPNext integration for Moldovan electronic tax invoices (e-Factura / SFS).

Version **3.0.0-dev**. Requires ERPNext / Frappe v15.

Outgoing invoices are **Sales eFactura**. Incoming invoices are **Purchase eFactura**. Desk workspaces: **eFactura**, **eFactura Sales**, **eFactura Purchase**, **Factura Purchase**.

Version 3 adds **Purchase Factura (PF)** for fiscal invoices received outside the SFS integration. The remaining scope is described in the [Sales Factura / Purchase Factura roadmap](#version-3-sales-factura-and-purchase-factura-roadmap).

### Features

#### Sales eFactura

- Create e-Factura from a Sales Invoice, or pull items from a Delivery Note / Sales Order (**Transfer** only).
- **Transfer** uses a Customer. **Non-Transfer** uses a Customer unless marked as a return (then a Supplier). Party IDNO must match the XML.
- **Non-Transfer return:** **Mark as Return** / **Unmark as Return** on a Draft. Link a submitted **Purchase Receipt Return** (`is_return`, `return_against`) 1:1; quantities on the e-Factura stay positive. Create **Sales eFactura for return (Non-Transfer)** from that PR. Submit requires full PR coverage. SI / SO / Delivery Note are not used for Non-Transfer.
- Sign, send, and track SFS status (`ef_status` text labels, separate from document Status: Draft / Submitted / Cancelled / Return).
- A submitted Sales eFactura can be cancelled in ERPNext regardless of SFS status (Fetch can restore it from SFS). **Cancel** in SFS (comment required) is still limited to statuses the API accepts. Drafts in SFS can only be deleted in the SFS portal.
- Optional **Include Archived Invoices** in eFactura Settings (Sales → Fetch / Sync; off by default) includes SFS status Archived (6) in Fetch / daily sync.
- Bulk **Register Signed** and **Register Unsigned** from the Sales eFactura list. A confirmation lists ineligible documents (skipped) and eligible documents that will be processed.
- Download XML / PDF, **Update Status**, and **Update Dates** (issue / delivery) while the invoice is still pending registration.
- Multi-currency: document currency vs eFactura currency (`MDL`) with `ef_conversion_rate`.
- Quantity guards against the linked Sales Invoice (block submit, exclude failed documents, warn on draft save).
- 0% VAT lines are included in document totals (`net_total` / `total`); XML line amounts stay in sync.
- Hourly SFS status sync; daily fetch of supplier invoices and cancelled invoices from SearchInvoices.

#### Purchase eFactura

- Fetch buyer invoices from SFS, including **Signed by Supplier** (cannot be created manually).
- Optional **Include Archived Invoices** in eFactura Settings (Purchase → Fetch / Sync; off by default) includes SFS status Archived (6).
- Optional **Do Not Create Cancelled eFactura** (on by default): Fetch / daily sync skip new documents already cancelled by the supplier in SFS.
- Accept, reject (with comment), PDF, and refresh status.
- Bulk **Sign** and **Accept** from the Purchase eFactura list. A confirmation lists ineligible documents (skipped) and eligible documents that will be processed.
- Map supplier items → Item, supplier UOM → eFactura UOM / purchase UOM (**Map Items**).
- **UOM conversion factors** are stored on the row at mapping time, so a later change of Item UOM does not rewrite qty.
- **Transfer:** create or link a Purchase Invoice (qty allocation; submit requires full allocation). Create Purchase Order only when the factura total is not negative.
- **Non-Transfer:** create or link a Purchase Receipt. **Mark as Return** switches the party to Customer and uses a Delivery Note Return instead.
- **Inverted credit (Transfer):** some suppliers issue a return as **+qty / −rate** with a negative total. ERPNext cannot book a negative rate. A yellow form notice explains this. Link or create a **return Purchase Invoice** (−qty / +rate); totals still have to match. Purchase Order is not offered. XML lines are left unchanged.
- Copy issue date **and time** from `IssuedDate` onto PI posting date/time and PO transaction date (eFactura Settings: Purchase → *Copy Issue Date to Purchase Invoice / Order*). **Supplier Invoice Date (`bill_date`) is not filled.**
- Multi-currency like Sales eFactura: `ef_*` amounts in eFactura currency, document amounts converted.
- Supplier IDNO must match the factura; taxpayer type is stored as Company / Individual / Non-Resident.
- Supplier / buyer / transporter shown as HTML (same pattern as Sales eFactura).
- Status sync prefers invoices awaiting buyer action (SFS 1 / 7 / 9). SearchInvoices uses 7-day `IssuedOn` windows (the SFS API has no pagination).

#### Purchase Factura (version 3)

- Manually register a Moldovan purchase factura received on paper. Preserve its original series, number, dates, issuer/recipient IDNO, VAT details, item values, references, and an optional private scan.
- Import a photographed/scanned paper factura or the observed **Orange Moldova** / **ARAX-IMPEX** PDFs from **Purchase Factura → Import**. **Any Image with AI** sends a JPG/JPEG/PNG or PDF copy to **Google Gemini** (API key in eFactura Settings → Purchase → Paper Import) and validates the returned JSON locally. **PDF Orange / Arax** prefers the embedded text layer. Numbered bilingual forms and retail till facturas (for example METRO: `SERIA … NR.`, `Bon fiscal`, EAN `Cod articol`, packing `Mod amb.`) are both supported. The private original and its hash stay in ERPNext. A PF draft is created only when the required requisites and all extracted arithmetic reconcile.
- Detect embedded PDF signature fields and verify CMS integrity on PDF import, including **Any Image with AI** when the file is a PDF with an extractable text layer. Image-only scan PDFs stay `Paper` with `signature_status` `Not Applicable`. Overall status is `Indeterminate` when integrity passes (certificate trust, revocation, and timestamp authority stay unchecked and are never mapped to `Valid`), `Invalid` when integrity fails, and `Not Applicable` when the PDF has no signature. The form shows a red message for missing or failed signatures on electronic originals.
- Match Company and Supplier through the configured IDNO fields. Creating a Supplier from PF prefills its name, configured IDNO field and fiscal territory, as in PEF. Reuse an existing supplier Item/UOM mapping when one is unambiguous; otherwise the user maps the Item, Factura UOM, purchase UOM before review.
- PF header fields follow PEF naming: `supplier_party_type` / `supplier_party` identify the ERP party, while values read from the original use `f_*` counterparts of PEF's `ef_*` fields. This includes `f_series`, `f_number`, and separate supplier/buyer names, IDNO, VAT IDs, taxpayer types, addresses, bank accounts, bank names, and bank codes. Party detail blocks show these source requisites together. Buyer bank fields extend the current PEF schema because the supported PDF originals contain them and PF must preserve all available source information.
- PF uses the currency and quantity fields corresponding to PEF, with `f_*` for factura fields instead of the XML-specific `ef_*` prefix. `currency` is the ERP document currency; `f_currency` is the preserved original currency, shown as **Factura Currency**. `f_conversion_rate` converts document currency to original currency: document amounts equal original amounts divided by this rate. The document currency defaults from Supplier, then Company, then system settings; the original currency defaults from eFactura Settings for manual entry and comes from the PDF for imports. A positive exchange rate is required when currencies differ; identical currencies use 1.
- Item fields follow PEF: Supplier Item Name, Supplier UOM, Factura UOM (`f_uom`), Quantity In Factura UOM (`f_qty`), Stock UOM/Quantity and purchase UOM/Quantity. `stock_qty = f_qty × f_conversion_factor`; `qty = stock_qty ÷ conversion_factor`. Mapping captures both factors and preserves them against later Item master changes. Quantities and amounts recalculate in the form and on the server.
- Original prices, VAT and totals use `f_*` counterparts of PEF’s `ef_*` fields; `rate`, `rate_with_vat`, `net_amount`, `vat_amount`, `amount` and document totals hold converted values. The existing **VAT Included in Rate** setting also applies to PF. Purchase Invoice unit prices are derived from the converted line amount and purchase quantity. Its Company-currency exchange rate uses the factura rate when the original currency is the Company currency, otherwise the standard ERPNext exchange-rate lookup.
- Record who reviewed the original and mapping. Imported source fields and source item values are immutable after import; ERP mapping remains editable in a draft and resets the review when changed.
- Photo/scan AI extraction fills the PF draft with the best complete result it can obtain; it does not introduce a separate confirmation flow for individual rows. The user reviews and corrects the resulting document through the normal PF form before marking it Reviewed and submitting it.
- Image import fails without creating a PF when critical data cannot be read reliably. Critical failures include missing or ambiguous supplier/customer identity and requisites, missing factura identity or totals, item arithmetic that does not reconcile, and row totals that do not reconcile with document totals. The error asks the user to provide a clearer, properly oriented photo or scan of the complete document. A Gemini API key must be set; extraction does not approve the contents.
- Create a draft Purchase Invoice or link an existing draft/submitted Purchase Invoice. The PF and PI must match Company, Supplier, currency, VAT total, and grand total. Several Purchase Invoice rows may cover one PF item when they share item, UOM and rate and their quantities and amounts sum to the factura line (for example two subcontracting rows of 40 against one embroidery service of 80). The PI receives the original `series + number` as Supplier Invoice No and the issue date as Supplier Invoice Date.
- The standard Purchase Invoice creates all General Ledger entries. PF itself creates no accounting or stock entries. A submitted PI linked to a PF shows `Pending (Draft)` until the reviewed PF is submitted, then `Completed`.
- Prevent a PF and PEF from allocating the same original or Purchase Invoice. Repeated PDF import returns the existing active PF; concurrent creation is serialized per Company. Cancelled PF/PEF/SEF records are ignored for duplicate identity, so the same original can be registered again.
- Cancelling PF removes its fiscal link and coverage without cancelling or reversing its Purchase Invoice. A submitted PF must be cancelled before its PI can be cancelled.
- Version 3 stage one supports ordinary positive Purchase Invoice transactions. Purchase Orders, Purchase Receipts, stock-only facturas, returns, partial/multiple allocations, email intake, and full signature validation remain later stages.

#### Fiscalization

Submitted **Sales Invoice** and **Purchase Invoice** show **Fiscalization** (form indicator and list badge). **Actions → Actualize Fiscal Status** (also bulk from the list) recalculates it.

- **Sales Invoice:** `Not Required` if the customer is not a Company; `Not Applicable` if the customer Territory is outside **Fiscal Territory** in eFactura Settings (including nested territories). Otherwise `Pending` / `In Progress` / `Partial` / `Completed` / `Failed` from linked Sales eFactura coverage.
- **Purchase Invoice:** `Not Required` if the supplier is Individual; otherwise coverage from linked Purchase eFactura (`Pending`, `In Progress`, `Partial`, `Completed`). A draft e-Factura adds the `(Draft)` suffix.
- **Purchase Receipt:** `Not Required` if the supplier is Individual. A linked Purchase eFactura uses the same coverage model as Purchase Invoice. Otherwise the receipt mirrors linked Purchase Invoice fiscalization (PI → PR), then a linked Sales eFactura, then related Sales Invoices (sales order / purchase order). **Purchase Receipt Return** uses the linked return Sales eFactura.

#### Settings

Configure API credentials per Company (**Company API Accounts**), IDNO fields, eFactura currency, VAT-in-rate, **Fiscal Territory**, UOM map (optional auto-add; Sales and Purchase fetch), buying tax templates per company, sales tax settings per company, and Sales / Purchase options under **eFactura Settings**.

There is no site-wide API user. Add one **Company API Accounts** row per legal entity (username and password required). Fetch and daily sync poll each account into that Company. The API URL on Settings is shared.

#### Roles

Assign Desk roles to match the workflow. Fetch / Register / Sign / Accept / Reject / Create PI are shown only when the user can write the corresponding document.

- **eFactura Manager** — Sales and Purchase eFactura, Purchase Factura, Settings (including per-company API credentials), supplier item map.
- **eFactura Sales User** / **Sales User** / **Sales Manager** — Sales eFactura only (create, submit, register). No Purchase eFactura.
- **Accounts User** / **Purchase User** / **Purchase Manager** — Purchase eFactura (write, submit, accept, sign, reject, create PI) and Purchase Factura (create, review, submit, and create/link PI). Accounts User can view Sales eFactura but cannot create or register it.
- **System Manager** / **Accounts Manager** — full module access. API URL and Company API Account username/password are permlevel 1 (these roles and eFactura Manager only).

Purchase eFactura cannot be created manually (`create` is off for every role); Fetch / sync still inserts documents server-side.

### Version 3: Sales Factura and Purchase Factura roadmap

**Status:** the first PF-to-PI stage described above is implemented on the `v3` branch. SF and the extended PF workflows below remain a design and implementation roadmap. Detailed fields, validation rules, and automation settings will continue to be refined against real documents in [examples](erpnext_moldova_efactura/examples/).

The initial [digital factura example analysis](docs/factura-example-analysis.md) covers Orange and ARAX PDFs, extracted fields, embedded signature checks and their limits, and concrete PF import requirements.

#### Scope and naming

Extend this app with **Sales Factura (SF)** and **Purchase Factura (PF)** for Moldovan fiscal invoices (*facturi fiscale*) registered outside the SFS integration. This is part of the same Moldova localization and business workflow, with shared party identification, item mapping, ERP document links, and fiscal coverage. A separate Fiscal Invoice app is not planned.

| DocType | Abbreviation | Registration and processing |
| --- | --- | --- |
| Sales eFactura | SEF | Outgoing factura through the existing SFS integration |
| Purchase eFactura | PEF | Incoming factura through the existing SFS integration |
| Sales Factura | SF | Outgoing factura registered outside the SFS integration |
| Purchase Factura | PF | Incoming factura registered outside the SFS integration |

Keep existing SEF/PEF names, records, and API workflows. `Sales` / `Purchase` follows ERPNext terminology; `Factura` reflects the Moldovan document; `eFactura` identifies the SFS integration.

SF/PF support both paper originals and electronic originals, including digitally signed PDFs sent by telecom and internet providers. Format and delivery channel are independent: an emailed document may be a signed original, a scan, or a copy of a factura also available in SFS. Registration outside the integration does not establish that the document is absent from SFS.

#### Accounting and document boundaries

- SF/PF record fiscal evidence and its allocation to ERP transactions. Submitting them must not create General Ledger or Stock Ledger entries directly.
- Sales Invoice (SI) and Purchase Invoice (PI) remain the accounting documents. Delivery Note (DN) and Purchase Receipt (PR), or invoices with Update Stock, retain their standard stock responsibilities.
- Provide separate SF/PF lists, naming series, forms, and create/link actions in the existing app's sales and purchase workspaces. Shared reports may include all four types with explicit source filters.
- SFS fetch, sync, registration, signing, acceptance, rejection, and remote cancellation continue to operate on SEF/PEF only. SF/PF do not require SFS credentials and do not receive artificial SFS statuses.
- Select the registration route per document. A Company, Customer, or Supplier preference may supply a default; it must not force every transaction for that party into one route.

#### Proposed SF/PF data model

| Area | Information to retain |
| --- | --- |
| Identity | Internal ERP naming series; original issuer series and number in separate fields; Company; direction implied by DocType |
| Parties | Supplier/customer links, issuer and recipient IDNO and relevant original names/requisites, preserving what appears on the original |
| Dates | Issue date, delivery date where present, and receipt/dispatch date |
| Classification | Original format (`Paper`, `Digitally Signed PDF`, `Other Electronic`), delivery channel (`Email`, `Portal`, `In Person`, `Other`), and supported transaction/return type |
| Currency and totals | Original currency, net amount, VAT breakdown and total, plus conversion rate/date and ERP currency amounts when conversion is needed |
| Items | Original description, item code if present, UOM, quantity, rate, net amount, VAT rate/amount, gross amount; mapped ERP Item/UOM and saved conversion factor |
| Allocations | Target ERP document and exact child row, allocated quantity/amount, and the related fiscal document where needed |
| Originals and provenance | Original files, scan for paper, detached signature if supplied, attachment hash, source reference, and email Message-ID/sender/received time when available |
| Review | Review state, reviewer and timestamp, discrepancy notes, and independent signature verification result/evidence |
| Corrections | Return/correction reference, amendment history, cancellation reason, and any explicit reconciliation with a matching SEF/PEF |

Keep original values separate from mapped or converted ERP values. Preserve series/number as text, including leading zeroes. Do not use the ERP document name as the original factura number. Do not invent a missing issue date from the email timestamp.

Store originals as permission-controlled attachments and preserve their bytes. A preview or reprinted PDF is a derivative, not a replacement for the original. Submitted evidence must not be silently overwritten; subsequent files and corrections need an audit trail.

Uploading a PDF does not verify its digital signature. Track signature checking separately, initially `Not Checked`, with proposed results `Valid`, `Invalid`, `Indeterminate`, and `Not Applicable` for formats without a digital signature. Record who or what performed the check, when, and its evidence. Automated verification depends on the actual signature formats found in the examples; unsupported signatures remain explicitly unverified. Business review and cryptographic verification must not be conflated.

#### Purchase Factura workflow: first implementation priority

The first complete use case is a telecom/internet service factura received as a signed PDF:

1. Upload the original and create a PF draft; later, email intake will create the same kind of draft with source metadata.
2. Identify the receiving Company and Supplier using the document's requisites/IDNO. Extract series, number, dates, items, and VAT into the draft for review.
3. Apply Company/Supplier defaults for ERP Item, UOM, expense account, cost center, tax template, and other required accounting dimensions.
4. Check for an existing PF, PEF, or PI before creating another accounting document. Present plausible matches and discrepancies.
5. Review the original, amounts, mapping, and signature-check result. Correct extracted data while retaining the original and extraction provenance.
6. Link an existing PI or create a draft PI from the reviewed PF. Copy the original supplier number/date to `bill_no` / `bill_date`; choose posting date separately under an explicit setting. This proposed PF behavior does not change the existing PEF date policy.
7. Submit the PI through its normal accounting process. Submit the reviewed, fully allocated PF to confirm fiscal coverage, then refresh the PI and related PR indicators.

Allow linking an already submitted PI so receipt of the original can follow accounting entry. Creating a PI must be explicit and repeat-safe: another click or import retry should return the existing linked result or request reconciliation, not create another invoice.

For recurring suppliers, rules should be scoped by Company and Supplier, with contract/account identifier where needed. Defaults may map descriptions to service Items and accounting dimensions; each new original supplies its own number, items, amounts, and VAT. Successful extraction creates a draft for normal document-level review. Missing required requisites, ambiguous party identity, inconsistent amounts, or other critical extraction failures reject the import and ask for a better source image. Email intake and extraction initially create drafts only; unattended accounting submission is a separate future decision.

#### Sales Factura workflow

Create SF from an existing SI, or manually register an externally issued original and link the relevant SI. If no SI exists, provide an explicit action to create a draft SI from reviewed SF data with the same duplicate and retry protections as PF-to-PI creation. Pull ERP values as suggestions when starting from an SI and record the actual issued series/number, dates, items, amounts, and original file or paper scan. Review and submit SF once its required allocations are complete.

For the initial release, SF registers evidence of an issued factura. Generating an official outgoing form, managing physical blank series, digitally signing PDFs, and dispatching documents are separate possible extensions whose requirements will be established from examples. SF submission alone must not be presented as issuing, signing, or delivering the original.

#### ERP links, quantities, and returns

- Reuse suitable item/UOM mapping, currency, tax, allocation, and fiscal-status helpers after separating their shared logic from SFS-specific assumptions. SF/PF have their own parent and item DocTypes; do not manufacture SEF/PEF records to reuse those helpers.
- Represent links at item level and allow partial allocations while drafting. Design for one factura covering multiple ERP documents and one ERP document covered by multiple facturas; the UI may start with the common one-to-one case.
- First deliver ordinary PF-to-PI and SF-to-SI flows. Extend stock-only/Non-Transfer and return flows in a separate stage, using the existing SEF/PEF business semantics as the reference: outgoing delivery to DN, incoming receipt to PR, customer return to DN Return, and supplier return to PR Return, with SI/PI returns for accounting where applicable. Final supported combinations require sample review and explicit validation.
- Match Company, relevant parties/IDNO, transaction direction, currency, quantities, and amounts. Compare quantities in compatible UOMs at the target row level; totals alone cannot prove that the correct items are covered.
- Define currency and quantity precision and rounding tolerances explicitly. Original totals, including zero-VAT lines and any adjustments, must reconcile to the amounts allocated in ERP.
- Preserve the original return signs and values while mapping to ERPNext's supported return representation. A return/correction must reference the original where available and adjust the appropriate coverage rather than cover a second normal purchase/sale.
- Require review and full allocation of the supported factura lines before SF/PF submission. A factura can fully allocate itself while covering only part of a larger SI/PI.

#### Review, submission, and cancellation

Use the normal Frappe lifecycle `Draft -> Submitted -> Cancelled`, with amendment links for corrections. Keep review state (proposed: `Pending Review`, `Needs Correction`, `Reviewed`) separate from `docstatus` and from signature verification.

Submission validates original-document evidence, required requisites, review, and allocation consistency on the server. The exact signature requirements for each original format remain to be defined; no implicit transition from `Not Checked` to `Valid` is allowed.

Cancelling SF/PF removes its active fiscal coverage and refreshes affected indicators; it does not cancel the linked SI/PI, reverse accounting, or cancel a document in SFS. Changes to linked ERP transactions must likewise trigger revalidation: block cancellation/amendment when submitted allocations would become invalid until those allocations are explicitly resolved. Record reasons and retain the original evidence and history.

#### Shared Fiscalization

Extend the existing fiscal coverage calculation to recognize SF/PF alongside SEF/PEF while preserving their separate workflows. Keep the current fiscal-scope decisions (`Not Required`, `Not Applicable`) unless a separate change is explicitly specified.

- Keep a coverage status and a separate source indicator: `SFS`, `External`, or `Mixed` (empty when there is no active source). Original format remains a separate property, because a signed PDF is also electronic.
- Reviewed and submitted SF/PF contributes completed coverage. Drafts may be shown as pending work, consistently with the existing draft indicator, but cannot complete coverage. Cancelled or superseded allocations contribute nothing.
- Full eligible coverage gives `Completed`; some completed coverage gives `Partial`; missing coverage remains `Pending`, with existing SFS progress/failure handling retained and tested. Signature issues stay visible on the source document and are subject to the agreed submission rules.
- Aggregate coverage per target row across both routes without double counting. `Mixed` is valid for distinct portions of a transaction; it does not permit two sources to cover the same portion twice.
- Refresh on SF/PF submission, cancellation, amendment, reconciliation, and relevant ERP document changes. Extend **Actualize Fiscal Status**, list indicators, and existing PI-to-PR propagation. Specify mixed-source precedence and return propagation in tests before enabling them.

Here `Completed` means the application's fiscal evidence and coverage requirements are met. It does not imply SFS registration or cryptographic signature verification for an SF/PF.

#### Duplicate control and later SFS reconciliation

Use our Company plus issuer identity, original series, and original number as the primary duplicate identity, with normalization that preserves the original text. Establish any date/year qualifier only if real issuer numbering patterns require it. Check across the relevant SF/SEF or PF/PEF pair, and use existing SI/PI references as additional evidence. Cancellation or amendment must not bypass duplicate history checks.

File hashes and email identifiers provide additional protection against repeated uploads, forwarding, and import retries; matching bytes are not the only way to identify the same business document. Enforce duplicate/allocation checks during server-side writes, including concurrent imports.

When SFS later supplies a matching PEF/SEF, retain the normal fetch/sync behavior and flag a reconciliation candidate. Do not automatically create another PI/SI or sum both documents' coverage. A reviewed reconciliation should link the representations of the same original, select which source owns each active allocation, and transfer coverage atomically while preserving attachments and audit history. A different number or amount may indicate a correction or a different document and requires review, not automatic merging.

#### Access, settings, and automation

Extend existing sales, purchase, accounting, and manager role patterns to SF/PF, with manual creation enabled for the appropriate roles. Define review, submit, cancel, original-file access, and settings permissions explicitly and respect Company access. Background import uses a defined service identity and applies the same Company, duplicate, and allocation checks as manual entry.

Add settings for supplier processing rules, default accounting mappings, date handling, and enabled intake channels within this app. SF/PF manual entry must work without an API account. Keep original receipt/dispatch, extraction, business review, signature checking, and accounting creation as distinct tracked events so failed automation can be retried without replaying completed actions.

Email intake will associate the message and relevant attachments with a draft and preserve provenance. Extraction should prefer embedded PDF text and use Gemini for scans as needed. A critically incomplete or inconsistent extraction fails and requests a better document instead of creating a partial draft. Neither email arrival nor successful extraction approves the contents; the user reviews and corrects the complete draft before submission.

#### Delivery stages and acceptance criteria

1. **Examples and field design.** Review documents in [examples](erpnext_moldova_efactura/examples/): paper scans, signed provider PDFs, itemized goods, recurring services, and returns/corrections as available. Record layouts, requisites, signature packaging, VAT/rounding patterns, numbering, and expected ERP mappings. Use findings to finalize the schema and parsing fixtures; do not assume every example is a fiscal original.
2. **PF manual workflow.** Add PF/items, originals, permissions, review, duplicate checks, PI creation/linking, allocation, and shared Fiscalization. Acceptance: a provider factura can be entered, reviewed, accounted for once, and fully traced from PF to PI and back; cancellation recalculates coverage.
3. **SF and extended transaction flows.** Add SF/items and SI creation/linking, then validated PR/DN, return, partial, and multiple-document allocation flows. Acceptance: sales, purchases, and their supported returns preserve accounting and stock behavior and enforce coverage limits across all four fiscal types.
4. **Recurring supplier assistance.** Add Company/Supplier rules, PDF text extraction/Gemini image extraction, and suggested mappings. Acceptance: supported documents produce complete editable drafts; critical omissions and arithmetic discrepancies reject the import with a request for a better photo or scan; the user reviews the entire PF before submission without a separate row-confirmation workflow.
5. **Email intake and signature integration.** Add configured mailbox intake, repeat-safe processing, provenance, and signature verification for supported formats. Acceptance: retries/forwarded copies do not create duplicate facturas or invoices, and failed/unsupported signature checks are accurately reported.

Validation should cover manual paper and signed-PDF workflows; permissions and Company isolation; number/date preservation; VAT, currencies, UOMs and rounding; repeated/concurrent imports; cross-route duplicates and SFS reconciliation; partial/mixed coverage; cancellation/amendment and returns; PI-to-PR propagation; and regression of existing SEF/PEF fetch, sync, signing and allocations. Add targeted tests as each stage is implemented. No SF/PF migration or behavior change is included by documenting this plan.

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/evghenin/erpnext_moldova_efactura.git
bench --site $SITE install-app erpnext_moldova_efactura
bench --site $SITE migrate
```

Then open **eFactura Settings**, set the SFS API URL, add a **Company API Accounts** row for each legal entity, and set the **Gemini API Key** under Purchase → Paper Import if you import photographed facturas.

### Upgrade from 2.x to version 3

Version 3 adds the `pypdf` runtime dependency, the Purchase Factura DocTypes, a Purchase Invoice link, and purchase workspace entries:

```bash
cd $PATH_TO_YOUR_BENCH
bench setup requirements --python erpnext_moldova_efactura
bench --site $SITE migrate
bench build --app erpnext_moldova_efactura
```

Confirm that **Company IDNO Field** and **Supplier IDNO Field** are configured in eFactura Settings before creating or importing PF records. For paper photos, set **Gemini API Key** (and optionally **Gemini Model**, default `gemini-3.6-flash`). Configure the existing Purchase Tax Settings and supplier Item/UOM mappings used to create Purchase Invoices.

The v3 PF currency/UOM migration copies earlier PF originals into the `f_*` fields and preserves their currency, totals, original quantities, purchase quantities and existing links, including submitted records. Records from the first single-currency PF schema start with matching original/document currencies and rate 1; they are not rebooked. Supplier payable-account currency must match its billing currency under the normal ERPNext rules.

### Upgrade from 2.0

```bash
bench --site $SITE migrate
```

A 2.1 migrate bug dropped `customer_party` on Sales eFactura. The follow-up patch fills empty **Customer** parties from the linked Sales Invoice (header, item, or `Sales Invoice.sales_efactura`), then by buyer IDNO. **Non-Transfer** parties are filled from Supplier IDNO, not from SI.customer.

The 2.1 rename of Purchase eFactura `supplier` → `supplier_party` left some Party values empty. The follow-up patch fills empty **Supplier** parties from a leftover `supplier` column, linked Purchase Invoice / Receipt / Order, then by supplier IDNO. **Return** parties are filled from the linked Delivery Note or customer IDNO, not from PI.supplier. After submit, Party is read-only.

Non-Transfer Sales eFactura party is **Customer** unless **Is Return**. A migrate patch retargets existing Non-Transfer rows (Supplier stays only on returns).

### Upgrade from 1.x

```bash
bench get-app https://github.com/evghenin/erpnext_moldova_efactura.git
bench --site $SITE migrate
```

- DocTypes are renamed (`eFactura` → `Sales eFactura`, `eFactura Buyer` → `Purchase eFactura`); patches convert existing data.
- Site-wide API username/password are removed. After migrate, copy leftover credentials onto **Company API Accounts** (the patch does this when it can) and fill any missing company rows.
- Assign **eFactura Manager** or **eFactura Sales User** as needed; existing Sales / Accounts / Purchase roles keep the mapping above.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/erpnext_moldova_efactura
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### Tests

```bash
bench --site $SITE run-tests --app erpnext_moldova_efactura
```

### License

mit

PF schema updates also migrate the intermediate `ef_*` fields to `f_*` without recalculating their stored values. PF Item exposes `purchase_invoice` and `pi_detail`; its invoice link follows the parent PF link. Expense Account and Cost Center are configured on Purchase Invoice using standard ERPNext defaults, not on PF Item.
