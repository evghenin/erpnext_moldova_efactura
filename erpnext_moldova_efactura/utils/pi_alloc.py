"""Purchase eFactura ↔ Purchase Invoice line links (one factura row ↔ one PI row)."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt

from erpnext_moldova_efactura.utils.pi_match import (
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
	qty_precision,
	throw_unmapped_items,
	use_abs_qty_rate_match,
)


def _child(obj, field):
	if obj is None:
		return []
	if hasattr(obj, "get"):
		val = obj.get(field)
		if val is not None:
			return val
	return getattr(obj, field, None) or []


def _item_linked(row) -> bool:
	return bool(getattr(row, "purchase_invoice", None))


def has_allocations(buyer) -> bool:
	return any(_item_linked(row) for row in _child(buyer, "items"))


def unallocated_rows(items) -> list:
	return [row for row in (items or []) if not _item_linked(row)]


def unallocated_item_messages(items, currency: str | None = None) -> list[str]:
	return [describe_unmapped_row(row, currency) for row in unallocated_rows(items)]


def throw_unallocated_items(items, heading: str, currency: str | None = None):
	msgs = unallocated_item_messages(items, currency)
	if not msgs:
		return
	items_html = "".join(f"<li>{frappe.utils.cstr(m)}</li>" for m in msgs)
	frappe.throw(heading + f"<ul>{items_html}</ul>", title=_("Rows not allocated"))


def remaining_qty_for_item(buyer, item_row) -> float:
	if _item_linked(item_row):
		return 0.0
	return buyer_row_qty(item_row)


def unique_purchase_invoices(buyer) -> list[str]:
	names: list[str] = []
	seen: set[str] = set()
	for row in _child(buyer, "items"):
		pi = getattr(row, "purchase_invoice", None)
		if pi and pi not in seen:
			seen.add(pi)
			names.append(pi)
	return names


def get_buyer_name_for_pi(pi) -> str | None:
	"""Buyer that has item rows linked to this Purchase Invoice."""
	names = get_buyer_names_for_pi(pi if isinstance(pi, str) else getattr(pi, "name", None))
	return names[0] if names else None


def purchase_invoice_is_fully_covered(pi_name: str) -> bool:
	"""True when every billed PI line is already linked to a live e-Factura."""
	if not pi_name or not frappe.db.has_column("Purchase eFactura Item", "pi_detail"):
		return False
	rows = frappe.get_all(
		"Purchase Invoice Item",
		filters={"parent": pi_name, "parenttype": "Purchase Invoice"},
		fields=["name", "qty"],
	)
	billable = [row.name for row in rows if flt(row.qty)]
	if not billable:
		return False
	linked = frappe.get_all(
		"Purchase eFactura Item",
		filters={"pi_detail": ["in", billable], "parenttype": "Purchase eFactura"},
		fields=["pi_detail", "parent"],
	)
	parents = {row.parent for row in linked if row.parent}
	live = set()
	if parents:
		live = set(
			frappe.get_all(
				"Purchase eFactura",
				filters={"name": ["in", list(parents)], "docstatus": ["<", 2]},
				pluck="name",
			)
		)
	taken = {row.pi_detail for row in linked if row.parent in live}
	return all(name in taken for name in billable)


def get_buyer_names_for_pi(pi_name: str) -> list[str]:
	if not pi_name:
		return []
	if not frappe.db.has_column("Purchase eFactura Item", "purchase_invoice"):
		return []
	names = frappe.get_all(
		"Purchase eFactura Item",
		filters={"purchase_invoice": pi_name, "parenttype": "Purchase eFactura"},
		pluck="parent",
	)
	out: list[str] = []
	seen: set[str] = set()
	for name in names:
		if name and name not in seen:
			seen.add(name)
			out.append(name)
	return out


def set_pi_buyer_link(pi_name: str, buyer_name: str) -> None:
	if not pi_name or not buyer_name:
		return
	if not frappe.get_meta("Purchase Invoice").has_field("purchase_efactura"):
		return
	current = frappe.db.get_value("Purchase Invoice", pi_name, "purchase_efactura")
	if current != buyer_name:
		frappe.db.set_value("Purchase Invoice", pi_name, "purchase_efactura", buyer_name, update_modified=False)


def clear_pi_buyer_link(pi_name: str) -> None:
	if not pi_name or not frappe.get_meta("Purchase Invoice").has_field("purchase_efactura"):
		return
	if frappe.db.get_value("Purchase Invoice", pi_name, "purchase_efactura"):
		frappe.db.set_value("Purchase Invoice", pi_name, "purchase_efactura", "", update_modified=False)


def find_source_buyer(pi) -> str | None:
	if getattr(pi, "meta", None) and pi.meta.has_field("purchase_efactura") and pi.get("purchase_efactura"):
		return pi.purchase_efactura
	by_po = find_buyer_by_po(pi)
	if by_po:
		return by_po
	return find_buyer_by_bill_no(pi)


def find_buyer_by_po(pi, po_name: str | None = None) -> str | None:
	"""Purchase eFactura linked on a Purchase Order this PI is billed from."""
	if not frappe.get_meta("Purchase Order").has_field("purchase_efactura"):
		return None
	names: list[str] = []
	if po_name:
		names.append(po_name)
	for row in _child(pi, "items"):
		po = getattr(row, "purchase_order", None)
		if po and po not in names:
			names.append(po)
	for name in names:
		pef = frappe.db.get_value("Purchase Order", name, "purchase_efactura")
		if pef:
			return pef
	return None


def find_buyer_by_bill_no(pi) -> str | None:
	bill_no = (getattr(pi, "bill_no", None) or "").strip()
	if not bill_no:
		return None
	company = getattr(pi, "company", None)
	filters = {}
	if company:
		filters["company"] = company
	for buyer in frappe.get_all(
		"Purchase eFactura",
		filters=filters,
		fields=["name", "ef_series", "ef_number"],
	):
		if f"{buyer.ef_series or ''}{buyer.ef_number or ''}" == bill_no:
			return buyer.name
	return None


def validate_allocation_qtys(buyer) -> None:
	from erpnext_moldova_efactura.utils.pef_mode import is_non_livrare, is_pef_return
	from erpnext_moldova_efactura.utils.stock_alloc import (
		DN_SPEC,
		PR_SPEC,
		validate_stock_allocation_qtys,
	)

	if is_pef_return(buyer):
		validate_stock_allocation_qtys(buyer, DN_SPEC)
		return
	if is_non_livrare(buyer):
		validate_stock_allocation_qtys(buyer, PR_SPEC)
		return
	seen_details: set[str] = set()
	for row in _child(buyer, "items"):
		if not _item_linked(row):
			row.pi_detail = None
			continue
		if not row.pi_detail:
			frappe.throw(
				_("e-Factura row {0} «{1}» is linked to Purchase Invoice {2} without a Purchase Invoice Item").format(
					row.idx,
					buyer_line_name(row),
					row.purchase_invoice,
				)
			)
		if row.pi_detail in seen_details:
			frappe.throw(
				_("Purchase Invoice Item {0} is allocated more than once").format(row.pi_detail)
			)
		seen_details.add(row.pi_detail)


def _pi_detail_taken(pi_detail: str, buyer) -> bool:
	if not pi_detail:
		return False
	for row in _child(buyer, "items"):
		if row.pi_detail == pi_detail:
			return True
	buyer_name = getattr(buyer, "name", None)
	if not buyer_name or not frappe.db.has_column("Purchase eFactura Item", "pi_detail"):
		return False
	other = frappe.db.get_value(
		"Purchase eFactura Item",
		{"pi_detail": pi_detail, "parent": ["!=", buyer_name]},
		"parent",
	)
	return bool(other)


def match_pi_to_remaining(buyer, pi) -> tuple[list[dict], list[str]]:
	"""Match each PI row to one unused factura row (full qty and amount)."""
	currency = buyer.currency or pi.currency or "MDL"
	mprec = money_precision(currency)
	qprec = qty_precision()
	abs_qty = use_abs_qty_rate_match(buyer, pi)
	errors: list[str] = []
	allocs: list[dict] = []

	throw_unmapped_items(
		buyer.items or [],
		_("Map all items before linking a Purchase Invoice"),
		currency,
	)

	used_buyer: set[str] = set()
	for row in _child(buyer, "items"):
		if _item_linked(row) and row.name:
			used_buyer.add(row.name)

	used_this: set[str] = set()
	for prow in pi.items or []:
		detail = prow.name or f"pi-{prow.idx}"
		if prow.name and _pi_detail_taken(prow.name, buyer):
			errors.append(
				_("Purchase Invoice row {0} «{1}» is already linked to an e-Factura").format(
					prow.idx, pi_line_name(prow)
				)
			)
			continue

		candidate = None
		mismatch_row = None
		for brow in buyer.items or []:
			key = brow.name or f"idx-{brow.idx}"
			if key in used_buyer or key in used_this:
				continue
			if abs_qty:
				if abs(flt(remaining_qty_for_item(buyer, brow), qprec)) <= 0:
					continue
			elif flt(remaining_qty_for_item(buyer, brow), qprec) <= 0:
				continue
			if brow.item_code and prow.item_code and brow.item_code != prow.item_code:
				continue
			if lines_compatible(brow, prow, qprec, mprec, abs_qty=abs_qty):
				candidate = brow
				break
			mismatch_row = brow

		if candidate is None:
			if mismatch_row is not None:
				errors.append(
					describe_line_mismatch(mismatch_row, prow, currency, qprec, mprec, abs_qty=abs_qty)
				)
			else:
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
			continue

		key = candidate.name or f"idx-{candidate.idx}"
		used_this.add(key)
		allocs.append(
			{
				"buyer_row": candidate,
				"pi_row": prow,
				"qty": buyer_row_qty(candidate),
				"pi_detail": detail,
			}
		)

	return allocs, errors


def is_full_document_cover(buyer, allocs: list[dict]) -> bool:
	"""True when this PI plus existing links close every factura line."""
	if any(_item_linked(row) for row in _child(buyer, "items")):
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


def apply_allocations(buyer, allocs: list[dict], pi_name: str) -> None:
	for a in allocs:
		row = a["buyer_row"]
		row.purchase_invoice = pi_name
		row.pi_detail = a.get("pi_detail") or a["pi_row"].name


def delete_allocations_for_pi(pi_name: str) -> list[str]:
	"""Clear item links for a PI. Returns affected Buyer names."""
	if not pi_name or not frappe.db.has_column("Purchase eFactura Item", "purchase_invoice"):
		return []
	parents = get_buyer_names_for_pi(pi_name)
	items = frappe.get_all(
		"Purchase eFactura Item",
		filters={"purchase_invoice": pi_name, "parenttype": "Purchase eFactura"},
		pluck="name",
	)
	for name in items:
		frappe.db.set_value(
			"Purchase eFactura Item",
			name,
			{"purchase_invoice": "", "pi_detail": ""},
			update_modified=False,
		)
	return parents


def reload_summaries_and_status(buyer_name: str) -> None:
	if not buyer_name or not frappe.db.exists("Purchase eFactura", buyer_name):
		return
	buyer = frappe.get_doc("Purchase eFactura", buyer_name)
	buyer.set_status(update=False)
	_save_buyer_links(buyer)


def _save_buyer_links(doc) -> None:
	from frappe.utils import cint

	if cint(doc.docstatus) == 1:
		doc.flags.ignore_validate_update_after_submit = True
	doc.save(ignore_permissions=True)
