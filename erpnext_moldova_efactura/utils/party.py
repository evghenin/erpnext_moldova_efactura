"""Party / IDNO helpers for eFactura Buyer."""

from __future__ import annotations

import frappe


def find_supplier_by_idno(idno: str) -> str | None:
	"""Return Supplier name matching IDNO field from eFactura Settings, or None."""
	if not idno:
		return None

	idno = str(idno).strip()
	fieldname = frappe.db.get_single_value("eFactura Settings", "supplier_idno_field")
	if not fieldname:
		return None

	meta = frappe.get_meta("Supplier")
	if not meta.has_field(fieldname):
		return None

	return frappe.db.get_value("Supplier", {fieldname: idno}, "name")


def get_default_company() -> str | None:
	"""Best-effort default company for buyer sync."""
	user_default = frappe.defaults.get_user_default("Company")
	if user_default:
		return user_default
	companies = frappe.get_all("Company", pluck="name", limit=1)
	return companies[0] if companies else None
