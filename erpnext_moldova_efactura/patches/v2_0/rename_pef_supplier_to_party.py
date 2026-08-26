import frappe
from frappe.model.utils.rename_field import rename_field


def execute():
	"""Rename Purchase eFactura.supplier → supplier_party before JSON sync."""
	if not frappe.db.table_exists("Purchase eFactura"):
		return
	if frappe.db.has_column("Purchase eFactura", "supplier") and not frappe.db.has_column(
		"Purchase eFactura", "supplier_party"
	):
		rename_field("Purchase eFactura", "supplier", "supplier_party")
