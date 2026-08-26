import frappe
from frappe.utils import cint, flt

from erpnext_moldova_efactura.utils.pi_alloc import (
	apply_allocations,
	clear_pi_buyer_link,
	delete_allocations_for_pi,
	find_source_buyer,
	get_buyer_name_for_pi,
	match_pi_to_remaining,
	reload_summaries_and_status,
	set_pi_buyer_link,
)
from erpnext_moldova_efactura.utils.pi_match import validate_existing_allocations
from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status


def before_insert(doc, method=None):
	# Mapper/client already applied defaults; don't overwrite a user-edited posting date.
	if cint(doc.get("set_posting_time")) and doc.meta.has_field("purchase_efactura") and doc.get(
		"purchase_efactura"
	):
		return
	from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
		apply_factura_defaults_from_po,
	)

	apply_factura_defaults_from_po(doc)


def before_submit(doc, method=None):
	buyer_name = get_buyer_name_for_pi(doc)
	if not buyer_name or not frappe.db.exists("Purchase eFactura", buyer_name):
		return
	buyer = frappe.get_doc("Purchase eFactura", buyer_name)
	validate_existing_allocations(buyer, doc, submit=True)


def on_update(doc, method=None):
	if int(doc.docstatus or 0) == 2:
		return
	_try_auto_allocate(doc)
	buyer_name = get_buyer_name_for_pi(doc)
	if buyer_name and frappe.db.exists("Purchase eFactura", buyer_name):
		buyer = frappe.get_doc("Purchase eFactura", buyer_name)
		buyer.set_status(update=True)
	sync_pi_fiscal_status(doc.name, pi=doc)


def on_cancel(doc, method=None):
	_clear_allocations(doc)
	sync_pi_fiscal_status(doc.name, pi=doc)


def on_trash(doc, method=None):
	_clear_allocations(doc)


def _clear_allocations(doc):
	parents = delete_allocations_for_pi(doc.name)
	clear_pi_buyer_link(doc.name)
	for name in parents:
		reload_summaries_and_status(name)


def _try_auto_allocate(doc):
	if get_buyer_name_for_pi(doc):
		return
	buyer_name = find_source_buyer(doc)
	if not buyer_name or not frappe.db.exists("Purchase eFactura", buyer_name):
		return
	buyer = frappe.get_doc("Purchase eFactura", buyer_name)
	if buyer.docstatus == 2:
		return
	from erpnext_moldova_efactura.utils.pef_mode import pef_supplier

	supplier = pef_supplier(buyer)
	if supplier and doc.supplier and supplier != doc.supplier:
		return
	if buyer.company and doc.company and buyer.company != doc.company:
		return
	explicit = bool(doc.meta.has_field("purchase_efactura") and doc.get("purchase_efactura") == buyer_name)
	if not explicit and len(buyer.items or []) != len(doc.items or []):
		return
	allocs, errors = match_pi_to_remaining(buyer, doc)
	if (errors or not allocs) and explicit:
		allocs = _allocs_by_row_order(buyer, doc)
		errors = []
	if errors or not allocs:
		return
	apply_allocations(buyer, allocs, doc.name)
	set_pi_buyer_link(doc.name, buyer.name)
	buyer.set_status(update=False)
	from erpnext_moldova_efactura.utils.pi_alloc import _save_buyer_links

	_save_buyer_links(buyer)


def _allocs_by_row_order(buyer, pi) -> list[dict]:
	from erpnext_moldova_efactura.utils.pi_match import buyer_row_qty, eq, qty_precision

	buyer_items = list(buyer.items or [])
	pi_items = list(pi.items or [])
	if not buyer_items or len(buyer_items) != len(pi_items):
		return []
	qprec = qty_precision()
	allocs = []
	for brow, prow in zip(buyer_items, pi_items):
		if not prow.name:
			return []
		need = buyer_row_qty(brow)
		if not eq(need, flt(prow.qty), qprec):
			return []
		allocs.append(
			{
				"buyer_row": brow,
				"pi_row": prow,
				"qty": need,
				"pi_detail": prow.name,
			}
		)
	return allocs
