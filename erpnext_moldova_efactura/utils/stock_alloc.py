"""Purchase eFactura ↔ Purchase Receipt / Delivery Note line links."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_moldova_efactura.utils.pi_alloc import _child
from erpnext_moldova_efactura.utils.pi_match import (
	amount_close,
	buyer_line_name,
	buyer_row_qty,
	describe_line_mismatch,
	describe_unmapped_row,
	eq,
	fmt_money,
	fmt_qty,
	lines_compatible,
	money_precision,
	pi_line_name,
	price_matches,
	qty_matches,
	qty_precision,
	throw_unmapped_items,
	uom_matches,
)


@dataclass(frozen=True)
class StockLinkSpec:
	doctype: str
	link_field: str
	detail_field: str
	party_field: str
	label: str
	require_is_return: bool | None = None
	abs_qty: bool = True


PR_SPEC = StockLinkSpec(
	doctype="Purchase Receipt",
	link_field="purchase_receipt",
	detail_field="pr_detail",
	party_field="supplier",
	label="Purchase Receipt",
	require_is_return=False,
	abs_qty=True,
)

DN_SPEC = StockLinkSpec(
	doctype="Delivery Note",
	link_field="delivery_note",
	detail_field="dn_detail",
	party_field="customer",
	label="Delivery Note",
	require_is_return=True,
	abs_qty=True,
)


def _item_linked(row, spec: StockLinkSpec) -> bool:
	return bool(getattr(row, spec.link_field, None))


def has_stock_allocations(buyer, spec: StockLinkSpec) -> bool:
	return any(_item_linked(row, spec) for row in _child(buyer, "items"))


def unique_stock_docs(buyer, spec: StockLinkSpec) -> list[str]:
	names: list[str] = []
	seen: set[str] = set()
	for row in _child(buyer, "items"):
		name = getattr(row, spec.link_field, None)
		if name and name not in seen:
			seen.add(name)
			names.append(name)
	return names


def throw_unallocated_stock(items, heading: str, spec: StockLinkSpec, currency: str | None = None):
	rows = [row for row in (items or []) if not _item_linked(row, spec)]
	if not rows:
		return
	msgs = [describe_unmapped_row(row, currency) for row in rows]
	items_html = "".join(f"<li>{frappe.utils.cstr(m)}</li>" for m in msgs)
	frappe.throw(heading + f"<ul>{items_html}</ul>", title=_("Rows not allocated"))


def remaining_qty_for_item(buyer, item_row, spec: StockLinkSpec) -> float:
	if _item_linked(item_row, spec):
		return 0.0
	return buyer_row_qty(item_row)


def get_buyer_names_for_stock(doc_name: str, spec: StockLinkSpec) -> list[str]:
	if not doc_name or not frappe.db.has_column("Purchase eFactura Item", spec.link_field):
		return []
	names = frappe.get_all(
		"Purchase eFactura Item",
		filters={spec.link_field: doc_name, "parenttype": "Purchase eFactura"},
		pluck="parent",
	)
	out: list[str] = []
	seen: set[str] = set()
	for name in names:
		if name and name not in seen:
			seen.add(name)
			out.append(name)
	return out


def set_stock_buyer_link(doc_name: str, buyer_name: str, spec: StockLinkSpec) -> None:
	if not doc_name or not buyer_name:
		return
	if not frappe.get_meta(spec.doctype).has_field("purchase_efactura"):
		return
	current = frappe.db.get_value(spec.doctype, doc_name, "purchase_efactura")
	if current != buyer_name:
		frappe.db.set_value(
			spec.doctype, doc_name, "purchase_efactura", buyer_name, update_modified=False
		)


def clear_stock_buyer_link(doc_name: str, spec: StockLinkSpec) -> None:
	if not doc_name or not frappe.get_meta(spec.doctype).has_field("purchase_efactura"):
		return
	if frappe.db.get_value(spec.doctype, doc_name, "purchase_efactura"):
		frappe.db.set_value(spec.doctype, doc_name, "purchase_efactura", "", update_modified=False)


def validate_stock_allocation_qtys(buyer, spec: StockLinkSpec) -> None:
	seen_details: set[str] = set()
	for row in _child(buyer, "items"):
		if not _item_linked(row, spec):
			setattr(row, spec.detail_field, None)
			continue
		detail = getattr(row, spec.detail_field, None)
		if not detail:
			frappe.throw(
				_("e-Factura row {0} «{1}» is linked to {2} {3} without a row name").format(
					row.idx,
					buyer_line_name(row),
					_(spec.label),
					getattr(row, spec.link_field),
				)
			)
		if detail in seen_details:
			frappe.throw(_("{0} Item {1} is allocated more than once").format(_(spec.label), detail))
		seen_details.add(detail)


def _detail_taken(detail: str, buyer, spec: StockLinkSpec) -> bool:
	if not detail:
		return False
	for row in _child(buyer, "items"):
		if getattr(row, spec.detail_field, None) == detail:
			return True
	buyer_name = getattr(buyer, "name", None)
	if not buyer_name or not frappe.db.has_column("Purchase eFactura Item", spec.detail_field):
		return False
	other = frappe.db.get_value(
		"Purchase eFactura Item",
		{spec.detail_field: detail, "parent": ["!=", buyer_name]},
		"parent",
	)
	return bool(other)


def match_stock_to_remaining(buyer, target, spec: StockLinkSpec) -> tuple[list[dict], list[str]]:
	currency = buyer.currency or getattr(target, "currency", None) or "MDL"
	mprec = money_precision(currency)
	qprec = qty_precision()
	errors: list[str] = []
	allocs: list[dict] = []

	throw_unmapped_items(
		buyer.items or [],
		_("Map all items before linking a {0}").format(_(spec.label)),
		currency,
	)

	used_buyer: set[str] = set()
	for row in _child(buyer, "items"):
		if _item_linked(row, spec) and row.name:
			used_buyer.add(row.name)

	used_this: set[str] = set()
	for prow in target.items or []:
		detail = prow.name or f"{spec.link_field}-{prow.idx}"
		if prow.name and _detail_taken(prow.name, buyer, spec):
			errors.append(
				_("{0} row {1} «{2}» is already linked to an e-Factura").format(
					_(spec.label), prow.idx, pi_line_name(prow)
				)
			)
			continue

		candidate = None
		mismatch_row = None
		for brow in buyer.items or []:
			key = brow.name or f"idx-{brow.idx}"
			if key in used_buyer or key in used_this:
				continue
			if flt(remaining_qty_for_item(buyer, brow, spec), qprec) <= 0:
				continue
			if brow.item_code and prow.item_code and brow.item_code != prow.item_code:
				continue
			if lines_compatible(brow, prow, qprec, mprec, abs_qty=spec.abs_qty):
				candidate = brow
				break
			mismatch_row = brow

		if candidate is None:
			if mismatch_row is not None:
				errors.append(
					describe_line_mismatch(
						mismatch_row,
						prow,
						currency,
						qprec,
						mprec,
						abs_qty=spec.abs_qty,
						label=_(spec.label),
					)
				)
			else:
				qty = abs(flt(prow.qty)) if spec.abs_qty else flt(prow.qty)
				amount = abs(flt(prow.amount)) if spec.abs_qty else flt(prow.amount)
				errors.append(
					_(
						"{0} row {1} «{2}»: qty {3} × rate {4} {6} (amount {5}) — not found on e-Factura"
					).format(
						_(spec.label),
						prow.idx,
						pi_line_name(prow),
						fmt_qty(qty, qprec),
						fmt_money(prow.rate, mprec),
						fmt_money(amount, mprec),
						currency,
					)
				)
			continue

		key = candidate.name or f"idx-{candidate.idx}"
		used_this.add(key)
		allocs.append(
			{
				"buyer_row": candidate,
				"stock_row": prow,
				"qty": buyer_row_qty(candidate),
				"detail": detail,
			}
		)

	return allocs, errors


def is_full_stock_cover(buyer, allocs: list[dict], spec: StockLinkSpec) -> bool:
	if any(_item_linked(row, spec) for row in _child(buyer, "items")):
		return False
	if not allocs:
		return False
	qprec = qty_precision()
	used: dict[str, float] = defaultdict(float)
	for a in allocs:
		key = a["buyer_row"].name or f"idx-{a['buyer_row'].idx}"
		used[key] += flt(a["qty"])
	for row in buyer.items or []:
		key = row.name or f"idx-{row.idx}"
		if not eq(used.get(key) or 0, buyer_row_qty(row), qprec):
			return False
	return True


def collect_stock_total_errors(buyer, target, spec: StockLinkSpec, mprec: int, currency: str) -> list[str]:
	errors: list[str] = []
	tgt_total = abs(flt(getattr(target, "grand_total", None) or getattr(target, "total", None) or 0))
	if not (
		amount_close(buyer.total, tgt_total, mprec) or amount_close(buyer.net_total, tgt_total, mprec)
	):
		errors.append(
			_("Grand Total mismatch: e-Factura {0} {2}, {3} {1} {2}").format(
				fmt_money(buyer.total, mprec),
				fmt_money(tgt_total, mprec),
				currency,
				_(spec.label),
			)
		)
	taxes = abs(flt(getattr(target, "total_taxes_and_charges", None) or 0))
	if taxes and not amount_close(buyer.vat_total, taxes, mprec):
		errors.append(
			_("VAT Total mismatch: e-Factura {0} {2}, {3} {1} {2}").format(
				fmt_money(buyer.vat_total, mprec),
				fmt_money(taxes, mprec),
				currency,
				_(spec.label),
			)
		)
	return errors


def collect_stock_document_errors(buyer, target, spec: StockLinkSpec) -> list[str]:
	from erpnext_moldova_efactura.utils.pef_mode import pef_customer, pef_supplier

	errors: list[str] = []
	if cint(target.docstatus) == 2:
		errors.append(_("{0} {1} is cancelled").format(_(spec.label), target.name))
	if spec.require_is_return is True and not cint(target.is_return):
		errors.append(_("{0} {1} must be a return").format(_(spec.label), target.name))
	if spec.require_is_return is False and cint(getattr(target, "is_return", 0)):
		errors.append(_("{0} {1} must not be a return").format(_(spec.label), target.name))

	pef_party = pef_customer(buyer) if spec.party_field == "customer" else pef_supplier(buyer)
	tgt_party = getattr(target, spec.party_field, None)
	if pef_party and tgt_party and pef_party != tgt_party:
		errors.append(
			_("Party mismatch: e-Factura {0}, {1} {2}").format(pef_party, _(spec.label), tgt_party)
		)
	if buyer.company and target.company and buyer.company != target.company:
		errors.append(
			_("Company mismatch: e-Factura {0}, {1} {2}").format(
				buyer.company, _(spec.label), target.company
			)
		)
	other = None
	if getattr(target, "name", None) and frappe.db.has_column("Purchase eFactura Item", spec.link_field):
		other = frappe.db.get_value(
			"Purchase eFactura Item",
			{spec.link_field: target.name, "parent": ["!=", getattr(buyer, "name", "") or ""]},
			"parent",
		)
	if other:
		errors.append(
			_("{0} {1} is already linked to e-Factura {2}").format(_(spec.label), target.name, other)
		)
	return errors


def raise_stock_link_error(doc_name: str, spec: StockLinkSpec, errors: list[str], submit: bool = False):
	items = "".join(f"<li>{frappe.utils.cstr(e)}</li>" for e in errors)
	if submit:
		frappe.throw(
			_("Cannot submit {0} {1}:").format(_(spec.label), doc_name) + f"<ul>{items}</ul>",
			title=_("Cannot submit {0} {1}").format(_(spec.label), doc_name),
		)
	frappe.throw(
		_("Cannot link {0} {1}:").format(_(spec.label), doc_name) + f"<ul>{items}</ul>",
		title=_("e-Factura and {0} do not match").format(_(spec.label)),
	)


def validate_and_match_stock(buyer, target, spec: StockLinkSpec, submit: bool = False) -> list[dict]:
	errors = collect_stock_document_errors(buyer, target, spec)
	allocs, line_errors = match_stock_to_remaining(buyer, target, spec)
	errors.extend(line_errors)
	if is_full_stock_cover(buyer, allocs, spec):
		currency = buyer.currency or getattr(target, "currency", None) or "MDL"
		errors.extend(collect_stock_total_errors(buyer, target, spec, money_precision(currency), currency))
	if errors:
		raise_stock_link_error(target.name, spec, errors, submit=submit)
	if not allocs:
		raise_stock_link_error(target.name, spec, [_("No matching rows to allocate")], submit=submit)
	return allocs


def apply_stock_allocations(buyer, allocs: list[dict], doc_name: str, spec: StockLinkSpec) -> None:
	for a in allocs:
		row = a["buyer_row"]
		setattr(row, spec.link_field, doc_name)
		setattr(row, spec.detail_field, a.get("detail") or a["stock_row"].name)


def delete_allocations_for_stock(doc_name: str, spec: StockLinkSpec) -> list[str]:
	if not doc_name or not frappe.db.has_column("Purchase eFactura Item", spec.link_field):
		return []
	parents = get_buyer_names_for_stock(doc_name, spec)
	items = frappe.get_all(
		"Purchase eFactura Item",
		filters={spec.link_field: doc_name, "parenttype": "Purchase eFactura"},
		pluck="name",
	)
	for name in items:
		frappe.db.set_value(
			"Purchase eFactura Item",
			name,
			{spec.link_field: "", spec.detail_field: ""},
			update_modified=False,
		)
	return parents


def validate_existing_stock_allocations(buyer, target, spec: StockLinkSpec, submit: bool = True) -> None:
	linked = [r for r in (buyer.items or []) if getattr(r, spec.link_field, None) == target.name]
	if not linked:
		return
	currency = buyer.currency or getattr(target, "currency", None) or "MDL"
	mprec = money_precision(currency)
	qprec = qty_precision()
	errors: list[str] = []
	errors.extend(collect_stock_document_errors(buyer, target, spec))
	by_name = {r.name: r for r in (target.items or []) if r.name}
	for brow in linked:
		prow = by_name.get(getattr(brow, spec.detail_field, None))
		if not prow:
			errors.append(
				_("{0} Item {1} is missing on {2}").format(
					_(spec.label), getattr(brow, spec.detail_field, None), target.name
				)
			)
			continue
		if brow.item_code and prow.item_code and brow.item_code != prow.item_code:
			errors.append(
				_("e-Factura row {0} «{1}»: item {2} vs {3} {4}").format(
					brow.idx, buyer_line_name(brow), brow.item_code, _(spec.label), prow.item_code
				)
			)
		if not uom_matches(brow, prow):
			errors.append(
				_("e-Factura row {0} «{1}»: UOM {2} vs {3} {4}").format(
					brow.idx,
					buyer_line_name(brow),
					brow.uom or brow.ef_uom or _("empty"),
					_(spec.label),
					prow.uom or _("empty"),
				)
			)
		if not price_matches(brow, prow, mprec, abs_qty=spec.abs_qty):
			errors.append(
				_("e-Factura row {0} «{1}»: amounts do not match {2}").format(
					brow.idx, buyer_line_name(brow), _(spec.label)
				)
			)
		if not qty_matches(brow, prow, qprec, abs_qty=spec.abs_qty):
			errors.append(
				_("e-Factura row {0} «{1}»: quantity does not match {2}").format(
					brow.idx, buyer_line_name(brow), _(spec.label)
				)
			)
	if errors:
		raise_stock_link_error(target.name, spec, errors, submit=submit)
