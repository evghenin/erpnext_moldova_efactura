import frappe


def execute():
	"""Submitted returns show status Return in the list, like Sales Invoice."""
	for doctype in ("Purchase eFactura", "Sales eFactura"):
		if not frappe.db.table_exists(doctype):
			continue
		if not frappe.db.has_column(doctype, "is_return"):
			continue
		if not frappe.db.has_column(doctype, "status"):
			continue
		frappe.db.sql(
			f"""
			UPDATE `tab{doctype}`
			SET status = 'Return'
			WHERE docstatus = 1 AND ifnull(is_return, 0) = 1
			"""
		)
