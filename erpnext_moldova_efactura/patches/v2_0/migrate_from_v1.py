"""One-shot upgrade from released v1 (master) after DocType JSON sync."""

import json

import frappe

from erpnext_moldova_efactura.utils.taxpayer_type import CODE_TO_LABEL

SEF = "Sales eFactura"
SEF_ITEM = "Sales eFactura Item"

# v1 column → v2 column. New columns exist after sync; copy then drop the old ones.
SEF_FIELD_COPIES = (
	("customer_party", "customer"),
	("supplier_bank_account", "company_bank_account"),
	("reference_name", "sales_invoice"),
)

SEF_DROP_COLUMNS = (
	"customer_party",
	"customer_party_type",
	"supplier_party",
	"supplier_party_type",
	"supplier",
	"supplier_bank_account",
	"reference_name",
	"reference_doctype",
)

OLD_NUMBER_CARDS = (
	"Finished",
	"Pending Registration",
	"Pending Customer Signature",
	"Rejected by Customer",
)

STALE_PROPERTY_SETTERS = (
	"eFactura Buyer-naming_series-default",
	"eFactura Buyer-naming_series-options",
	"Purchase eFactura-ef_number-in_list_view",
	"Sales eFactura-ef_number-in_list_view",
	"Purchase eFactura-status-in_list_view",
	"Purchase eFactura-efactura_status-hidden",
	"Purchase eFactura-main-field_order",
	"Purchase eFactura-custom_column_break_ndj1j",
)


def execute():
	_copy_sales_efactura_fields()
	_copy_item_sales_invoice()
	_drop_legacy_sef_columns()
	_migrate_taxpayer_types()
	_apply_settings_defaults()
	_migrate_workspaces()
	_cleanup_customize_form()
	frappe.clear_cache()


def _live_fieldnames(doctype):
	if not frappe.db.exists("DocType", doctype):
		return set()
	return {df.fieldname for df in frappe.get_meta(doctype).fields}


def _copy_sales_efactura_fields():
	if not frappe.db.table_exists(SEF):
		return
	live = _live_fieldnames(SEF)
	for old, new in SEF_FIELD_COPIES:
		# 2.1 reuses customer_party; never copy the live field into a leftover column.
		if old in live:
			continue
		if not frappe.db.has_column(SEF, old) or not frappe.db.has_column(SEF, new):
			continue
		where_ref = ""
		if old == "reference_name" and frappe.db.has_column(SEF, "reference_doctype"):
			where_ref = "AND IFNULL(reference_doctype, 'Sales Invoice') IN ('Sales Invoice', '')"
		frappe.db.sql(
			f"""
			UPDATE `tab{SEF}`
			SET `{new}` = `{old}`
			WHERE IFNULL(`{new}`, '') = ''
				AND IFNULL(`{old}`, '') != ''
				{where_ref}
			"""
		)


def _copy_item_sales_invoice():
	if not frappe.db.table_exists(SEF) or not frappe.db.table_exists(SEF_ITEM):
		return
	if not frappe.db.has_column(SEF, "sales_invoice"):
		return
	if not frappe.db.has_column(SEF_ITEM, "sales_invoice"):
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{SEF_ITEM}` i
		INNER JOIN `tab{SEF}` p ON p.name = i.parent
		SET i.sales_invoice = p.sales_invoice
		WHERE IFNULL(i.sales_invoice, '') = ''
			AND IFNULL(p.sales_invoice, '') != ''
		"""
	)


def _drop_legacy_sef_columns():
	"""Drop leftover v1/v2.0 columns. Never drop fields that are still on the DocType.

	2.1 put the live party back on ``customer_party`` / ``customer_party_type``.
	Dropping those as v1 leftovers wiped every Sales eFactura party after migrate.
	"""
	if not frappe.db.table_exists(SEF):
		return
	live = _live_fieldnames(SEF)
	for col in SEF_DROP_COLUMNS:
		if col in live:
			continue
		if frappe.db.has_column(SEF, col):
			frappe.db.sql_ddl(f"ALTER TABLE `tab{SEF}` DROP COLUMN `{col}`")


def _migrate_taxpayer_types():
	fields = {
		SEF: (
			"ef_supplier_taxpayer_type",
			"ef_customer_taxpayer_type",
			"ef_transporter_taxpayer_type",
		),
	}
	for doctype, cols in fields.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for field in cols:
			if not frappe.db.has_column(doctype, field):
				continue
			for code, label in CODE_TO_LABEL.items():
				frappe.db.sql(
					f"""
					UPDATE `tab{doctype}`
					SET `{field}` = %s
					WHERE `{field}` = %s
					""",
					(label, code),
				)


def _apply_settings_defaults():
	if not frappe.db.exists("DocType", "eFactura Settings"):
		return
	meta = frappe.get_meta("eFactura Settings")
	for field, value in (
		("copy_date_from_factura", 1),
		("do_not_create_cancelled_invoices", 1),
	):
		if not meta.has_field(field):
			continue
		if frappe.db.sql(
			"SELECT 1 FROM tabSingles WHERE doctype=%s AND field=%s",
			("eFactura Settings", field),
		):
			continue
		frappe.db.set_single_value("eFactura Settings", field, value)


def _migrate_workspaces():
	old, new = "Moldova eFactura", "eFactura"
	if frappe.db.exists("Workspace", old):
		if frappe.db.exists("Workspace", new):
			frappe.delete_doc("Workspace", old, force=1)
		else:
			frappe.rename_doc("Workspace", old, new, force=True)
			frappe.db.set_value("Workspace", new, "title", "eFactura", update_modified=False)

	for name in OLD_NUMBER_CARDS:
		if not frappe.db.exists("Number Card", name):
			continue
		document_type = frappe.db.get_value("Number Card", name, "document_type")
		if document_type in (SEF, "eFactura"):
			frappe.delete_doc("Number Card", name, force=1)

	chart_old, chart_new = "Sales eFactura", "Sales eFactura Statuses"
	if frappe.db.exists("Dashboard Chart", chart_old) and not frappe.db.exists("Dashboard Chart", chart_new):
		frappe.rename_doc("Dashboard Chart", chart_old, chart_new, force=True)
	elif frappe.db.exists("Dashboard Chart", chart_old) and frappe.db.exists("Dashboard Chart", chart_new):
		frappe.delete_doc("Dashboard Chart", chart_old, force=1)
	if frappe.db.exists("Dashboard Chart", "eFactura"):
		document_type = frappe.db.get_value("Dashboard Chart", "eFactura", "document_type")
		if document_type in (SEF, "eFactura"):
			frappe.delete_doc("Dashboard Chart", "eFactura", force=1)

	if frappe.db.exists("Workspace", new):
		frappe.db.set_value("Workspace", new, "parent_page", "", update_modified=False)
		frappe.db.set_value("Workspace", new, "sequence_id", 3.0, update_modified=False)

	for name, sequence_id in (("eFactura Sales", 3.1), ("eFactura Purchase", 3.2)):
		if not frappe.db.exists("Workspace", name):
			continue
		frappe.db.set_value("Workspace", name, "title", name, update_modified=False)
		frappe.db.set_value("Workspace", name, "parent_page", "eFactura", update_modified=False)
		frappe.db.set_value("Workspace", name, "sequence_id", sequence_id, update_modified=False)

	if frappe.db.table_exists("Workspace Shortcut"):
		for label, url in (("Sales", "/app/efactura-sales"), ("Purchase", "/app/efactura-purchase")):
			frappe.db.sql(
				"""
				UPDATE `tabWorkspace Shortcut`
				SET type = 'URL', url = %s, link_to = NULL
				WHERE parent = 'eFactura' AND label = %s
				""",
				(url, label),
			)

	_rewrite_stale_routes()


def _rewrite_stale_routes():
	if frappe.db.table_exists("Route History"):
		replacements = (
			("eFactura Buyer", "Purchase eFactura"),
			("Workspaces/Moldova eFactura", "Workspaces/eFactura"),
			("efactura-buyer", "purchase-efactura"),
			("moldova-efactura", "efactura"),
		)
		for old, new in replacements:
			frappe.db.sql(
				"""
				UPDATE `tabRoute History`
				SET route = REPLACE(route, %s, %s)
				WHERE route LIKE %s
				""",
				(old, new, f"%{old}%"),
			)
		frappe.db.sql(
			"""
			UPDATE `tabRoute History`
			SET route = 'List/Sales eFactura/List'
			WHERE route IN ('List/eFactura/List', 'List/eFactura')
			"""
		)

	if not frappe.db.has_column("User", "recent"):
		return
	renames = {
		"eFactura Buyer": "Purchase eFactura",
		"eFactura": "Sales eFactura",
	}
	for name, recent in frappe.db.sql("SELECT name, recent FROM `tabUser` WHERE IFNULL(recent, '') != ''"):
		try:
			data = json.loads(recent)
		except (TypeError, ValueError):
			continue
		if not isinstance(data, list):
			continue
		changed = False
		new_data = []
		for row in data:
			if not (isinstance(row, list) and row):
				new_data.append(row)
				continue
			doctype = row[0]
			if doctype in renames:
				row = [renames[doctype], *row[1:]]
				changed = True
			new_data.append(row)
		if changed:
			frappe.db.set_value("User", name, "recent", json.dumps(new_data), update_modified=False)


def _cleanup_customize_form():
	if frappe.db.exists("DocType", SEF):
		frappe.db.set_value("DocType", SEF, "title_field", "", update_modified=False)
	if frappe.db.exists("DocType", "Purchase eFactura"):
		frappe.db.set_value("DocType", "Purchase eFactura", "title_field", "", update_modified=False)

	for name in STALE_PROPERTY_SETTERS:
		if frappe.db.exists("Property Setter", name):
			frappe.delete_doc("Property Setter", name, force=1, ignore_permissions=True)

	if frappe.db.table_exists("Property Setter"):
		frappe.db.sql(
			"""
			DELETE FROM `tabProperty Setter`
			WHERE doc_type IN (%s, 'eFactura')
				AND field_name IN (
					'reference_doctype', 'reference_name',
					'supplier_party_type', 'customer_party_type',
					'supplier_party', 'customer_party',
					'supplier', 'supplier_bank_account'
				)
			""",
			(SEF,),
		)

	if frappe.db.exists("Custom Field", "Purchase eFactura-custom_column_break_ndj1j"):
		frappe.delete_doc(
			"Custom Field",
			"Purchase eFactura-custom_column_break_ndj1j",
			force=1,
			ignore_permissions=True,
		)
