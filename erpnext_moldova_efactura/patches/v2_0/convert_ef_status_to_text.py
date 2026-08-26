import frappe

from erpnext_moldova_efactura.utils.buyer_status import BUYER_STATUS_MAP
from erpnext_moldova_efactura.utils.fiscal_status import SEF_EF_STATUS_LABELS


def execute():
	"""Store SFS workflow as text on ef_status; keep Status as Draft/Submitted/Cancelled/Return."""
	_convert_doctype("Sales eFactura", SEF_EF_STATUS_LABELS)
	_convert_doctype("Purchase eFactura", BUYER_STATUS_MAP)
	_rewrite_saved_filters()


def _convert_doctype(doctype: str, labels: dict):
	if not frappe.db.table_exists(doctype):
		return
	columns = frappe.db.get_table_columns(doctype)
	if "ef_status" not in columns:
		return

	label_values = tuple(dict.fromkeys(labels.values()))
	labels_in = ", ".join(frappe.db.escape(label) for label in label_values)
	code_case = " ".join(
		f"WHEN {frappe.db.escape(str(code))} THEN {frappe.db.escape(label)}"
		for code, label in labels.items()
	)
	has_efactura = "efactura_status" in columns
	prefer = (
		"WHEN ifnull(efactura_status, '') != '' THEN efactura_status" if has_efactura else ""
	)

	frappe.db.sql(
		f"""
		UPDATE `tab{doctype}`
		SET ef_status = CASE
			WHEN ef_status IN ({labels_in}) THEN ef_status
			{prefer}
			WHEN status IN ({labels_in}) THEN status
			ELSE CASE CAST(ef_status AS CHAR) {code_case} ELSE ef_status END
		END
		"""
	)

	has_return = frappe.db.has_column(doctype, "is_return")
	return_sql = "WHEN docstatus = 1 AND ifnull(is_return, 0) = 1 THEN 'Return'" if has_return else ""
	frappe.db.sql(
		f"""
		UPDATE `tab{doctype}`
		SET status = CASE
			WHEN docstatus = 0 THEN 'Draft'
			WHEN docstatus = 2 THEN 'Cancelled'
			{return_sql}
			WHEN docstatus = 1 THEN 'Submitted'
			ELSE status
		END
		"""
	)

	if has_efactura:
		frappe.db.sql_ddl(f"ALTER TABLE `tab{doctype}` DROP COLUMN `efactura_status`")


def _rewrite_saved_filters():
	replacements = (
		('"efactura_status"', '"ef_status"'),
		('"ef_status", "in", [1, 7, 9]', '"ef_status", "in", ["Signed by Supplier", "Sent to Buyer"]'),
	)
	_replace_column("Number Card", "filters_json", replacements)
	_replace_column("Dashboard Chart", "filters_json", replacements)
	if frappe.db.table_exists("Dashboard Chart") and frappe.db.has_column(
		"Dashboard Chart", "group_by_based_on"
	):
		frappe.db.sql(
			"""
			UPDATE `tabDashboard Chart`
			SET group_by_based_on = 'ef_status'
			WHERE document_type IN ('Sales eFactura', 'Purchase eFactura')
				AND group_by_based_on = 'status'
			"""
		)
	_replace_column("List View Settings", "fields", replacements)
	if frappe.db.table_exists("Workspace Shortcut") and frappe.db.has_column(
		"Workspace Shortcut", "stats_filter"
	):
		_replace_column("Workspace Shortcut", "stats_filter", replacements)


def _replace_column(doctype: str, column: str, replacements: tuple, extra_where: str = ""):
	if not frappe.db.table_exists(doctype) or not frappe.db.has_column(doctype, column):
		return
	where = extra_where or "1=1"
	for old, new in replacements:
		frappe.db.sql(
			f"""
			UPDATE `tab{doctype}`
			SET `{column}` = REPLACE(`{column}`, %s, %s)
			WHERE {where}
				AND IFNULL(`{column}`, '') LIKE %s
			""",
			(old, new, f"%{old}%"),
		)
