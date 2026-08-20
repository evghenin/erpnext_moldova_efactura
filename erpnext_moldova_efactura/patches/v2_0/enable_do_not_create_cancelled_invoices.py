import frappe


def execute():
	"""Default Incoming option: do not create invoices already cancelled in SFS."""
	if not frappe.db.exists("DocType", "eFactura Settings"):
		return
	if not frappe.get_meta("eFactura Settings").has_field("do_not_create_cancelled_invoices"):
		return
	frappe.db.set_single_value("eFactura Settings", "do_not_create_cancelled_invoices", 1)
