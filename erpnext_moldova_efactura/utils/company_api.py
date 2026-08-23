"""Resolve e-Factura SOAP credentials per Company."""

from __future__ import annotations

import frappe
from frappe import _

from erpnext_moldova_efactura.utils.party import get_default_company


def resolve_api_credentials(company: str | None = None) -> dict:
	"""Return wsdl/username/password, using a company row when set."""
	settings = frappe.get_single("eFactura Settings")
	wsdl_url = getattr(settings, "api_wsdl_url", None) or getattr(settings, "api_url", None)
	username = (getattr(settings, "api_username", None) or "").strip()
	password = (getattr(settings, "api_password", None) or "").strip()

	if company:
		for row in settings.get("company_api_accounts") or []:
			if row.company != company:
				continue
			username = (row.api_username or "").strip() or username
			password = (row.api_password or "").strip() or password
			break

	if not wsdl_url:
		frappe.throw(_("eFactura Settings: api_wsdl_url is not set."))
	if not username or not password:
		frappe.throw(_("eFactura Settings: API username/password are not set."))

	timeout = int(getattr(settings, "api_timeout_seconds", 20) or 20)
	verify_tls = bool(getattr(settings, "api_verify_tls", 1))
	return {
		"wsdl_url": wsdl_url,
		"username": username,
		"password": password,
		"timeout": timeout,
		"verify_tls": verify_tls,
		"service_name": getattr(settings, "api_service_name", None),
		"port_name": getattr(settings, "api_port_name", None),
	}


def get_sync_targets(company: str | None = None) -> list[dict]:
	"""Companies to poll in Fetch / daily sync, each with resolved API credentials.

	Empty Company API table → one target (default Company + global API user).
	Rows in the table → only those companies (global user used when a row has blank username).
	"""
	settings = frappe.get_single("eFactura Settings")
	wsdl_url = getattr(settings, "api_wsdl_url", None) or getattr(settings, "api_url", None)
	global_user = (getattr(settings, "api_username", None) or "").strip()
	global_pass = (getattr(settings, "api_password", None) or "").strip()

	rows: list[dict] = []
	seen_companies: set[str] = set()
	for row in settings.get("company_api_accounts") or []:
		if not row.company or row.company in seen_companies:
			continue
		username = (row.api_username or "").strip() or global_user
		password = (row.api_password or "").strip() or global_pass
		if not username or not password:
			continue
		seen_companies.add(row.company)
		rows.append(
			{
				"company": row.company,
				"username": username,
				"password": password,
				"wsdl_url": wsdl_url,
			}
		)

	if company:
		for target in rows:
			if target["company"] == company:
				return [target]
		if not global_user or not global_pass:
			frappe.throw(_("eFactura Settings: API username/password are not set."))
		return [
			{
				"company": company,
				"username": global_user,
				"password": global_pass,
				"wsdl_url": wsdl_url,
			}
		]

	if rows:
		return _unique_by_username(rows)

	default = get_default_company()
	if not default:
		frappe.throw(_("No Company found for eFactura sync"))
	if not global_user or not global_pass:
		frappe.throw(_("eFactura Settings: API username/password are not set."))
	return [
		{
			"company": default,
			"username": global_user,
			"password": global_pass,
			"wsdl_url": wsdl_url,
		}
	]


def _unique_by_username(rows: list[dict]) -> list[dict]:
	"""One SearchInvoices per SFS account; keep the first company for a username."""
	out: list[dict] = []
	seen: set[str] = set()
	for row in rows:
		key = row["username"]
		if key in seen:
			continue
		seen.add(key)
		out.append(row)
	return out
