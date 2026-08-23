### ERPNext Moldova Efactura

ERPNext integration for Moldovan electronic tax invoices (e-Factura / SFS): outgoing **Sales eFactura** and incoming **Purchase eFactura**.

Version **2.0**. Requires ERPNext / Frappe v15.

### Features

#### Outgoing — Sales eFactura

- Create e-Factura from a Sales Invoice (Transfer / Non-Transfer).
- Sign, send, and track SFS status (`ef_status` + document status).
- Optional **Load Archived Invoices** in eFactura Settings (Outgoing; off by default) includes SFS status Archived (6) in Fetch / daily sync.
- Bulk **Register Signed** and **Register Unsigned** from the Sales eFactura list. A confirmation lists ineligible documents (skipped) and eligible documents that will be processed.
- Multi-currency: document currency vs eFactura currency (`MDL`) with `ef_conversion_rate`.
- Quantity guards against the linked Sales Invoice (block submit, exclude failed documents, warn on draft save).
- 0% VAT lines are included in document totals (`net_total` / `total`); XML line amounts stay in sync.

#### Incoming — Purchase eFactura

- Fetch buyer invoices from SFS (cannot be created manually).
- Optional **Load Archived Invoices** in eFactura Settings (Incoming; off by default) includes SFS status Archived (6).
- Accept, reject (with comment), PDF, and refresh status.
- Bulk **Sign** and **Accept** from the Purchase eFactura list. A confirmation lists ineligible documents (skipped) and eligible documents that will be processed.
- Map supplier items → Item, supplier UOM → eFactura UOM / purchase UOM.
- **UOM conversion factors** are stored on the row at mapping time, so a later change of Item UOM does not rewrite qty.
- Create Purchase Order and/or Purchase Invoice, or link an existing PI (qty allocation; submit requires full allocation).
- Copy issue date **and time** from `IssuedDate` onto PI posting date/time (eFactura Settings: *Copy Date from Factura*).
- Multi-currency like Sales eFactura: `ef_*` amounts in eFactura currency, document amounts converted.
- Supplier IDNO must match the factura; taxpayer type is stored as Company / Individual / Non-Resident.
- Supplier / buyer / transporter shown as HTML (same pattern as Sales eFactura).

#### Settings

Configure API credentials per Company (Company API Accounts), IDNO fields, eFactura currency, VAT-in-rate, UOM map (optional auto-add), buying tax templates per company, and incoming/outgoing options under **eFactura Settings**.

There is no site-wide API user. Add one **Company API Accounts** row per legal entity (username and password required). Fetch and daily sync poll each account into that Company. The API URL on Settings is shared.

#### Roles

Assign Desk roles to match the workflow. Fetch / Register / Sign / Accept / Reject / Create PI are shown only when the user can write the corresponding document.

- **eFactura Manager** — outgoing and incoming documents, Settings (including per-company API credentials), supplier item map.
- **eFactura Sales User** / **Sales User** / **Sales Manager** — outgoing Sales eFactura only (create, submit, register). No Purchase eFactura.
- **Accounts User** / **Purchase User** / **Purchase Manager** — incoming Purchase eFactura (write, submit, accept, sign, reject, create PI). Accounts User can view Sales eFactura but cannot create or register it.
- **System Manager** / **Accounts Manager** — full module access. API URL and Company API Account username/password are permlevel 1 (these roles and eFactura Manager only).

Purchase eFactura cannot be created manually (`create` is off for every role); Fetch / sync still inserts documents server-side.

Upgrade: run `bench migrate` so Role fixtures and DocType permissions sync. Then assign **eFactura Manager** or **eFactura Sales User** to users as needed; existing Sales / Accounts / Purchase roles keep the mapping above.

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch v2
bench --site $SITE install-app erpnext_moldova_efactura
bench --site $SITE migrate
```

Upgrade from 1.x: install this version and run `bench migrate`. DocTypes are renamed (`eFactura` → `Sales eFactura`, `eFactura Buyer` → `Purchase eFactura`); patches convert existing data.

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

### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.

### License

mit
