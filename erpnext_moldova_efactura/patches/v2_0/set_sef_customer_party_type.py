import frappe


def execute():
	"""Set Sales eFactura.customer_party_type from Type after the field rename."""
	if not frappe.db.table_exists("Sales eFactura"):
		return
	if not frappe.db.has_column("Sales eFactura", "customer_party_type"):
		return
	if frappe.db.has_column("Sales eFactura", "type"):
		frappe.db.sql(
			"""
			UPDATE `tabSales eFactura`
			SET customer_party_type = CASE
				WHEN type = 'Non-Transfer' THEN 'Supplier'
				ELSE 'Customer'
			END
			WHERE ifnull(customer_party_type, '') = ''
			"""
		)
		return
	frappe.db.sql(
		"""
		UPDATE `tabSales eFactura`
		SET customer_party_type = 'Customer'
		WHERE ifnull(customer_party_type, '') = ''
		"""
	)
