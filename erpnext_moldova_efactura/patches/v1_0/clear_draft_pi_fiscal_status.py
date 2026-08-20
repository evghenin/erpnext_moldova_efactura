import frappe


def execute():
	"""Draft / cancelled Purchase Invoices must not keep a fiscalization status."""
	if not frappe.db.has_column("Purchase Invoice", "fiscal_status"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabPurchase Invoice`
		SET fiscal_status = ''
		WHERE docstatus != 1
			AND IFNULL(fiscal_status, '') != ''
		"""
	)
