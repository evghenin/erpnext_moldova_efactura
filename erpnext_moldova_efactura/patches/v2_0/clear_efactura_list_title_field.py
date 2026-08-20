import frappe


def execute():
	"""List view first column is title_field; keep ID, not eFactura Number."""
	for doctype in ("Sales eFactura", "Purchase eFactura"):
		if frappe.db.exists("DocType", doctype):
			frappe.db.set_value("DocType", doctype, "title_field", "", update_modified=False)

	# Stale Customize Form leftovers from the old Buyer doctype
	for name in (
		"eFactura Buyer-naming_series-default",
		"eFactura Buyer-naming_series-options",
		"Purchase eFactura-ef_number-in_list_view",
		"Sales eFactura-ef_number-in_list_view",
	):
		if frappe.db.exists("Property Setter", name):
			frappe.delete_doc("Property Setter", name, force=1, ignore_permissions=True)

	frappe.clear_cache(doctype="Sales eFactura")
	frappe.clear_cache(doctype="Purchase eFactura")
