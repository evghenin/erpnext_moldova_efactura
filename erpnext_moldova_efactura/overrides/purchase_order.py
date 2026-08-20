import frappe


@frappe.whitelist()
def make_purchase_invoice(source_name, target_doc=None, args=None):
	from erpnext.buying.doctype.purchase_order.purchase_order import (
		make_purchase_invoice as erpnext_make_purchase_invoice,
	)

	from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
		apply_factura_defaults_from_po,
	)

	pi = erpnext_make_purchase_invoice(source_name, target_doc, args)
	apply_factura_defaults_from_po(pi, source_name)
	return pi
