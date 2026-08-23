# Copyright (c) 2026, Evgheni Nemerenco and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class eFacturaSupplierItemMap(Document):
	def validate(self):
		if not self.supplier_item_code and not self.supplier_item_name:
			frappe.throw(_("Supplier Item Code or Supplier Item Name is required"))

		# SFS Code is unreliable and is often reused; the stable key is the name.
		name = (self.supplier_item_name or "").strip()
		if not name:
			return
		if frappe.db.exists(
			"eFactura Supplier Item Map",
			{
				"supplier": self.supplier,
				"supplier_item_name": name,
				"name": ["!=", self.name],
			},
		):
			frappe.throw(_("Item map already exists for this supplier item"))
