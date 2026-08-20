import frappe
from frappe.utils import flt

from erpnext_moldova_efactura.utils.uom_map import get_item_uom_conversion


def execute():
	"""Backfill PEF Item conversion factors from stored qty, then Item UOM."""
	if not frappe.db.exists("DocType", "Purchase eFactura Item"):
		return
	if not frappe.db.has_column("Purchase eFactura Item", "conversion_factor"):
		return
	if not frappe.db.has_column("Purchase eFactura Item", "ef_conversion_factor"):
		return

	rows = frappe.db.sql(
		"""
		SELECT name, item_code, uom, ef_uom, ef_qty, qty, stock_qty,
			conversion_factor, ef_conversion_factor
		FROM `tabPurchase eFactura Item`
		WHERE ifnull(conversion_factor, 0) = 0 OR ifnull(ef_conversion_factor, 0) = 0
		""",
		as_dict=True,
	)
	for row in rows:
		updates = {}
		if not flt(row.conversion_factor):
			if flt(row.qty) and flt(row.stock_qty):
				updates["conversion_factor"] = flt(row.stock_qty) / flt(row.qty)
			else:
				updates["conversion_factor"] = get_item_uom_conversion(row.item_code, row.uom) or 1
		if not flt(row.ef_conversion_factor):
			if flt(row.ef_qty) and flt(row.stock_qty):
				updates["ef_conversion_factor"] = flt(row.stock_qty) / flt(row.ef_qty)
			else:
				updates["ef_conversion_factor"] = get_item_uom_conversion(row.item_code, row.ef_uom) or 1
		if updates:
			frappe.db.set_value("Purchase eFactura Item", row.name, updates, update_modified=False)
