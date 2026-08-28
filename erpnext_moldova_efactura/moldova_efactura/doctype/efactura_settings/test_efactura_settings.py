# Copyright (c) 2025, Evgheni Nemerenco and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_moldova_efactura.moldova_efactura.doctype.efactura_settings.efactura_settings import (
	get_form_settings,
)


class TesteFacturaSettings(FrappeTestCase):
	def test_form_settings_excludes_sensitive_child_tables(self):
		settings = get_form_settings()

		self.assertEqual(
			set(settings),
			{
				"customer_idno_field",
				"supplier_idno_field",
				"fiscal_territory",
				"vat_included_in_rate",
			},
		)
		self.assertNotIn("company_api_accounts", settings)
		self.assertEqual(
			settings["customer_idno_field"],
			frappe.db.get_single_value("eFactura Settings", "customer_idno_field"),
		)
