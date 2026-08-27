import frappe
from frappe.permissions import add_permission, update_permission_property


def execute():
	"""eFactura Sales User / Manager can read Purchase Receipt (return e-Factura path)."""
	for role in ("eFactura Sales User", "eFactura Manager"):
		if not frappe.db.exists("Role", role):
			continue
		exists = frappe.db.exists(
			"Custom DocPerm",
			{"parent": "Purchase Receipt", "role": role, "permlevel": 0},
		)
		if not exists:
			add_permission("Purchase Receipt", role, 0)
		for prop, value in (
			("read", 1),
			("select", 1),
			("write", 0),
			("create", 0),
			("delete", 0),
			("submit", 0),
			("cancel", 0),
			("amend", 0),
		):
			try:
				update_permission_property("Purchase Receipt", role, 0, prop, value)
			except Exception:
				frappe.log_error(title="grant_sales_user_pr_read", message=frappe.get_traceback())
