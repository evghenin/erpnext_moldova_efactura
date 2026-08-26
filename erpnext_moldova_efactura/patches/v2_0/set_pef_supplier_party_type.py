import frappe


def execute():
	"""Default existing PEF rows to Supplier party type after the field rename."""
	if not frappe.db.table_exists("Purchase eFactura"):
		return
	if not frappe.db.has_column("Purchase eFactura", "supplier_party_type"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabPurchase eFactura`
		SET supplier_party_type = 'Supplier'
		WHERE ifnull(supplier_party_type, '') = ''
		"""
	)
