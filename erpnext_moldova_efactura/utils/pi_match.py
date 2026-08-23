"""Match an existing Purchase Invoice to Purchase eFactura lines and totals."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_moldova_efactura.utils.buying_rate import implied_unit_rate


def money_precision(currency: str | None = None) -> int:
	try:
		prec = frappe.get_precision("Purchase eFactura", "total")
		if prec is not None:
			return cint(prec)
	except Exception:
		pass
	if currency:
		try:
			frac = frappe.db.get_value("Currency", currency, "fraction")
			if frac is not None:
				return max(cint(frac), 0)
		except Exception:
			pass
	return 2


def qty_precision() -> int:
	try:
		prec = frappe.get_precision("Purchase eFactura Item", "ef_qty")
		if prec is not None:
			return cint(prec)
	except Exception:
		pass
	return 3


def eq(a, b, precision: int) -> bool:
	return flt(a, precision) == flt(b, precision)


def fmt_money(value, precision: int) -> str:
	return f"{flt(value, precision):.{precision}f}"


def fmt_qty(value, precision: int) -> str:
	return f"{flt(value, precision):.{precision}f}"


def buyer_line_name(row) -> str:
	return (
		getattr(row, "supplier_item_name", None)
		or getattr(row, "item_name", None)
		or getattr(row, "item_code", None)
		or getattr(row, "supplier_item_code", None)
		or getattr(row, "ef_item_code", None)
		or ""
	).strip() or _("row {0}").format(row.idx)


def pi_line_name(row) -> str:
	return (row.item_name or row.item_code or "").strip() or _("row {0}").format(row.idx)


def qty_matches(buyer_row, pi_row, qprec: int) -> bool:
	pi_qty = flt(pi_row.qty)
	if eq(pi_qty, buyer_row.ef_qty, qprec):
		return True
	if flt(buyer_row.qty) and eq(pi_qty, buyer_row.qty, qprec):
		return True
	return False


def rate_matches(buyer_row, pi_row, mprec: int) -> bool:
	pi_rate = flt(pi_row.rate)
	expected = expected_buyer_rate(buyer_row)
	alts = {
		expected,
		flt(buyer_row.rate),
		flt(buyer_row.rate_with_vat),
		implied_unit_rate(buyer_row, vat_included=False),
		implied_unit_rate(buyer_row, vat_included=True),
	}
	return any(eq(pi_rate, alt, mprec) for alt in alts if alt or alt == 0)


def price_matches(buyer_row, pi_row, mprec: int) -> bool:
	pi_amount = flt(pi_row.amount)
	rate_ok = rate_matches(buyer_row, pi_row, mprec)
	amount_ok = eq(pi_amount, buyer_row.net_amount, mprec) or eq(pi_amount, buyer_row.amount, mprec)
	return rate_ok or amount_ok


def describe_rate_mismatch(buyer_row, pi_row, currency: str, mprec: int) -> str:
	return _("e-Factura row {0} «{1}»: {2}.").format(
		buyer_row.idx,
		buyer_line_name(buyer_row),
		_("rate {0} / net {1} vs Purchase Invoice rate {2} / amount {3} {4}").format(
			fmt_money(expected_buyer_rate(buyer_row), mprec),
			fmt_money(buyer_row.net_amount, mprec),
			fmt_money(pi_row.rate, mprec),
			fmt_money(pi_row.amount, mprec),
			currency or "",
		),
	)


def uom_matches(buyer_row, pi_row) -> bool:
	pi_uom = (pi_row.uom or "").strip()
	if not pi_uom:
		return True
	known = {u.strip() for u in (buyer_row.uom, buyer_row.ef_uom) if u}
	if not known:
		return True
	return pi_uom in known


def lines_compatible(buyer_row, pi_row, qprec: int, mprec: int) -> bool:
	return qty_matches(buyer_row, pi_row, qprec) and price_matches(buyer_row, pi_row, mprec) and uom_matches(buyer_row, pi_row)


def describe_line_mismatch(buyer_row, pi_row, currency: str, qprec: int, mprec: int) -> str:
	parts: list[str] = []
	if not qty_matches(buyer_row, pi_row, qprec):
		parts.append(
			_("quantity {0} vs Purchase Invoice {1}").format(
				fmt_qty(buyer_row.ef_qty if flt(buyer_row.ef_qty) else buyer_row.qty, qprec),
				fmt_qty(pi_row.qty, qprec),
			)
		)
	if not price_matches(buyer_row, pi_row, mprec):
		parts.append(
			_("rate {0} / net {1} vs Purchase Invoice rate {2} / amount {3} {4}").format(
				fmt_money(buyer_row.rate, mprec),
				fmt_money(buyer_row.net_amount, mprec),
				fmt_money(pi_row.rate, mprec),
				fmt_money(pi_row.amount, mprec),
				currency or "",
			)
		)
	if not uom_matches(buyer_row, pi_row):
		parts.append(
			_("UOM {0} vs Purchase Invoice {1}").format(
				buyer_row.uom or buyer_row.ef_uom or _("empty"),
				pi_row.uom or _("empty"),
			)
		)
	if buyer_row.item_code and pi_row.item_code and buyer_row.item_code != pi_row.item_code:
		parts.append(
			_("item {0} vs Purchase Invoice {1}").format(buyer_row.item_code, pi_row.item_code)
		)
	if not parts:
		parts.append(_("does not match"))
	return _("e-Factura row {0} «{1}»: {2}.").format(buyer_row.idx, buyer_line_name(buyer_row), "; ".join(parts))


def collect_totals_and_line_errors(
	buyer,
	pi,
	mprec: int | None = None,
	qprec: int | None = None,
) -> tuple[list[str], list[tuple[Any, Any]]]:
	"""Return (error messages, matched pairs). Pairs may be partial when errors exist."""
	currency = buyer.currency or pi.currency or "MDL"
	mprec = money_precision(currency) if mprec is None else mprec
	qprec = qty_precision() if qprec is None else qprec
	errors: list[str] = []

	errors.extend(collect_total_errors(buyer, pi, mprec, currency))

	buyer_items = list(buyer.items or [])
	pi_items = list(pi.items or [])

	if len(buyer_items) != len(pi_items):
		errors.append(
			_("Item count mismatch: e-Factura has {0} row(s), Purchase Invoice has {1} row(s)").format(
				len(buyer_items), len(pi_items)
			)
		)

	used: set[int] = set()
	pairs: list[tuple[Any, Any]] = []

	for brow in buyer_items:
		candidate_idx = None
		if brow.item_code:
			same = [
				i
				for i, prow in enumerate(pi_items)
				if i not in used and prow.item_code == brow.item_code
			]
			compatible = [i for i in same if lines_compatible(brow, pi_items[i], qprec, mprec)]
			if compatible:
				candidate_idx = compatible[0]
			elif same:
				idx = same[0]
				errors.append(describe_line_mismatch(brow, pi_items[idx], currency, qprec, mprec))
				used.add(idx)
				continue

		if candidate_idx is None:
			for i, prow in enumerate(pi_items):
				if i in used:
					continue
				if brow.item_code and prow.item_code and brow.item_code != prow.item_code:
					continue
				if lines_compatible(brow, prow, qprec, mprec):
					candidate_idx = i
					break

		if candidate_idx is None:
			errors.append(
				_(
					"e-Factura row {0} «{1}»: qty {2} × rate {3} {5} (net {4}) — no matching Purchase Invoice row"
				).format(
					brow.idx,
					buyer_line_name(brow),
					fmt_qty(buyer_row_qty(brow), qprec),
					fmt_money(brow.rate, mprec),
					fmt_money(brow.net_amount, mprec),
					currency,
				)
			)
			continue

		used.add(candidate_idx)
		pairs.append((brow, pi_items[candidate_idx]))

	for i, prow in enumerate(pi_items):
		if i in used:
			continue
		errors.append(
			_(
				"Purchase Invoice row {0} «{1}»: qty {2} × rate {3} {5} (amount {4}) — not found on e-Factura"
			).format(
				prow.idx,
				pi_line_name(prow),
				fmt_qty(prow.qty, qprec),
				fmt_money(prow.rate, mprec),
				fmt_money(prow.amount, mprec),
				currency,
			)
		)

	return errors, pairs


def collect_total_errors(buyer, pi, mprec: int, currency: str) -> list[str]:
	errors: list[str] = []
	if not amount_close(buyer.total, pi.grand_total, mprec):
		errors.append(
			_("Grand Total mismatch: e-Factura {0} {2}, Purchase Invoice {1} {2}").format(
				fmt_money(buyer.total, mprec),
				fmt_money(pi.grand_total, mprec),
				currency,
			)
		)
	if not amount_close(buyer.vat_total, pi.total_taxes_and_charges, mprec):
		errors.append(
			_("VAT Total mismatch: e-Factura {0} {2}, Purchase Invoice {1} {2}").format(
				fmt_money(buyer.vat_total, mprec),
				fmt_money(pi.total_taxes_and_charges, mprec),
				currency,
			)
		)
	return errors


def buyer_row_qty(row) -> float:
	return flt(row.qty) if flt(row.qty) else flt(row.ef_qty)


def describe_unmapped_row(row, currency: str | None = None) -> str:
	qprec = qty_precision()
	mprec = money_precision(currency)
	return _("Row {0}: {1} — qty {2}, rate {3} {4}").format(
		row.idx,
		buyer_line_name(row),
		fmt_qty(buyer_row_qty(row), qprec),
		fmt_money(row.rate, mprec),
		currency or "",
	)


def unmapped_item_messages(items, currency: str | None = None) -> list[str]:
	return [describe_unmapped_row(r, currency) for r in (items or []) if not r.item_code]


def throw_unmapped_items(items, heading: str, currency: str | None = None):
	msgs = unmapped_item_messages(items, currency)
	if not msgs:
		return
	items_html = "".join(f"<li>{frappe.utils.cstr(m)}</li>" for m in msgs)
	frappe.throw(heading + f"<ul>{items_html}</ul>", title=_("Map all items"))


SPLIT_AMOUNT_TOLERANCE = 0.01


def expected_buyer_rate(buyer_row) -> float:
	vat_included = False
	try:
		vat_included = bool(frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate"))
	except Exception:
		pass
	if vat_included and flt(buyer_row.rate_with_vat):
		return flt(buyer_row.rate_with_vat)
	return flt(buyer_row.rate)


def amount_close(a, b, mprec: int) -> bool:
	return eq(a, b, mprec) or abs(flt(a) - flt(b)) <= SPLIT_AMOUNT_TOLERANCE + 1e-9


def collect_document_errors(buyer, pi) -> list[str]:
	errors: list[str] = []
	if cint(pi.docstatus) == 2:
		errors.append(_("Purchase Invoice {0} is cancelled").format(pi.name))
	if buyer.supplier and pi.supplier and buyer.supplier != pi.supplier:
		errors.append(
			_("Supplier mismatch: e-Factura {0}, Purchase Invoice {1}").format(
				buyer.supplier, pi.supplier
			)
		)
	if buyer.company and pi.company and buyer.company != pi.company:
		errors.append(
			_("Company mismatch: e-Factura {0}, Purchase Invoice {1}").format(
				buyer.company, pi.company
			)
		)
	other = None
	if getattr(pi, "name", None) and frappe.db.has_column("Purchase eFactura Item", "purchase_invoice"):
		other = frappe.db.get_value(
			"Purchase eFactura Item",
			{"purchase_invoice": pi.name, "parent": ["!=", getattr(buyer, "name", "") or ""]},
			"parent",
		)
	if other:
		errors.append(
			_("Purchase Invoice {0} is already linked to e-Factura {1}").format(pi.name, other)
		)
	return errors


def raise_link_error(pi_name: str, errors: list[str], submit: bool = False):
	items = "".join(f"<li>{frappe.utils.cstr(e)}</li>" for e in errors)
	if submit:
		frappe.throw(
			_("Cannot submit Purchase Invoice {0}:").format(pi_name) + f"<ul>{items}</ul>",
			title=_("Cannot submit Purchase Invoice {0}").format(pi_name),
		)
	frappe.throw(
		_("Cannot link Purchase Invoice {0}:").format(pi_name) + f"<ul>{items}</ul>",
		title=_("e-Factura and Purchase Invoice do not match"),
	)


def validate_and_match(buyer, pi, submit: bool = False) -> list[dict]:
	"""Match PI rows to remaining factura qty and return allocation dicts."""
	from erpnext_moldova_efactura.utils.pi_alloc import is_full_document_cover, match_pi_to_remaining

	errors = collect_document_errors(buyer, pi)
	allocs, line_errors = match_pi_to_remaining(buyer, pi)
	errors.extend(line_errors)
	if is_full_document_cover(buyer, allocs):
		currency = buyer.currency or pi.currency or "MDL"
		errors.extend(collect_total_errors(buyer, pi, money_precision(currency), currency))
	if errors:
		raise_link_error(pi.name, errors, submit=submit)
	if not allocs:
		raise_link_error(
			pi.name,
			[_("No matching rows to allocate")],
			submit=submit,
		)
	return allocs


def validate_existing_allocations(buyer, pi, submit: bool = True) -> None:
	"""On PI submit: linked factura rows must still match item, qty, rate, UOM."""
	linked = [r for r in (buyer.items or []) if r.purchase_invoice == pi.name]
	if not linked:
		return
	currency = buyer.currency or pi.currency or "MDL"
	mprec = money_precision(currency)
	qprec = qty_precision()
	errors: list[str] = []
	errors.extend(collect_document_errors(buyer, pi))
	pi_by_name = {r.name: r for r in (pi.items or []) if r.name}
	for brow in linked:
		prow = pi_by_name.get(brow.pi_detail)
		if not prow:
			errors.append(
				_("Purchase Invoice Item {0} is missing on {1}").format(brow.pi_detail, pi.name)
			)
			continue
		if brow.item_code and prow.item_code and brow.item_code != prow.item_code:
			errors.append(
				_("e-Factura row {0} «{1}»: item {2} vs Purchase Invoice {3}").format(
					brow.idx, buyer_line_name(brow), brow.item_code, prow.item_code
				)
			)
		if not uom_matches(brow, prow):
			errors.append(
				_("e-Factura row {0} «{1}»: UOM {2} vs Purchase Invoice {3}").format(
					brow.idx,
					buyer_line_name(brow),
					brow.uom or brow.ef_uom or _("empty"),
					prow.uom or _("empty"),
				)
			)
		if not rate_matches(brow, prow, mprec):
			errors.append(describe_rate_mismatch(brow, prow, currency, mprec))
		if not qty_matches(brow, prow, qprec):
			errors.append(describe_line_mismatch(brow, prow, currency, qprec, mprec))
	if is_full_cover_existing(buyer, pi.name):
		errors.extend(collect_total_errors(buyer, pi, mprec, currency))
	if errors:
		raise_link_error(pi.name, errors, submit=submit)


def is_full_cover_existing(buyer, pi_name: str) -> bool:
	linked = [r for r in (buyer.items or []) if r.purchase_invoice == pi_name]
	others = [r for r in (buyer.items or []) if r.purchase_invoice and r.purchase_invoice != pi_name]
	if others or not linked:
		return False
	return all(r.purchase_invoice == pi_name for r in (buyer.items or []))
