import frappe


def on_update(doc, method=None):
	if not doc.get("efactura_buyer"):
		return
	if not frappe.db.exists("eFactura Buyer", doc.efactura_buyer):
		return

	status = "Linked to PI"
	frappe.db.set_value(
		"eFactura Buyer",
		doc.efactura_buyer,
		{"purchase_invoice": doc.name, "status": status},
		update_modified=False,
	)
