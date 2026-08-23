"""Drop stale (Draft) fiscal labels after the linked Purchase eFactura is submitted."""

import frappe


def execute():
	if not frappe.db.has_column("Purchase Invoice", "fiscal_status"):
		return

	from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status

	names = frappe.get_all(
		"Purchase Invoice",
		filters={"docstatus": 1, "fiscal_status": ["like", "%(Draft)%"]},
		pluck="name",
	)
	for name in names:
		sync_pi_fiscal_status(name)
