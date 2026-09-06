import frappe
from frappe import _


def protect_purchase_factura_original(doc, method=None):
	"""Keep evidence bytes referenced by any Purchase Factura, including cancelled records."""
	if not doc.file_url or not frappe.db.table_exists("Purchase Factura"):
		return
	factura = frappe.db.get_value("Purchase Factura", {"original_file": doc.file_url}, "name")
	if factura:
		frappe.throw(
			_("File is the preserved original for Purchase Factura {0} and cannot be deleted").format(factura)
		)
