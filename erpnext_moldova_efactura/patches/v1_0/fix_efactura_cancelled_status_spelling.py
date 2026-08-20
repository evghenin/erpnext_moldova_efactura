import frappe


def execute():
	"""Align eFactura cancelled status with ERPNext spelling (Cancelled).

	Also covers rows previously rewritten to American 'Canceled' by an earlier patch.
	Does not touch 'Canceled by Supplier' (API status label).
	"""
	if frappe.db.exists("DocType", "eFactura"):
		doctype, table = "eFactura", "`tabeFactura`"
	elif frappe.db.exists("DocType", "Sales eFactura"):
		doctype, table = "Sales eFactura", "`tabSales eFactura`"
	else:
		return

	count = frappe.db.count(doctype, {"status": "Canceled"})
	if not count:
		return

	frappe.db.sql(
		f"""
		UPDATE {table}
		SET `status` = 'Cancelled'
		WHERE `status` = 'Canceled'
		"""
	)
	frappe.db.commit()
	frappe.logger().info(f"[eFactura] Renamed status Canceled → Cancelled on {count} record(s)")
