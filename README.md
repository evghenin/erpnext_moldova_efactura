### ERPNext Moldova Efactura

ERPNext integration for Moldovan electronic tax invoices (e-Factura / SFS).

Version **2.1.0**. Requires ERPNext / Frappe v15.

Outgoing invoices are **Sales eFactura**. Incoming invoices are **Purchase eFactura**. Desk workspaces: **eFactura**, **eFactura Sales**, **eFactura Purchase**.

### Features

#### Sales eFactura

- Create e-Factura from a Sales Invoice, or pull items from a Delivery Note / Sales Order (Transfer / Non-Transfer).
- **Transfer** uses a Customer; **Non-Transfer** uses a Supplier. Party IDNO must match the XML.
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

#### Fiscalization

Submitted **Sales Invoice** and **Purchase Invoice** show **Fiscalization** (form indicator and list badge). **Actions → Actualize Fiscal Status** (also bulk from the list) recalculates it.

- **Sales Invoice:** `Not Required` if the customer is not a Company; `Not Applicable` if the customer Territory is outside **Fiscal Territory** in eFactura Settings (including nested territories). Otherwise `Pending` / `In Progress` / `Partial` / `Completed` / `Failed` from linked Sales eFactura coverage.
- **Purchase Invoice:** `Not Required` if the supplier is Individual; otherwise coverage from linked Purchase eFactura (`Pending`, `In Progress`, `Partial`, `Completed`). A draft e-Factura adds the `(Draft)` suffix.

#### Settings

Configure API credentials per Company (**Company API Accounts**), IDNO fields, eFactura currency, VAT-in-rate, **Fiscal Territory**, UOM map (optional auto-add; Sales and Purchase fetch), buying tax templates per company, sales tax settings per company, and Sales / Purchase options under **eFactura Settings**.

There is no site-wide API user. Add one **Company API Accounts** row per legal entity (username and password required). Fetch and daily sync poll each account into that Company. The API URL on Settings is shared.

#### Roles

Assign Desk roles to match the workflow. Fetch / Register / Sign / Accept / Reject / Create PI are shown only when the user can write the corresponding document.

- **eFactura Manager** — Sales and Purchase eFactura, Settings (including per-company API credentials), supplier item map.
- **eFactura Sales User** / **Sales User** / **Sales Manager** — Sales eFactura only (create, submit, register). No Purchase eFactura.
- **Accounts User** / **Purchase User** / **Purchase Manager** — Purchase eFactura (write, submit, accept, sign, reject, create PI). Accounts User can view Sales eFactura but cannot create or register it.
- **System Manager** / **Accounts Manager** — full module access. API URL and Company API Account username/password are permlevel 1 (these roles and eFactura Manager only).

Purchase eFactura cannot be created manually (`create` is off for every role); Fetch / sync still inserts documents server-side.

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/evghenin/erpnext_moldova_efactura.git
bench --site $SITE install-app erpnext_moldova_efactura
bench --site $SITE migrate
```

Then open **eFactura Settings**, set the API URL, and add a **Company API Accounts** row for each legal entity.

### Upgrade from 2.0

```bash
bench --site $SITE migrate
```

A 2.1 migrate bug dropped `customer_party` on Sales eFactura. The follow-up patch fills empty **Customer** parties from the linked Sales Invoice (header, item, or `Sales Invoice.sales_efactura`), then by buyer IDNO. **Non-Transfer** parties are filled from Supplier IDNO, not from SI.customer.

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
