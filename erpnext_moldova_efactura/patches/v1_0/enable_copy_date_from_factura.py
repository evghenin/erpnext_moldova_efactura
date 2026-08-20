import frappe


def execute():
	"""Default Incoming option: copy PI posting date/time from e-Factura issue date."""
	if not frappe.db.exists("DocType", "eFactura Settings"):
		return
	if not frappe.get_meta("eFactura Settings").has_field("copy_date_from_factura"):
		return
	frappe.db.set_single_value("eFactura Settings", "copy_date_from_factura", 1)
