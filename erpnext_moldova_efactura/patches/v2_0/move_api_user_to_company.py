"""Move leftover site-wide e-Factura API user onto Company API Accounts."""

import frappe

from erpnext_moldova_efactura.utils.party import get_default_company


def execute():
	username = _legacy_single("api_username")
	password = _legacy_single("api_password")
	if username and password:
		_copy_to_company_accounts(username, password)

	frappe.db.sql(
		"DELETE FROM `tabSingles` WHERE doctype=%s AND field IN (%s, %s)",
		("eFactura Settings", "api_username", "api_password"),
	)


def _legacy_single(field: str) -> str:
	rows = frappe.db.sql(
		"SELECT `value` FROM `tabSingles` WHERE `doctype`=%s AND `field`=%s",
		("eFactura Settings", field),
	)
	if not rows or rows[0][0] is None:
		return ""
	return str(rows[0][0]).strip()


def _copy_to_company_accounts(username: str, password: str) -> None:
	rows = frappe.get_all(
		"eFactura Company API",
		fields=["name", "company", "api_username", "api_password"],
		order_by="idx",
	)
	if not rows:
		company = get_default_company()
		if not company:
			return
		settings = frappe.get_single("eFactura Settings")
		settings.append(
			"company_api_accounts",
			{
				"company": company,
				"api_username": username,
				"api_password": password,
			},
		)
		settings.flags.ignore_permissions = True
		settings.save()
		return

	taken = {(row.api_username or "").strip() for row in rows if (row.api_username or "").strip()}
	for row in rows:
		row_user = (row.api_username or "").strip()
		row_pass = (row.api_password or "").strip()
		if row_user and not row_pass:
			frappe.db.set_value(
				"eFactura Company API",
				row.name,
				"api_password",
				password,
				update_modified=False,
			)
		elif not row_user and not row_pass and username not in taken:
			frappe.db.set_value(
				"eFactura Company API",
				row.name,
				{"api_username": username, "api_password": password},
				update_modified=False,
			)
			taken.add(username)
