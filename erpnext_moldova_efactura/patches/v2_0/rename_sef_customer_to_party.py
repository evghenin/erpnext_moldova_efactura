import frappe
from frappe.model.utils.rename_field import rename_field


def execute():
	"""Rename Sales eFactura.customer → customer_party before JSON sync."""
	if not frappe.db.table_exists("Sales eFactura"):
		return
	if frappe.db.has_column("Sales eFactura", "customer") and not frappe.db.has_column(
		"Sales eFactura", "customer_party"
	):
		rename_field("Sales eFactura", "customer", "customer_party")
