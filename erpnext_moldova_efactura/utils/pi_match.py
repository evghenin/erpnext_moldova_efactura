"""Match an existing Purchase Invoice to Purchase eFactura lines and totals."""

from __future__ import annotations

from itertools import combinations
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_moldova_efactura.utils.buying_rate import (
	BUYING_RATE_PRECISION,
	buying_rate_for_row,
	implied_unit_rate,
	row_rate_with_vat,
)


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


def qty_matches(buyer_row, pi_row, qprec: int, abs_qty: bool = False) -> bool:
	pi_qty = abs(flt(pi_row.qty)) if abs_qty else flt(pi_row.qty)
	buyer_ef = abs(flt(buyer_row.ef_qty)) if abs_qty else flt(buyer_row.ef_qty)
	if eq(pi_qty, buyer_ef, qprec):
		return True
	buyer_qty = abs(flt(buyer_row.qty)) if abs_qty else flt(buyer_row.qty)
	if buyer_qty and eq(pi_qty, buyer_qty, qprec):
		return True
	return False


def _maybe_abs(value, abs_qty: bool) -> float:
	return abs(flt(value)) if abs_qty else flt(value)


def unit_rate_compatible(buyer_row, pi_row, mprec: int, abs_qty: bool = False) -> bool:
	"""Same unit rate, ignoring that a PI row may be only part of the factura qty."""
	pi_rate = _maybe_abs(pi_row.rate, abs_qty)
	alts = {
		_maybe_abs(expected_buyer_rate(buyer_row), abs_qty),
		_maybe_abs(buyer_row.rate, abs_qty),
		_maybe_abs(row_rate_with_vat(buyer_row), abs_qty),
		_maybe_abs(implied_unit_rate(buyer_row, vat_included=False), abs_qty),
		_maybe_abs(implied_unit_rate(buyer_row, vat_included=True), abs_qty),
		_maybe_abs(buying_rate_for_row(buyer_row, False), abs_qty),
		_maybe_abs(buying_rate_for_row(buyer_row, True), abs_qty),
	}
	return any(
		eq(pi_rate, alt, mprec) or eq(pi_rate, alt, BUYING_RATE_PRECISION)
		for alt in alts
		if alt or alt == 0
	)


def rate_matches(buyer_row, pi_row, mprec: int, abs_qty: bool = False) -> bool:
	if unit_rate_compatible(buyer_row, pi_row, mprec, abs_qty=abs_qty):
		return True
	# Converted UOM (kWh → MWh): compare line extension, not XML unit price.
	pi_ext = abs(flt(pi_row.qty) * flt(pi_row.rate)) if abs_qty else flt(pi_row.qty) * flt(pi_row.rate)
	pi_amount = _maybe_abs(pi_row.amount, abs_qty)
	if not pi_ext:
		pi_ext = pi_amount
	buyer_net = _maybe_abs(buyer_row.net_amount, abs_qty)
	buyer_amt = _maybe_abs(buyer_row.amount, abs_qty)
	return (
		amount_close(pi_ext, buyer_net, mprec)
		or amount_close(pi_ext, buyer_amt, mprec)
		or amount_close(pi_amount, buyer_net, mprec)
		or amount_close(pi_amount, buyer_amt, mprec)
	)


def price_matches(buyer_row, pi_row, mprec: int, abs_qty: bool = False) -> bool:
	pi_amount = _maybe_abs(pi_row.amount, abs_qty)
	rate_ok = rate_matches(buyer_row, pi_row, mprec, abs_qty=abs_qty)
	amount_ok = eq(pi_amount, _maybe_abs(buyer_row.net_amount, abs_qty), mprec) or eq(
		pi_amount, _maybe_abs(buyer_row.amount, abs_qty), mprec
	)
	return rate_ok or amount_ok


def is_erpnext_return_invoice(pi) -> bool:
	if cint(getattr(pi, "is_return", 0)):
		return True
	items = getattr(pi, "items", None) or []
	if not items or flt(getattr(pi, "grand_total", 0)) >= 0:
		return False
	return all(flt(row.qty) < 0 and flt(row.rate) >= 0 for row in items)


def use_abs_qty_rate_match(buyer, pi) -> bool:
	from erpnext_moldova_efactura.utils.pef_mode import has_inverted_credit_signs

	return has_inverted_credit_signs(buyer) and is_erpnext_return_invoice(pi)


def describe_rate_mismatch(buyer_row, pi_row, currency: str, mprec: int) -> str:
	return _("e-Factura row {0} «{1}»: {2}.").format(
		buyer_row.idx,
		buyer_line_name(buyer_row),
		_("rate {0} / net {1} vs Purchase Invoice rate {2} / amount {3} {4}").format(
			fmt_money(buying_rate_for_row(buyer_row, False) or expected_buyer_rate(buyer_row), mprec),
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


def lines_compatible(buyer_row, pi_row, qprec: int, mprec: int, abs_qty: bool = False) -> bool:
	return (
		qty_matches(buyer_row, pi_row, qprec, abs_qty=abs_qty)
		and price_matches(buyer_row, pi_row, mprec, abs_qty=abs_qty)
		and uom_matches(buyer_row, pi_row)
	)


def identity_rate_compatible(buyer_row, pi_row, mprec: int, abs_qty: bool = False) -> bool:
	"""Same ERP item (split PO lines may use Unit vs Nos) or same UOM when unmapped."""
	if buyer_row.item_code and pi_row.item_code:
		return buyer_row.item_code == pi_row.item_code
	return uom_matches(buyer_row, pi_row)


def split_child_names(value) -> list[str]:
	if not value:
		return []
	return [part for part in str(value).replace(",", "\n").split() if part]


def join_child_names(names) -> str:
	out: list[str] = []
	seen: set[str] = set()
	for name in names or []:
		if name and name not in seen:
			seen.add(name)
			out.append(name)
	return "\n".join(out)


def _signed_qty(row, abs_qty: bool) -> float:
	return abs(flt(row.qty)) if abs_qty else flt(row.qty)


def _signed_amount(row, abs_qty: bool) -> float:
	amount = flt(getattr(row, "amount", None) or 0)
	if not amount:
		amount = flt(getattr(row, "net_amount", None) or 0)
	return abs(amount) if abs_qty else amount


def covering_pi_rows(buyer_row, candidates, qprec: int, mprec: int, abs_qty: bool = False):
	"""Smallest set of PI rows whose qty and amount sum to the factura row."""
	if not candidates:
		return None
	need_qty = abs(buyer_row_qty(buyer_row)) if abs_qty else buyer_row_qty(buyer_row)
	n = len(candidates)

	def _ok(combo) -> bool:
		qty = sum(_signed_qty(candidates[i], abs_qty) for i in combo)
		amount = sum(_signed_amount(candidates[i], abs_qty) for i in combo)
		if not eq(qty, need_qty, qprec):
			return False
		rates = [_maybe_abs(candidates[i].rate, abs_qty) for i in combo]
		if rates and not all(
			eq(rate, rates[0], mprec) or eq(rate, rates[0], BUYING_RATE_PRECISION) for rate in rates
		):
			return False
		pi_rate = rates[0] if rates else 0
		implied_pi = need_qty * pi_rate if pi_rate else 0
		buyer_rate = _maybe_abs(buyer_row.rate, abs_qty)
		implied_buyer = need_qty * buyer_rate if buyer_rate else 0
		net_like = [
			_maybe_abs(buyer_row.net_amount, abs_qty),
			_maybe_abs(getattr(buyer_row, "ef_net_amount", None), abs_qty),
			implied_buyer,
		]
		gross_like = [
			_maybe_abs(buyer_row.amount, abs_qty),
			_maybe_abs(getattr(buyer_row, "ef_amount", None), abs_qty),
		]
		if any(amount_close(amount, total, mprec) for total in net_like if total):
			return True
		if implied_pi and amount_close(amount, implied_pi, mprec):
			if not buyer_rate or unit_rate_compatible(buyer_row, candidates[combo[0]], mprec, abs_qty=abs_qty):
				return True
		return any(amount_close(amount, total, mprec) for total in gross_like if total)

	if n > 12:
		for i in range(n):
			if _ok((i,)):
				return [candidates[i]]
		if _ok(tuple(range(n))):
			return list(candidates)
		return None
	for size in range(1, n + 1):
		for combo in combinations(range(n), size):
			if _ok(combo):
				return [candidates[i] for i in combo]
	return None


def describe_line_mismatch(
	buyer_row,
	pi_row,
	currency: str,
	qprec: int,
	mprec: int,
	abs_qty: bool = False,
	label: str | None = None,
) -> str:
	other = label or _("Purchase Invoice")
	other_qty = abs(flt(pi_row.qty)) if abs_qty else flt(pi_row.qty)
	other_amount = abs(flt(pi_row.amount)) if abs_qty else flt(pi_row.amount)
	parts: list[str] = []
	if not qty_matches(buyer_row, pi_row, qprec, abs_qty=abs_qty):
		parts.append(
			_("quantity {0} vs {1} {2}").format(
				fmt_qty(buyer_row.ef_qty if flt(buyer_row.ef_qty) else buyer_row.qty, qprec),
				other,
				fmt_qty(other_qty, qprec),
			)
		)
	if not price_matches(buyer_row, pi_row, mprec, abs_qty=abs_qty):
		parts.append(
			_("rate {0} / net {1} vs {2} rate {3} / amount {4} {5}").format(
				fmt_money(buyer_row.rate, mprec),
				fmt_money(buyer_row.net_amount, mprec),
				other,
				fmt_money(pi_row.rate, mprec),
				fmt_money(other_amount, mprec),
				currency or "",
			)
		)
	if not uom_matches(buyer_row, pi_row):
		parts.append(
			_("UOM {0} vs {1} {2}").format(
				buyer_row.uom or buyer_row.ef_uom or _("empty"),
				other,
				pi_row.uom or _("empty"),
			)
		)
	if buyer_row.item_code and pi_row.item_code and buyer_row.item_code != pi_row.item_code:
		parts.append(_("item {0} vs {1} {2}").format(buyer_row.item_code, other, pi_row.item_code))
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
	abs_qty = use_abs_qty_rate_match(buyer, pi)

	buyer_items = list(buyer.items or [])
	pi_items = list(pi.items or [])
	used: set[int] = set()
	pairs: list[tuple[Any, Any]] = []

	for brow in buyer_items:
		open_idx = [
			i
			for i, prow in enumerate(pi_items)
			if i not in used and identity_rate_compatible(brow, prow, mprec, abs_qty=abs_qty)
		]
		chosen = covering_pi_rows(
			brow, [pi_items[i] for i in open_idx], qprec, mprec, abs_qty=abs_qty
		)
		if not chosen:
			if open_idx:
				errors.append(
					describe_line_mismatch(
						brow, pi_items[open_idx[0]], currency, qprec, mprec, abs_qty=abs_qty
					)
				)
			else:
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
		chosen_ids = {id(row) for row in chosen}
		for i in open_idx:
			if id(pi_items[i]) in chosen_ids:
				used.add(i)
				pairs.append((brow, pi_items[i]))

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
	if vat_included and row_rate_with_vat(buyer_row):
		return row_rate_with_vat(buyer_row)
	return flt(buyer_row.rate)


def amount_close(a, b, mprec: int) -> bool:
	return eq(a, b, mprec) or abs(flt(a) - flt(b)) <= SPLIT_AMOUNT_TOLERANCE + 1e-9


def collect_document_errors(buyer, pi) -> list[str]:
	errors: list[str] = []
	if cint(pi.docstatus) == 2:
		errors.append(_("Purchase Invoice {0} is cancelled").format(pi.name))
	from erpnext_moldova_efactura.utils.pef_mode import pef_supplier

	supplier = pef_supplier(buyer)
	if supplier and pi.supplier and supplier != pi.supplier:
		errors.append(
			_("Supplier mismatch: e-Factura {0}, Purchase Invoice {1}").format(
				supplier, pi.supplier
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
	abs_qty = use_abs_qty_rate_match(buyer, pi)
	errors: list[str] = []
	errors.extend(collect_document_errors(buyer, pi))
	pi_by_name = {r.name: r for r in (pi.items or []) if r.name}
	for brow in linked:
		details = split_child_names(brow.pi_detail)
		prows = []
		for detail in details:
			prow = pi_by_name.get(detail)
			if not prow:
				errors.append(_("Purchase Invoice Item {0} is missing on {1}").format(detail, pi.name))
				continue
			prows.append(prow)
		if not prows:
			continue
		for prow in prows:
			if brow.item_code and prow.item_code and brow.item_code != prow.item_code:
				errors.append(
					_("e-Factura row {0} «{1}»: item {2} vs Purchase Invoice {3}").format(
						brow.idx, buyer_line_name(brow), brow.item_code, prow.item_code
					)
				)
			if not (
				brow.item_code and prow.item_code and brow.item_code == prow.item_code
			) and not uom_matches(brow, prow):
				errors.append(
					_("e-Factura row {0} «{1}»: UOM {2} vs Purchase Invoice {3}").format(
						brow.idx,
						buyer_line_name(brow),
						brow.uom or brow.ef_uom or _("empty"),
						prow.uom or _("empty"),
					)
				)
			if not price_matches(brow, prow, mprec, abs_qty=abs_qty):
				errors.append(describe_rate_mismatch(brow, prow, currency, mprec))
		need = abs(buyer_row_qty(brow)) if abs_qty else buyer_row_qty(brow)
		got = sum(_signed_qty(prow, abs_qty) for prow in prows)
		if not eq(need, got, qprec):
			errors.append(describe_line_mismatch(brow, prows[0], currency, qprec, mprec, abs_qty=abs_qty))
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
