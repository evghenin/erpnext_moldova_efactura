"""Pick Item Tax Template for Sales eFactura lines loaded from SFS."""

from __future__ import annotations

import frappe
from frappe.utils import flt


def item_tax_template_for_vat_rate(vat_rate, company: str | None = None) -> str | None:
	"""First allowed outgoing template whose VAT % matches the e-Factura line."""
	names = _allowed_item_tax_templates()
	if not names:
		return None
	want = flt(vat_rate, 2)
	for name in names:
		rate, tpl_company, disabled = _template_vat_info(name)
		if disabled:
			continue
		if company and tpl_company and tpl_company != company:
			continue
		if flt(rate, 2) == want:
			return name
	return None


def _allowed_item_tax_templates() -> list[str]:
	if not frappe.db.table_exists("eFactura Item Tax Template"):
		return []
	rows = frappe.get_all(
		"eFactura Item Tax Template",
		filters={"parent": "eFactura Settings", "parenttype": "eFactura Settings"},
		fields=["item_tax_template", "idx"],
		order_by="idx asc",
	)
	seen: set[str] = set()
	out: list[str] = []
	for row in rows:
		name = (row.item_tax_template or "").strip()
		if not name or name in seen:
			continue
		seen.add(name)
		out.append(name)
	return out


def _template_vat_info(name: str) -> tuple[float, str | None, bool]:
	if not frappe.db.exists("Item Tax Template", name):
		return 0.0, None, True
	meta = frappe.get_meta("Item Tax Template")
	fields = ["name"]
	if meta.has_field("company"):
		fields.append("company")
	if meta.has_field("disabled"):
		fields.append("disabled")
	doc = frappe.db.get_value("Item Tax Template", name, fields, as_dict=True) or {}
	disabled = bool(doc.get("disabled"))
	rate = 0.0
	taxes = frappe.get_all(
		"Item Tax Template Detail",
		filters={"parent": name},
		fields=["tax_rate"],
		order_by="idx asc",
		limit=1,
	)
	if taxes and taxes[0].tax_rate is not None:
		rate = flt(taxes[0].tax_rate)
	return rate, doc.get("company"), disabled
