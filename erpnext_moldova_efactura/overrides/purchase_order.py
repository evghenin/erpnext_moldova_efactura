import frappe


def on_update(doc, method=None):
	if not doc.get("efactura_buyer"):
		return
	if not frappe.db.exists("eFactura Buyer", doc.efactura_buyer):
		return
	if frappe.db.get_value("eFactura Buyer", doc.efactura_buyer, "purchase_order") == doc.name:
		return

	frappe.db.set_value(
		"eFactura Buyer",
		doc.efactura_buyer,
		"purchase_order",
		doc.name,
		update_modified=False,
	)
