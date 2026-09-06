"""Preserve original values and existing allocations from the first PF schema."""

import frappe
from frappe.utils import flt


def execute():
	if not frappe.db.has_column("Purchase Factura Item", "source_qty"):
		return
	for name in frappe.get_all("Purchase Factura", filters={"f_currency": ["is", "not set"]}, pluck="name"):
		doc = frappe.get_doc("Purchase Factura", name)
		for row in doc.items:
			old = frappe.db.sql(
				"""select description, source_uom, source_qty, source_rate, vat_rate
				from `tabPurchase Factura Item` where name=%s""",
				row.name,
				as_dict=True,
			)[0]
			cf = flt(row.conversion_factor) or 1
			stock_qty = flt(row.qty) * cf
			values = {
				"supplier_item_name": old.description,
				"supplier_uom": old.source_uom,
				"f_qty": old.source_qty,
				"f_rate": old.source_rate,
				"f_vat_rate": old.vat_rate,
				"f_net_amount": row.net_amount,
				"f_vat_amount": row.vat_amount,
				"f_amount": row.amount,
				"f_rate_with_vat": flt(row.amount) / flt(old.source_qty) if flt(old.source_qty) else 0,
				"rate": old.source_rate,
				"rate_with_vat": flt(row.amount) / flt(old.source_qty) if flt(old.source_qty) else 0,
				"conversion_factor": cf,
			}
			if row.item_code:
				item = frappe.db.get_value("Item", row.item_code, ["stock_uom", "item_name"], as_dict=True)
				values.update(stock_uom=item.stock_uom, item_name=item.item_name, stock_qty=stock_qty)
				# Preserve the historical source-to-stock ratio, including manually mapped service units.
				values.update(
					f_uom=row.uom or item.stock_uom,
					f_conversion_factor=stock_qty / flt(old.source_qty) if flt(old.source_qty) else 1,
				)
			frappe.db.set_value(row.doctype, row.name, values, update_modified=False)
		frappe.db.set_value(
			doc.doctype,
			doc.name,
			{
				"f_currency": doc.currency,
				"f_conversion_rate": 1,
				"f_net_total": doc.net_total,
				"f_vat_total": doc.vat_total,
				"f_total": doc.total,
			},
			update_modified=False,
		)
