import frappe
from frappe.model.utils.rename_field import rename_field


def execute():
	"""Rename efactura_uom → supplier_uom on buyer item and UOM map."""
	_rename_if_needed("eFactura Buyer Item", "efactura_uom", "supplier_uom")
	_rename_if_needed("eFactura UOM Map", "efactura_uom", "supplier_uom")


def _rename_if_needed(doctype: str, old: str, new: str):
	if not frappe.db.table_exists(doctype):
		return
	if frappe.db.has_column(doctype, old) and not frappe.db.has_column(doctype, new):
		rename_field(doctype, old, new)
