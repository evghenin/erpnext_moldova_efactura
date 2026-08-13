# Copyright (c) 2025, Evgheni Nemerenco and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class eFacturaSettings(Document):
	def validate(self):
		seen = set()
		for row in self.get("uom_map") or []:
			key = (row.supplier_uom or "").strip().lower()
			if not key:
				continue
			if key in seen:
				frappe.throw(_("Duplicate Supplier UOM in map: {0}").format(row.supplier_uom))
			seen.add(key)
