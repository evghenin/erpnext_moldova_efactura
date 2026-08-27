"""Recompute fiscal_status on submitted return Purchase Invoices.

Return PI qty is negative; coverage used to treat need <= 0 as Pending even when a
submitted Purchase eFactura was already linked. sync uses db_set, so submitted
documents are not saved or amended.
"""

import frappe


def execute():
	if not frappe.db.has_column("Purchase Invoice", "fiscal_status"):
		return
	if not frappe.db.has_column("Purchase Invoice", "is_return"):
		return

	from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status

	names = frappe.get_all(
		"Purchase Invoice",
		filters={"docstatus": 1, "is_return": 1},
		pluck="name",
	)
	for name in names:
		sync_pi_fiscal_status(name)
