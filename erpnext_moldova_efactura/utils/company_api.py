"""Resolve e-Factura SOAP credentials per Company."""

from __future__ import annotations

import frappe
from frappe import _


def resolve_api_credentials(company: str | None = None) -> dict:
	"""Return wsdl/username/password for one Company. No site-wide API user."""
	if not company:
		frappe.throw(_("e-Factura API credentials are set per Company. Pass a Company."))

	settings = frappe.get_single("eFactura Settings")
	wsdl_url = getattr(settings, "api_wsdl_url", None) or getattr(settings, "api_url", None)
	username = password = ""
	for row in settings.get("company_api_accounts") or []:
		if row.company != company:
			continue
		username = (row.api_username or "").strip()
		password = (row.api_password or "").strip()
		break

	if not wsdl_url:
		frappe.throw(_("eFactura Settings: API URL is not set."))
	if not username or not password:
		frappe.throw(
			_("Set API username and password for company {0} in eFactura Settings (Company API Accounts).").format(
				company
			)
		)

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
	"""Companies to poll in Fetch / daily sync. Only rows in Company API Accounts."""
	settings = frappe.get_single("eFactura Settings")
	wsdl_url = getattr(settings, "api_wsdl_url", None) or getattr(settings, "api_url", None)

	rows: list[dict] = []
	seen_companies: set[str] = set()
	for row in settings.get("company_api_accounts") or []:
		if not row.company or row.company in seen_companies:
			continue
		username = (row.api_username or "").strip()
		password = (row.api_password or "").strip()
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
		frappe.throw(
			_("Set API username and password for company {0} in eFactura Settings (Company API Accounts).").format(
				company
			)
		)

	if not rows:
		frappe.throw(
			_("Add at least one Company API Account with username and password in eFactura Settings.")
		)
	return _unique_by_username(rows)


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
