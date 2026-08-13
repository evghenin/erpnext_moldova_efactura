# Copyright (c) 2025, Evgheni Nemerenco and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class eFacturaSettings(Document):
	def validate(self):
		self._validate_company_settings()
		seen = set()
		for row in self.get("uom_map") or []:
			key = (row.supplier_uom or "").strip().lower()
			if not key:
				continue
			if key in seen:
				frappe.throw(_("Duplicate Supplier UOM in map: {0}").format(row.supplier_uom))
			seen.add(key)

	def _validate_company_settings(self):
		seen = set()
		for row in self.get("company_settings") or []:
			if not row.company:
				continue
			if row.company in seen:
				frappe.throw(_("Company {0} is listed more than once").format(row.company))
			seen.add(row.company)
			if row.buying_vat_account:
				acc = frappe.db.get_value(
					"Account", row.buying_vat_account, ["is_group", "company"], as_dict=True
				)
				if acc:
					if acc.is_group:
						frappe.throw(_("VAT Account (Purchase) must be a ledger account, not a group"))
					if acc.company and acc.company != row.company:
						frappe.throw(
							_("VAT Account {0} does not belong to company {1}").format(
								row.buying_vat_account, row.company
							)
						)
			if row.taxes_and_charges:
				tmpl_company = frappe.db.get_value(
					"Purchase Taxes and Charges Template", row.taxes_and_charges, "company"
				)
				if tmpl_company and tmpl_company != row.company:
					frappe.throw(
						_("Purchase Taxes and Charges Template {0} does not belong to company {1}").format(
							row.taxes_and_charges, row.company
						)
					)
