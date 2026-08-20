# Copyright (c) 2025, Evgheni Nemerenco and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt


class TestSaleseFactura(FrappeTestCase):
	def test_apply_vat_zero_rate_includes_line_in_totals(self):
		doc = frappe.get_doc(
			{
				"doctype": "Sales eFactura",
				"ef_conversion_rate": 1,
				"items": [
					{
						"item_code": "SKU00508",
						"item_name": "Set dentar ECO",
						"qty": 396,
						"rate": 6.8,
						"uom": "Nos",
						"ef_uom": "Nos",
					}
				],
			}
		)
		doc.apply_vat()
		row = doc.items[0]
		self.assertEqual(flt(row.amount, 2), 2692.8)
		self.assertEqual(flt(row.net_amount, 2), 2692.8)
		self.assertEqual(flt(row.vat_amount, 2), 0)
		self.assertEqual(flt(doc.net_total, 2), 2692.8)
		self.assertEqual(flt(doc.vat_total, 2), 0)
		self.assertEqual(flt(doc.total, 2), 2692.8)
		self.assertEqual(flt(doc.ef_net_total, 2), 2692.8)
		self.assertEqual(flt(doc.ef_vat_total, 2), 0)
		self.assertEqual(flt(doc.ef_total, 2), 2692.8)
