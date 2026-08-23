"""Purchase eFactura document currency vs eFactura (SFS) currency."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, today

# XML / eFactura money → document money (SEF convention: rate is doc → ef).
ITEM_EF_TO_DOC = (
	("ef_rate", "rate"),
	("ef_rate_with_vat", "rate_with_vat"),
	("ef_amount", "amount"),
	("ef_net_amount", "net_amount"),
	("ef_vat_amount", "vat_amount"),
)
HEADER_EF_TO_DOC = (
	("ef_net_total", "net_total"),
	("ef_vat_total", "vat_total"),
	("ef_total", "total"),
)
XML_ITEM_MONEY = {
	"rate": "ef_rate",
	"rate_with_vat": "ef_rate_with_vat",
	"amount": "ef_amount",
	"net_amount": "ef_net_amount",
	"vat_amount": "ef_vat_amount",
}


def system_default_currency() -> str:
	return (
		frappe.db.get_default("currency")
		or frappe.db.get_single_value("Global Defaults", "default_currency")
		or "MDL"
	)


def default_document_currency(supplier: str | None = None, company: str | None = None) -> str:
	"""Supplier default → company default → system default."""
	if supplier:
		cur = frappe.db.get_value("Supplier", supplier, "default_currency")
		if cur:
			return cur
	if company:
		cur = frappe.db.get_value("Company", company, "default_currency")
		if cur:
			return cur
	return system_default_currency()


def settings_ef_currency() -> str:
	cur = frappe.db.get_single_value("eFactura Settings", "currency")
	if not cur:
		frappe.throw(_("Please set Currency in eFactura Settings."))
	return cur


def apply_supplier_or_default_currency(doc, overwrite_company_default: bool = False) -> None:
	"""Set document currency on draft. Do not override a user-chosen currency."""
	if cint(getattr(doc, "docstatus", 0)):
		return
	if not doc.currency:
		doc.currency = default_document_currency(doc.supplier, doc.company)
		return
	if not overwrite_company_default or not doc.supplier:
		return
	sup_cur = frappe.db.get_value("Supplier", doc.supplier, "default_currency")
	if not sup_cur:
		return
	company_cur = (
		frappe.db.get_value("Company", doc.company, "default_currency") if doc.company else None
	)
	if doc.currency in (company_cur, system_default_currency()):
		doc.currency = sup_cur


def apply_ef_conversion_rate_rules(doc) -> None:
	if not doc.currency or not doc.ef_currency:
		return
	if doc.currency == doc.ef_currency:
		doc.ef_conversion_rate = 1
		return
	if flt(doc.ef_conversion_rate) > 0:
		return
	tx_date = doc.issue_date or today()
	from erpnext.setup.utils import get_exchange_rate

	rate = get_exchange_rate(doc.currency, doc.ef_currency, tx_date)
	if rate:
		doc.ef_conversion_rate = rate


def apply_document_amounts_from_ef(doc) -> None:
	"""Document amounts = eFactura amounts / (doc → ef rate)."""
	apply_ef_conversion_rate_rules(doc)
	has_ef = flt(getattr(doc, "ef_total", None))
	if not has_ef:
		has_ef = any(flt(getattr(row, "ef_amount", None)) for row in doc.get("items") or [])
	if not has_ef:
		return
	conv = flt(doc.ef_conversion_rate) or 1
	vat_included = cint(frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate") or 0)
	for row in getattr(doc, "items", None) or []:
		for ef_field, doc_field in ITEM_EF_TO_DOC:
			setattr(row, doc_field, flt(getattr(row, ef_field, None)) / conv)
		_apply_line_rate_amount_from_vat_setting(row, vat_included)
	for ef_field, doc_field in HEADER_EF_TO_DOC:
		setattr(doc, doc_field, flt(getattr(doc, ef_field, None)) / conv)


def _apply_line_rate_amount_from_vat_setting(row, vat_included: int) -> None:
	"""rate/amount follow eFactura Settings: VAT included in rate or not."""
	if vat_included:
		row.rate = flt(row.rate_with_vat) or flt(row.rate)
		gross = flt(row.net_amount) + flt(row.vat_amount)
		row.amount = gross or flt(row.amount)
	else:
		row.amount = flt(row.net_amount)


def remap_xml_item_money(item: dict) -> dict:
	payload = dict(item)
	for src, dst in XML_ITEM_MONEY.items():
		if src in payload:
			payload[dst] = payload.pop(src)
	return payload
