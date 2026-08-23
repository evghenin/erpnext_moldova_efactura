# Copyright (c) 2025, Evgheni Nemerenco and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint


class TestEFacturaPermissions(FrappeTestCase):
	def _perm_roles(self, doctype):
		return {p.role: p for p in frappe.get_meta(doctype).permissions if not cint(p.permlevel)}

	def test_purchase_efactura_has_no_create(self):
		for p in frappe.get_meta("Purchase eFactura").permissions:
			self.assertFalse(cint(p.create), p.role)

	def test_sales_roles_cannot_access_purchase_efactura(self):
		roles = {p.role for p in frappe.get_meta("Purchase eFactura").permissions}
		for role in ("Sales User", "Sales Manager", "eFactura Sales User"):
			self.assertNotIn(role, roles)

	def test_accounts_user_sales_efactura_is_read_only(self):
		p = self._perm_roles("Sales eFactura")["Accounts User"]
		self.assertTrue(cint(p.read))
		self.assertFalse(cint(p.write))
		self.assertFalse(cint(p.create))
		self.assertFalse(cint(p.submit))

	def test_efactura_manager_covers_sales_and_purchase(self):
		sales = self._perm_roles("Sales eFactura")["eFactura Manager"]
		purchase = self._perm_roles("Purchase eFactura")["eFactura Manager"]
		self.assertTrue(cint(sales.write) and cint(sales.create) and cint(sales.submit))
		self.assertTrue(cint(purchase.write) and cint(purchase.submit))
		self.assertFalse(cint(purchase.create))

	def test_purchase_user_can_write_purchase_efactura(self):
		p = self._perm_roles("Purchase eFactura")["Purchase User"]
		self.assertTrue(cint(p.read) and cint(p.write) and cint(p.submit))
		self.assertFalse(cint(p.create))

	def test_settings_api_restricted_to_managers(self):
		api_roles = {
			p.role
			for p in frappe.get_meta("eFactura Settings").permissions
			if cint(p.permlevel) == 1 and cint(p.write)
		}
		self.assertEqual(api_roles, {"System Manager", "eFactura Manager", "Accounts Manager"})
