# Copyright (c) 2026, Evgheni Nemerenco and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class eFacturaSupplierItemMap(Document):
	def validate(self):
		if not self.supplier_item_code and not self.supplier_item_name:
			frappe.throw(_("Supplier Item Code or Supplier Item Name is required"))

		filters = {"supplier": self.supplier, "name": ["!=", self.name]}
		if self.supplier_item_code:
			filters["supplier_item_code"] = self.supplier_item_code
		else:
			filters["supplier_item_name"] = self.supplier_item_name
			filters["supplier_item_code"] = ["in", ["", None]]

		if frappe.db.exists("eFactura Supplier Item Map", filters):
			frappe.throw(_("Item map already exists for this supplier item"))
