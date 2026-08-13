import frappe

from erpnext_moldova_efactura.utils.buyer_status import compose_buyer_status


def on_update(doc, method=None):
	if not doc.get("efactura_buyer"):
		return
	if not frappe.db.exists("eFactura Buyer", doc.efactura_buyer):
		return

	ef_status = frappe.db.get_value("eFactura Buyer", doc.efactura_buyer, "ef_status")
	status = compose_buyer_status(ef_status, doc.name)
	frappe.db.set_value(
		"eFactura Buyer",
		doc.efactura_buyer,
		{"purchase_invoice": doc.name, "status": status},
		update_modified=False,
	)
