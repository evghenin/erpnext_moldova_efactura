"""VAT on Purchase Order / Purchase Invoice created from Purchase eFactura."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt


def get_company_buying_tax(company: str | None) -> dict:
	empty = {"tax_category": None, "taxes_and_charges": None, "buying_vat_account": None}
	if not company:
		return empty
	row = frappe.db.get_value(
		"eFactura Company Setting",
		{"parent": "eFactura Settings", "parenttype": "eFactura Settings", "company": company},
		["tax_category", "taxes_and_charges", "buying_vat_account"],
		as_dict=True,
	)
	return row or empty


def apply_buying_taxes(target, source) -> None:
	"""Fill taxes on a new PO/PI.

	1) Tax Category from Company Settings (helps Item Tax Template).
	2) Item-wise tax if Accounts Settings adds taxes from Item Tax Template
	   and every VAT line resolves a template.
	3) Else Purchase Taxes and Charges Template from Company Settings.
	4) If VAT Account is set and not already in taxes, add it with the
	   e-Factura VAT amount (Actual) or rate (On Net Total if VAT is included).
	5) Set included_in_print_rate on the VAT row from eFactura Settings.
	"""
	if not target.meta.get_field("taxes"):
		return

	if target.meta.has_field("posting_date") and not target.get("posting_date"):
		from frappe.utils import today

		target.posting_date = today()
	if target.meta.has_field("conversion_rate") and not flt(target.get("conversion_rate")):
		target.conversion_rate = 1

	cfg = get_company_buying_tax(target.company)
	if cfg.get("tax_category") and target.meta.has_field("tax_category"):
		target.tax_category = cfg["tax_category"]

	vat_included = bool(frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate"))
	vat_account = cfg.get("buying_vat_account")
	if not _apply_itemwise_taxes(target, source, vat_included, vat_account):
		_clear_item_tax_fields(target)
		_apply_purchase_tax_template(
			target, source, cfg.get("taxes_and_charges"), vat_included, vat_account
		)

	_ensure_actual_vat_row(target, source, vat_account, vat_included)


def _apply_itemwise_taxes(target, source, vat_included: bool, vat_account: str | None) -> bool:
	if not frappe.db.get_single_value("Accounts Settings", "add_taxes_from_item_tax_template"):
		return False

	from erpnext.controllers.accounts_controller import add_taxes_from_tax_template
	from erpnext.stock.get_item_details import get_item_tax_map

	vat_lines = 0
	for item, buyer_row in zip(target.items or [], source.items or []):
		if not item.item_code:
			continue
		template = _resolve_item_tax_template(target, item)
		if template and item.meta.has_field("item_tax_template"):
			item.item_tax_template = template
			if item.meta.has_field("item_tax_rate"):
				item.item_tax_rate = get_item_tax_map(target.company, template, as_json=True)
		if _buyer_row_has_vat(buyer_row):
			vat_lines += 1
			if not template:
				return False

	if not vat_lines:
		return False

	for item in target.items or []:
		add_taxes_from_tax_template(item, target, db_insert=False)

	if not target.get("taxes"):
		return False

	_set_included_in_print_rate(target, source, vat_included, vat_account)
	_calculate(target)
	return True


def _apply_purchase_tax_template(
	target, source, template: str | None, vat_included: bool, vat_account: str | None
) -> bool:
	if not template:
		return False

	from erpnext.controllers.accounts_controller import get_taxes_and_charges

	if target.meta.has_field("taxes_and_charges"):
		target.taxes_and_charges = template
	rows = get_taxes_and_charges("Purchase Taxes and Charges Template", template) or []
	if not rows:
		return False
	for tax in rows:
		target.append("taxes", tax)
	_set_included_in_print_rate(target, source, vat_included, vat_account)
	_calculate(target)
	return True


def _set_included_in_print_rate(target, source, vat_included: bool, vat_account: str | None) -> None:
	"""Set included_in_print_rate on VAT rows from eFactura Settings.

	Actual cannot be inclusive: if VAT is included in rate, switch that row to On Net Total.
	"""
	vat_rate = _primary_vat_rate(source) if vat_included else 0
	for tax in target.taxes or []:
		if not tax.meta.has_field("included_in_print_rate"):
			continue
		if vat_account and tax.account_head != vat_account:
			continue
		if tax.charge_type == "Actual" and vat_included and flt(vat_rate):
			tax.charge_type = "On Net Total"
			if tax.meta.has_field("rate"):
				tax.rate = flt(vat_rate)
			tax.included_in_print_rate = 1
			continue
		if tax.charge_type == "Actual":
			tax.included_in_print_rate = 0
			continue
		tax.included_in_print_rate = 1 if vat_included else 0


def _resolve_item_tax_template(target, item) -> str | None:
	from erpnext.stock.get_item_details import get_item_tax_template

	item_doc = frappe.get_cached_doc("Item", item.item_code)
	args = frappe._dict(
		{
			"item_code": item.item_code,
			"company": target.company,
			"tax_category": target.get("tax_category"),
			"bill_date": target.get("bill_date"),
			"posting_date": target.get("posting_date") or target.get("transaction_date"),
			"transaction_date": target.get("transaction_date") or target.get("posting_date"),
			"child_doctype": item.doctype,
		}
	)
	out: dict = {}
	return get_item_tax_template(args, item=item_doc, out=out) or None


def _ensure_actual_vat_row(target, source, vat_account: str | None, vat_included: bool) -> None:
	"""Add VAT Account as Actual when template/item-wise did not already include it."""
	vat_total = flt(source.vat_total)
	if vat_total <= 0:
		return

	if vat_account and any((t.account_head or "") == vat_account for t in (target.taxes or [])):
		return

	if not vat_account:
		if not target.get("taxes"):
			frappe.msgprint(
				_(
					"e-Factura VAT is {0} but Purchase taxes are empty. "
					"For company {1} set VAT Account (Purchase) or Purchase Taxes and Charges Template "
					"in eFactura Settings (Incoming → Company Settings)."
				).format(
					frappe.format_value(vat_total, {"fieldtype": "Currency", "options": source.currency}),
					target.company,
				),
				title=_("Purchase taxes are empty"),
				indicator="orange",
			)
		return

	vat_rate = _primary_vat_rate(source)
	cost_center = frappe.get_cached_value("Company", target.company, "cost_center")
	# Actual cannot be included in rate; use On Net Total so included_in_print_rate can follow settings.
	if vat_included and flt(vat_rate):
		_append_vat_tax(
			target,
			account=vat_account,
			description=_vat_label(vat_rate),
			charge_type="On Net Total",
			rate=vat_rate,
			included_in_print_rate=1,
			cost_center=cost_center,
		)
	else:
		_append_vat_tax(
			target,
			account=vat_account,
			description=_vat_label(vat_rate),
			charge_type="Actual",
			amount=vat_total,
			included_in_print_rate=0,
			cost_center=cost_center,
		)
	_set_included_in_print_rate(target, source, vat_included, vat_account)
	_calculate(target)


def _primary_vat_rate(source) -> float:
	for row in source.items or []:
		if flt(row.vat_amount) > 0 and flt(row.ef_vat_rate):
			return flt(row.ef_vat_rate)
	net = flt(getattr(source, "net_total", 0))
	vat = flt(getattr(source, "vat_total", 0))
	if net and vat:
		return flt(vat / net * 100)
	return 0.0


def _append_vat_tax(
	target,
	account: str,
	description: str,
	charge_type: str,
	cost_center=None,
	rate: float = 0,
	amount: float = 0,
	included_in_print_rate: int = 0,
):
	tax = target.append("taxes", {})
	tax.charge_type = charge_type
	tax.account_head = account
	tax.description = description
	if charge_type == "Actual":
		tax.tax_amount = flt(amount)
		if tax.meta.has_field("rate"):
			tax.rate = 0
	else:
		tax.rate = flt(rate)
	if tax.meta.has_field("included_in_print_rate"):
		tax.included_in_print_rate = 1 if included_in_print_rate else 0
	if tax.meta.has_field("category"):
		tax.category = "Total"
	if tax.meta.has_field("add_deduct_tax"):
		tax.add_deduct_tax = "Add"
	if cost_center and tax.meta.has_field("cost_center") and not tax.cost_center:
		tax.cost_center = cost_center


def _vat_label(rate: float) -> str:
	if flt(rate):
		return _("VAT {0}%").format(flt(rate, 2))
	return _("VAT")


def _buyer_row_has_vat(row) -> bool:
	return flt(getattr(row, "vat_amount", 0)) > 0 or flt(getattr(row, "ef_vat_rate", 0)) > 0


def _clear_item_tax_fields(target) -> None:
	for item in target.items or []:
		if item.meta.has_field("item_tax_template"):
			item.item_tax_template = None
		if item.meta.has_field("item_tax_rate"):
			item.item_tax_rate = None
	target.set("taxes", [])


def _calculate(target) -> None:
	if hasattr(target, "calculate_taxes_and_totals"):
		target.calculate_taxes_and_totals()
