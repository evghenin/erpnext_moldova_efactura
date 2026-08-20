import frappe


def execute():
	"""Drop Customize Form overlay so Purchase eFactura layout comes from the DocType."""
	for name in (
		"Purchase eFactura-main-field_order",
		"Purchase eFactura-custom_column_break_ndj1j",
	):
		if frappe.db.exists("Property Setter", name):
			frappe.delete_doc("Property Setter", name, force=1, ignore_permissions=True)

	if frappe.db.exists("Custom Field", "Purchase eFactura-custom_column_break_ndj1j"):
		frappe.delete_doc(
			"Custom Field",
			"Purchase eFactura-custom_column_break_ndj1j",
			force=1,
			ignore_permissions=True,
		)

	frappe.clear_cache(doctype="Purchase eFactura")
