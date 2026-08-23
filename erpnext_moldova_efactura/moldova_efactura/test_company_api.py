# Copyright (c) 2026, Evgheni Nemerenco and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_moldova_efactura.utils.company_api import (
	_unique_by_username,
	get_sync_targets,
	resolve_api_credentials,
)


def _account_payload(settings):
	return [
		{
			"company": row.company,
			"api_username": row.api_username,
			"api_password": row.api_password,
		}
		for row in (settings.get("company_api_accounts") or [])
	]


class TestCompanyAPI(FrappeTestCase):
	def test_unique_by_username_keeps_first_company(self):
		rows = [
			{"company": "A", "username": "u1"},
			{"company": "B", "username": "u1"},
			{"company": "C", "username": "u2"},
		]
		out = _unique_by_username(rows)
		self.assertEqual([r["company"] for r in out], ["A", "C"])

	def test_empty_table_throws(self):
		settings = frappe.get_single("eFactura Settings")
		prev = _account_payload(settings)
		settings.set("company_api_accounts", [])
		settings.flags.ignore_permissions = True
		settings.save()
		try:
			with self.assertRaises(frappe.ValidationError):
				get_sync_targets()
			with self.assertRaises(frappe.ValidationError):
				resolve_api_credentials(None)
		finally:
			settings.reload()
			settings.set("company_api_accounts", prev)
			settings.flags.ignore_permissions = True
			settings.save()

	def test_table_rows_become_sync_targets(self):
		companies = frappe.get_all("Company", pluck="name", limit=2)
		if not companies:
			self.skipTest("Need a Company")
		settings = frappe.get_single("eFactura Settings")
		prev = _account_payload(settings)
		rows = [{"company": companies[0], "api_username": "user-a", "api_password": "pass-a"}]
		if len(companies) > 1:
			rows.append({"company": companies[1], "api_username": "user-b", "api_password": "pass-b"})
		settings.set("company_api_accounts", rows)
		settings.flags.ignore_permissions = True
		settings.save()
		try:
			targets = get_sync_targets()
			self.assertEqual({t["company"] for t in targets}, {r["company"] for r in rows})
			creds = resolve_api_credentials(companies[0])
			self.assertEqual(creds["username"], "user-a")
			self.assertEqual(creds["password"], "pass-a")
			if len(companies) > 1:
				one = get_sync_targets(company=companies[1])
				self.assertEqual(len(one), 1)
				self.assertEqual(one[0]["username"], "user-b")
			with self.assertRaises(frappe.ValidationError):
				resolve_api_credentials("Not A Real Company")
		finally:
			settings.reload()
			settings.set("company_api_accounts", prev)
			settings.flags.ignore_permissions = True
			settings.save()
