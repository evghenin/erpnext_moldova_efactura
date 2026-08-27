"""PI/PO unit rate from e-Factura line totals (not printed unit price).

SFS line totals are authoritative. qty × UnitPriceWithoutTVA often differs
from TotalPriceWithoutTVA after rounding (e.g. 186.763 × 26.08 → 4,870.78
vs XML 4,870.23). Booking rate = line amount / qty so ERPNext amount matches.
"""

from __future__ import annotations

from frappe.utils import flt

BUYING_RATE_PRECISION = 6


def row_rate_with_vat(row) -> float:
	"""Gross unit rate. Purchase eFactura Item has rate_with_vat; Sales eFactura Item does not."""
	if callable(getattr(row, "get", None)):
		return flt(row.get("rate_with_vat"))
	return flt(getattr(row, "rate_with_vat", None) or 0)


def line_amount(row, vat_included: bool) -> float:
	return flt(row.amount) if vat_included else flt(row.net_amount)


def printed_unit_rate(row, vat_included: bool) -> float:
	if vat_included:
		return row_rate_with_vat(row) or flt(row.rate)
	return flt(row.rate)


def implied_unit_rate(row, vat_included: bool) -> float:
	qty = flt(row.qty) if flt(row.qty) else flt(getattr(row, "ef_qty", 0))
	amount = line_amount(row, vat_included)
	if qty and amount:
		return amount / qty
	return printed_unit_rate(row, vat_included)


def buying_rate_for_row(row, vat_included: bool) -> float:
	"""Rate such that booking qty × rate ≈ XML line amount."""
	qty = flt(row.qty)
	amount = line_amount(row, vat_included)
	if qty and amount:
		return amount / qty
	printed = printed_unit_rate(row, vat_included)
	ef_qty = flt(getattr(row, "ef_qty", 0))
	if qty and ef_qty:
		return printed * ef_qty / qty
	return printed
