# Copyright (c) 2026, Evgheni Nemerenco and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_moldova_efactura.utils.company_api import _unique_by_username, get_sync_targets


class TestCompanyAPI(FrappeTestCase):
	def test_unique_by_username_keeps_first_company(self):
		rows = [
			{"company": "A", "username": "u1"},
			{"company": "B", "username": "u1"},
			{"company": "C", "username": "u2"},
		]
		out = _unique_by_username(rows)
		self.assertEqual([r["company"] for r in out], ["A", "C"])

	def test_empty_table_uses_default_company(self):
		settings = frappe.get_single("eFactura Settings")
		if not (settings.api_username and settings.api_password):
			self.skipTest("Global API credentials are not set")
		prev = list(settings.get("company_api_accounts") or [])
		settings.set("company_api_accounts", [])
		settings.flags.ignore_permissions = True
		settings.save()
		try:
			targets = get_sync_targets()
			self.assertEqual(len(targets), 1)
			self.assertTrue(targets[0]["company"])
			self.assertEqual(targets[0]["username"], settings.api_username)
		finally:
			settings.reload()
			settings.set("company_api_accounts", prev)
			settings.flags.ignore_permissions = True
			settings.save()

	def test_table_rows_become_sync_targets(self):
		companies = frappe.get_all("Company", pluck="name", limit=2)
		if len(companies) < 2:
			self.skipTest("Need two companies")
		settings = frappe.get_single("eFactura Settings")
		if not (settings.api_username and settings.api_password):
			self.skipTest("Global API credentials are not set")
		prev = list(settings.get("company_api_accounts") or [])
		settings.set(
			"company_api_accounts",
			[
				{"company": companies[0], "api_username": "user-a", "api_password": "pass-a"},
				{"company": companies[1], "api_username": "user-b", "api_password": "pass-b"},
			],
		)
		settings.flags.ignore_permissions = True
		settings.save()
		try:
			targets = get_sync_targets()
			self.assertEqual({t["company"] for t in targets}, set(companies[:2]))
			by_company = {t["company"]: t["username"] for t in targets}
			self.assertEqual(by_company[companies[0]], "user-a")
			self.assertEqual(by_company[companies[1]], "user-b")
			one = get_sync_targets(company=companies[1])
			self.assertEqual(len(one), 1)
			self.assertEqual(one[0]["company"], companies[1])
			self.assertEqual(one[0]["username"], "user-b")
		finally:
			settings.reload()
			settings.set("company_api_accounts", prev)
			settings.flags.ignore_permissions = True
			settings.save()
