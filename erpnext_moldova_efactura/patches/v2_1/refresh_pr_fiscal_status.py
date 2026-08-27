"""Recompute Purchase Receipt fiscal_status from PEF / PI / SEF / SI links."""

import frappe


def execute():
	if not frappe.db.has_column("Purchase Receipt", "fiscal_status"):
		return

	from erpnext_moldova_efactura.utils.fiscal_status import sync_pr_fiscal_status

	names = frappe.get_all(
		"Purchase Receipt",
		filters={"docstatus": 1},
		pluck="name",
	)
	for name in names:
		sync_pr_fiscal_status(name)
