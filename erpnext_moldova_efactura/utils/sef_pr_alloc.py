"""Sales eFactura ↔ Purchase Receipt Return line links (1:1)."""

from __future__ import annotations

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
from erpnext_moldova_efactura.utils.sef_mode import is_sef_return, sef_supplier
from erpnext_moldova_efactura.utils.stock_alloc import StockLinkSpec, throw_unallocated_stock

SEF_PR_SPEC = StockLinkSpec(
	doctype="Purchase Receipt",
	link_field="purchase_receipt",
	detail_field="pr_detail",
	party_field="supplier",
	label="Purchase Receipt Return",
	require_is_return=True,
	abs_qty=True,
)

SEF_ITEM = "Sales eFactura Item"


def _item_linked(row) -> bool:
	return bool(getattr(row, SEF_PR_SPEC.link_field, None))


def has_pr_allocations(sef) -> bool:
	return any(_item_linked(row) for row in _child(sef, "items"))


def unique_purchase_receipts(sef) -> list[str]:
	names: list[str] = []
	seen: set[str] = set()
	for row in _child(sef, "items"):
		name = getattr(row, SEF_PR_SPEC.link_field, None)
		if name and name not in seen:
			seen.add(name)
			names.append(name)
	return names


def get_sef_names_for_pr(pr_name: str) -> list[str]:
	if not pr_name or not frappe.db.has_column(SEF_ITEM, SEF_PR_SPEC.link_field):
		return []
	names = frappe.get_all(
		SEF_ITEM,
		filters={SEF_PR_SPEC.link_field: pr_name, "parenttype": "Sales eFactura"},
		pluck="parent",
	)
	out: list[str] = []
	seen: set[str] = set()
	for name in names:
		if name and name not in seen:
			seen.add(name)
			out.append(name)
	return out


def live_sef_for_pr(pr) -> str | None:
	"""Existing non-cancelled Sales eFactura bound to this PR (header or items)."""
	pr_name = getattr(pr, "name", None) or pr
	if not pr_name:
		return None
	header = None
	if isinstance(pr, str):
		if frappe.get_meta("Purchase Receipt").has_field("sales_efactura"):
			header = frappe.db.get_value("Purchase Receipt", pr_name, "sales_efactura")
	elif getattr(pr, "meta", None) and pr.meta.has_field("sales_efactura"):
		header = pr.get("sales_efactura")
	candidates = []
	if header:
		candidates.append(header)
	candidates.extend(get_sef_names_for_pr(pr_name))
	seen: set[str] = set()
	for name in candidates:
		if not name or name in seen:
			continue
		seen.add(name)
		status = frappe.db.get_value("Sales eFactura", name, "docstatus")
		if status is not None and cint(status) != 2:
			return name
	return None


def set_pr_sef_link(pr_name: str, sef_name: str) -> None:
	if not pr_name or not sef_name:
		return
	if not frappe.get_meta("Purchase Receipt").has_field("sales_efactura"):
		return
	current = frappe.db.get_value("Purchase Receipt", pr_name, "sales_efactura")
	if current != sef_name:
		frappe.db.set_value("Purchase Receipt", pr_name, "sales_efactura", sef_name, update_modified=False)


def clear_pr_sef_link(pr_name: str, sef_name: str | None = None) -> None:
	if not pr_name or not frappe.get_meta("Purchase Receipt").has_field("sales_efactura"):
		return
	current = frappe.db.get_value("Purchase Receipt", pr_name, "sales_efactura")
	if not current:
		return
	if sef_name and current != sef_name:
		return
	frappe.db.set_value("Purchase Receipt", pr_name, "sales_efactura", "", update_modified=False)


def throw_unallocated_pr(items, currency: str | None = None) -> None:
	throw_unallocated_stock(
		items,
		_("Allocate all rows to a Purchase Receipt Return before submit"),
		SEF_PR_SPEC,
		currency,
	)


def _detail_taken(detail: str, sef) -> bool:
	if not detail:
		return False
	for row in _child(sef, "items"):
		if getattr(row, SEF_PR_SPEC.detail_field, None) == detail:
			return True
	sef_name = getattr(sef, "name", None)
	if not sef_name or not frappe.db.has_column(SEF_ITEM, SEF_PR_SPEC.detail_field):
		return False
	other = frappe.db.get_value(
		SEF_ITEM,
		{SEF_PR_SPEC.detail_field: detail, "parent": ["!=", sef_name]},
		"parent",
	)
	if not other:
		return False
	status = frappe.db.get_value("Sales eFactura", other, "docstatus")
	return status is not None and cint(status) != 2


def collect_pr_document_errors(sef, pr) -> list[str]:
	errors: list[str] = []
	if cint(pr.docstatus) != 1:
		errors.append(_("Purchase Receipt {0} must be submitted").format(pr.name))
	if cint(pr.docstatus) == 2:
		errors.append(_("Purchase Receipt {0} is cancelled").format(pr.name))
	if not cint(getattr(pr, "is_return", 0)):
		errors.append(_("Purchase Receipt {0} must be a return").format(pr.name))
	if not (getattr(pr, "return_against", None) or "").strip():
		errors.append(_("Purchase Receipt Return {0} must have a Return Against document").format(pr.name))

	party = sef_supplier(sef)
	if party and pr.supplier and party != pr.supplier:
		errors.append(_("Party mismatch: e-Factura {0}, Purchase Receipt {1}").format(party, pr.supplier))
	if sef.company and pr.company and sef.company != pr.company:
		errors.append(
			_("Company mismatch: e-Factura {0}, Purchase Receipt {1}").format(sef.company, pr.company)
		)

	other = live_sef_for_pr(pr)
	sef_name = getattr(sef, "name", None) or ""
	if other and other != sef_name:
		errors.append(
			_("Purchase Receipt {0} is already linked to Sales eFactura {1}").format(pr.name, other)
		)

	linked = unique_purchase_receipts(sef)
	if any(name != pr.name for name in linked):
		errors.append(
			_("Sales eFactura is already linked to Purchase Receipt {0}").format(linked[0])
		)
	return errors


def match_pr_to_sef(sef, pr) -> tuple[list[dict], list[str]]:
	currency = sef.currency or pr.currency or "MDL"
	mprec = money_precision(currency)
	qprec = qty_precision()
	errors: list[str] = []
	allocs: list[dict] = []

	throw_unmapped_items(
		sef.items or [],
		_("Map all items before linking a Purchase Receipt Return"),
		currency,
	)

	used_sef: set[str] = set()
	for row in _child(sef, "items"):
		if _item_linked(row) and row.name:
			used_sef.add(row.name)

	used_this: set[str] = set()
	for prow in pr.items or []:
		detail = prow.name or f"purchase_receipt-{prow.idx}"
		if prow.name and _detail_taken(prow.name, sef):
			errors.append(
				_("Purchase Receipt row {0} «{1}» is already linked to an e-Factura").format(
					prow.idx, pi_line_name(prow)
				)
			)
			continue

		candidate = None
		mismatch_row = None
		for srow in sef.items or []:
			key = srow.name or f"idx-{srow.idx}"
			if key in used_sef or key in used_this:
				continue
			if _item_linked(srow):
				continue
			if srow.item_code and prow.item_code and srow.item_code != prow.item_code:
				continue
			if lines_compatible(srow, prow, qprec, mprec, abs_qty=True):
				candidate = srow
				break
			mismatch_row = srow

		if candidate is None:
			if mismatch_row is not None:
				errors.append(
					describe_line_mismatch(
						mismatch_row,
						prow,
						currency,
						qprec,
						mprec,
						abs_qty=True,
						label=_("Purchase Receipt Return"),
					)
				)
			else:
				qty = abs(flt(prow.qty))
				amount = abs(flt(prow.amount))
				errors.append(
					_(
						"Purchase Receipt row {0} «{1}»: qty {2} × rate {3} {5} (amount {4}) — not found on e-Factura"
					).format(
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
				"sef_row": candidate,
				"stock_row": prow,
				"qty": buyer_row_qty(candidate),
				"detail": detail,
			}
		)

	return allocs, errors


def _is_full_cover(sef, allocs: list[dict]) -> bool:
	if has_pr_allocations(sef):
		return False
	if not allocs:
		return False
	qprec = qty_precision()
	used: dict[str, float] = {}
	for a in allocs:
		key = a["sef_row"].name or f"idx-{a['sef_row'].idx}"
		used[key] = used.get(key, 0) + flt(a["qty"])
	for row in sef.items or []:
		key = row.name or f"idx-{row.idx}"
		if not eq(used.get(key) or 0, buyer_row_qty(row), qprec):
			return False
	return True


def collect_pr_total_errors(sef, pr, mprec: int, currency: str) -> list[str]:
	errors: list[str] = []
	tgt_total = abs(flt(getattr(pr, "grand_total", None) or getattr(pr, "total", None) or 0))
	if not (
		amount_close(sef.total, tgt_total, mprec) or amount_close(sef.net_total, tgt_total, mprec)
	):
		errors.append(
			_("Grand Total mismatch: e-Factura {0} {2}, Purchase Receipt {1} {2}").format(
				fmt_money(sef.total, mprec),
				fmt_money(tgt_total, mprec),
				currency,
			)
		)
	taxes = abs(flt(getattr(pr, "total_taxes_and_charges", None) or 0))
	if taxes and not amount_close(sef.vat_total, taxes, mprec):
		errors.append(
			_("VAT Total mismatch: e-Factura {0} {2}, Purchase Receipt {1} {2}").format(
				fmt_money(sef.vat_total, mprec),
				fmt_money(taxes, mprec),
				currency,
			)
		)
	return errors


def raise_pr_link_error(pr_name: str, errors: list[str], submit: bool = False):
	items = "".join(f"<li>{frappe.utils.cstr(e)}</li>" for e in errors)
	if submit:
		frappe.throw(
			_("Cannot submit Purchase Receipt {0}:").format(pr_name) + f"<ul>{items}</ul>",
			title=_("Cannot submit Purchase Receipt {0}").format(pr_name),
		)
	frappe.throw(
		_("Cannot link Purchase Receipt {0}:").format(pr_name) + f"<ul>{items}</ul>",
		title=_("e-Factura and Purchase Receipt Return do not match"),
	)


def validate_and_match_pr(sef, pr, submit: bool = False) -> list[dict]:
	errors = collect_pr_document_errors(sef, pr)
	allocs, line_errors = match_pr_to_sef(sef, pr)
	errors.extend(line_errors)
	if _is_full_cover(sef, allocs):
		currency = sef.currency or pr.currency or "MDL"
		errors.extend(collect_pr_total_errors(sef, pr, money_precision(currency), currency))
	if errors:
		raise_pr_link_error(pr.name, errors, submit=submit)
	if not allocs:
		raise_pr_link_error(pr.name, [_("No matching rows to allocate")], submit=submit)
	return allocs


def apply_pr_allocations(sef, allocs: list[dict], pr_name: str) -> None:
	for a in allocs:
		row = a["sef_row"]
		row.purchase_receipt = pr_name
		row.pr_detail = a.get("detail") or a["stock_row"].name


def clear_pr_item_links(sef) -> None:
	for row in sef.items or []:
		row.purchase_receipt = None
		row.pr_detail = None


def delete_allocations_for_pr(pr_name: str) -> list[str]:
	if not pr_name or not frappe.db.has_column(SEF_ITEM, "purchase_receipt"):
		return []
	parents = get_sef_names_for_pr(pr_name)
	items = frappe.get_all(
		SEF_ITEM,
		filters={"purchase_receipt": pr_name, "parenttype": "Sales eFactura"},
		pluck="name",
	)
	for name in items:
		frappe.db.set_value(
			SEF_ITEM,
			name,
			{"purchase_receipt": "", "pr_detail": ""},
			update_modified=False,
		)
	return parents


def submitted_sef_for_pr(pr_name: str) -> str | None:
	for name in get_sef_names_for_pr(pr_name):
		if cint(frappe.db.get_value("Sales eFactura", name, "docstatus")) == 1:
			return name
	header = None
	if frappe.get_meta("Purchase Receipt").has_field("sales_efactura"):
		header = frappe.db.get_value("Purchase Receipt", pr_name, "sales_efactura")
	if header and cint(frappe.db.get_value("Sales eFactura", header, "docstatus")) == 1:
		return header
	return None


def throw_if_submitted_sef_blocks_pr(pr_name: str) -> None:
	sef_name = submitted_sef_for_pr(pr_name)
	if not sef_name:
		return
	frappe.throw(
		_(
			"Cannot cancel or amend Purchase Receipt {0}: Sales eFactura {1} is submitted"
		).format(pr_name, sef_name)
	)


def validate_existing_pr_allocations(sef, pr, submit: bool = True) -> None:
	linked = [r for r in (sef.items or []) if getattr(r, "purchase_receipt", None) == pr.name]
	if not linked:
		return
	currency = sef.currency or pr.currency or "MDL"
	mprec = money_precision(currency)
	qprec = qty_precision()
	errors: list[str] = []
	errors.extend(collect_pr_document_errors(sef, pr))
	by_name = {r.name: r for r in (pr.items or []) if r.name}
	for srow in linked:
		prow = by_name.get(getattr(srow, "pr_detail", None))
		if not prow:
			errors.append(
				_("Purchase Receipt Item {0} is missing on {1}").format(srow.pr_detail, pr.name)
			)
			continue
		if srow.item_code and prow.item_code and srow.item_code != prow.item_code:
			errors.append(
				_("e-Factura row {0} «{1}»: item {2} vs Purchase Receipt {3}").format(
					srow.idx, buyer_line_name(srow), srow.item_code, prow.item_code
				)
			)
		if not uom_matches(srow, prow):
			errors.append(
				_("e-Factura row {0} «{1}»: UOM {2} vs Purchase Receipt {3}").format(
					srow.idx,
					buyer_line_name(srow),
					srow.uom or srow.ef_uom or _("empty"),
					prow.uom or _("empty"),
				)
			)
		if not price_matches(srow, prow, mprec, abs_qty=True):
			errors.append(
				_("e-Factura row {0} «{1}»: amounts do not match Purchase Receipt").format(
					srow.idx, buyer_line_name(srow)
				)
			)
		if not qty_matches(srow, prow, qprec, abs_qty=True):
			errors.append(
				_("e-Factura row {0} «{1}»: quantity does not match Purchase Receipt").format(
					srow.idx, buyer_line_name(srow)
				)
			)
	if errors:
		raise_pr_link_error(pr.name, errors, submit=submit)


def find_pr_qty_overages(sef) -> list[dict]:
	"""SEF qty vs abs(PR qty) by item_code. 1:1 — no sibling SEFs."""
	if not is_sef_return(sef):
		return []
	pr_names = unique_purchase_receipts(sef)
	if not pr_names:
		return []
	precision = qty_precision()
	sef_qty: dict[str, float] = {}
	item_name: dict[str, str] = {}
	for row in sef.get("items") or []:
		code = (row.item_code or "").strip()
		if not code:
			continue
		sef_qty[code] = sef_qty.get(code, 0) + abs(flt(row.qty) or flt(row.stock_qty))
		if row.item_name:
			item_name[code] = row.item_name
	pr_qty: dict[str, float] = {}
	for pr_name in pr_names:
		for row in frappe.get_all(
			"Purchase Receipt Item",
			filters={"parent": pr_name},
			fields=["item_code", "item_name", "qty"],
		):
			code = (row.item_code or "").strip()
			if not code:
				continue
			pr_qty[code] = pr_qty.get(code, 0) + abs(flt(row.qty))
			item_name.setdefault(code, row.item_name or code)
	overages = []
	for code, this_qty in sef_qty.items():
		allowed = flt(pr_qty.get(code, 0), precision)
		this_qty = flt(this_qty, precision)
		if this_qty <= allowed:
			continue
		overages.append(
			{
				"item_code": code,
				"item_name": item_name.get(code) or code,
				"pr_qty": allowed,
				"this_qty": this_qty,
			}
		)
	return overages


def format_pr_overage_html(overages: list[dict], pr_name: str | None) -> str:
	if not overages:
		return ""
	rows = "".join(
		f"<li>{frappe.utils.escape_html(row['item_name'])} ({frappe.utils.escape_html(row['item_code'])}): "
		f"{fmt_qty(row['this_qty'], qty_precision())} &gt; "
		f"{fmt_qty(row['pr_qty'], qty_precision())}</li>"
		for row in overages
	)
	heading = _("Quantity exceeds Purchase Receipt Return {0}").format(pr_name or "")
	return heading + f"<ul>{rows}</ul>"


def enforce_pr_qty_on_submit(sef) -> None:
	overages = find_pr_qty_overages(sef)
	if not overages:
		return
	pr_name = (unique_purchase_receipts(sef) or [None])[0]
	frappe.throw(format_pr_overage_html(overages, pr_name), title=_("Quantity exceeds Purchase Receipt Return"))
