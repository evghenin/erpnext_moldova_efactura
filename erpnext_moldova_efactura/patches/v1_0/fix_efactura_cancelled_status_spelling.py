import frappe


def execute():
	"""Align eFactura cancelled status with ERPNext spelling (Cancelled).

	Also covers rows previously rewritten to American 'Canceled' by an earlier patch.
	Does not touch 'Canceled by Supplier' (API status label).
	"""
	if not frappe.db.exists("DocType", "eFactura"):
		return

	count = frappe.db.count("eFactura", {"status": "Canceled"})
	if not count:
		return

	frappe.db.sql(
		"""
		UPDATE `tabeFactura`
		SET `status` = 'Cancelled'
		WHERE `status` = 'Canceled'
		"""
	)
	frappe.db.commit()
	frappe.logger().info(f"[eFactura] Renamed status Canceled → Cancelled on {count} record(s)")
