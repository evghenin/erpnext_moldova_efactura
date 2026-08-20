"""Link Purchase eFactura to one or more Purchase Orders and allocate PI qty."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_moldova_efactura.utils.uom_map import get_item_uom_conversion


def get_linked_purchase_orders(buyer) -> list[str]:
	names: list[str] = []
	seen: set[str] = set()
	for row in buyer.get("purchase_orders") or []:
		po = row.purchase_order
		if po and po not in seen:
			seen.add(po)
			names.append(po)
	if not names and buyer.get("purchase_order"):
		names.append(buyer.purchase_order)
	return names


def sync_purchase_order_field(buyer) -> None:
	"""Keep legacy Link in sync with the first child row (list filter)."""
	pos = get_linked_purchase_orders(buyer)
	buyer.purchase_order = pos[0] if pos else None


def add_linked_purchase_order(buyer, po_name: str) -> bool:
	"""Append PO if missing. Returns True when a row was added."""
	if po_name in get_linked_purchase_orders(buyer):
		return False
	buyer.append("purchase_orders", {"purchase_order": po_name})
	sync_purchase_order_field(buyer)
	return True


def remove_linked_purchase_order(buyer, po_name: str) -> bool:
	rows = [r for r in (buyer.get("purchase_orders") or []) if r.purchase_order == po_name]
	if not rows:
		return False
	for row in rows:
		buyer.remove(row)
	sync_purchase_order_field(buyer)
	return True


def get_po_item_billed_qty(po_item_name: str) -> float:
	if not po_item_name:
		return 0.0
	return flt(
		frappe.db.sql(
			"""
			SELECT SUM(qty)
			FROM `tabPurchase Invoice Item`
			WHERE docstatus = 1 AND po_detail = %s
			""",
			po_item_name,
		)[0][0]
	)


def po_item_remaining_qty(po_item) -> float:
	if po_item.get("name") and frappe.db.exists("Purchase Order Item", po_item.name):
		billed = get_po_item_billed_qty(po_item.name)
	else:
		billed = flt(po_item.get("billed_qty"))
	return max(flt(po_item.qty) - billed, 0.0)


def remaining_in_buyer_uom(
	po_item, buyer_uom: str | None, item_code: str, buyer_conversion_factor=None
) -> float:
	remaining = po_item_remaining_qty(po_item)
	po_uom = po_item.uom
	if not remaining or not buyer_uom or not po_uom or po_uom == buyer_uom:
		return remaining
	po_cf = flt(po_item.get("conversion_factor")) or get_item_uom_conversion(item_code, po_uom) or 1.0
	buyer_cf = flt(buyer_conversion_factor) or get_item_uom_conversion(item_code, buyer_uom) or 1.0
	if not buyer_cf:
		return remaining
	return remaining * flt(po_cf) / flt(buyer_cf)


def _po_item_defaults(po_item) -> dict:
	out = {}
	if po_item.get("warehouse"):
		out["warehouse"] = po_item.warehouse
	if po_item.get("expense_account"):
		out["expense_account"] = po_item.expense_account
	if po_item.get("cost_center"):
		out["cost_center"] = po_item.cost_center
	return out


def allocate_buyer_rows_to_po(buyer, po_names: list[str]) -> list[dict]:
	"""Split each buyer line across remaining PO qty. Throws if qty cannot be covered."""
	qprec = 3
	try:
		from erpnext_moldova_efactura.utils.pi_match import qty_precision

		qprec = qty_precision()
	except Exception:
		pass

	po_docs = []
	for po_name in po_names:
		status = frappe.db.get_value("Purchase Order", po_name, "docstatus")
		if cint(status) != 1:
			frappe.throw(_("Submit Purchase Order {0} before creating Purchase Invoice").format(po_name))
		po_docs.append(frappe.get_doc("Purchase Order", po_name))

	remaining: dict[str, float] = {}
	po_items_by_name: dict[str, object] = {}
	pool_by_item: dict[str, list[tuple[str, object]]] = defaultdict(list)
	for po in po_docs:
		for item in po.items or []:
			if not item.item_code:
				continue
			left = po_item_remaining_qty(item)
			if left <= 0:
				continue
			remaining[item.name] = left
			po_items_by_name[item.name] = item
			pool_by_item[item.item_code].append((po.name, item))

	allocations: list[dict] = []
	shortages: list[str] = []

	for brow in buyer.items or []:
		need = flt(brow.qty) if flt(brow.qty) else flt(brow.ef_qty)
		item_code = brow.item_code
		if need <= 0 or not item_code:
			continue
		left = need
		pool = pool_by_item.get(item_code) or []
		for po_name, po_item in pool:
			if left <= 0:
				break
			avail_po = remaining.get(po_item.name) or 0
			if avail_po <= 0:
				continue
			avail_buyer = remaining_in_buyer_uom(
				frappe._dict(
					{
						"qty": avail_po,
						"billed_qty": 0,
						"uom": po_item.uom,
						"conversion_factor": po_item.conversion_factor,
					}
				),
				brow.uom,
				item_code,
				buyer_conversion_factor=brow.conversion_factor,
			)
			# remaining_in_buyer_uom uses qty - billed; we pass billed=0 and qty=avail_po
			take = min(left, avail_buyer)
			if flt(take, qprec) <= 0:
				continue
			# consume PO remaining in PO uom
			if avail_buyer:
				take_po = avail_po * (take / avail_buyer)
			else:
				take_po = 0
			remaining[po_item.name] = max(avail_po - take_po, 0)
			left = flt(left - take, qprec)
			chunk = {
				"buyer_row": brow,
				"qty": take,
				"purchase_order": po_name,
				"po_detail": po_item.name,
			}
			chunk.update(_po_item_defaults(po_item))
			allocations.append(chunk)

		if flt(left, qprec) > 0:
			avail_notes = []
			for po_name, po_item in pool:
				avail_notes.append(
					_("{0}: {1} {2}").format(
						po_name,
						flt(remaining.get(po_item.name) or 0, qprec),
						po_item.uom or "",
					)
				)
			if not avail_notes:
				for po_name in po_names:
					avail_notes.append(_("{0}: no remaining qty for {1}").format(po_name, item_code))
			shortages.append(
				_(
					"e-Factura row {0} «{1}»: qty {2} exceeds remaining Purchase Order qty ({3}). "
					"Increase qty on a linked Purchase Order or Link another Purchase Order, then create the invoice again."
				).format(
					brow.idx,
					brow.item_name or brow.supplier_item_name or item_code,
					flt(need, qprec),
					"; ".join(avail_notes) if avail_notes else _("none"),
				)
			)

	if shortages:
		items = "".join(f"<li>{frappe.utils.cstr(s)}</li>" for s in shortages)
		frappe.throw(
			_("Cannot create Purchase Invoice from e-Factura:") + f"<ul>{items}</ul>",
			title=_("Purchase Order qty is not enough"),
		)
	return allocations


def unlink_purchase_order_from_all_buyers(po_name: str) -> None:
	"""Remove a Purchase Order from every Purchase eFactura child table."""
	if not po_name:
		return
	parents = frappe.get_all(
		"Purchase eFactura Purchase Order",
		filters={"purchase_order": po_name},
		pluck="parent",
	)
	legacy = []
	if frappe.db.has_column("Purchase eFactura", "purchase_order"):
		legacy = frappe.get_all("Purchase eFactura", filters={"purchase_order": po_name}, pluck="name")
	if frappe.db.exists("DocType", "Purchase eFactura Purchase Order"):
		frappe.db.delete("Purchase eFactura Purchase Order", {"purchase_order": po_name})
	for parent in set(parents) | set(legacy):
		if not frappe.db.exists("Purchase eFactura", parent):
			continue
		first = None
		if frappe.db.exists("DocType", "Purchase eFactura Purchase Order"):
			first = frappe.db.get_value(
				"Purchase eFactura Purchase Order",
				{"parent": parent},
				"purchase_order",
				order_by="idx asc",
			)
		frappe.db.set_value("Purchase eFactura", parent, "purchase_order", first, update_modified=False)
