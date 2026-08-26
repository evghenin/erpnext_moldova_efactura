import frappe

from erpnext_moldova_efactura.utils.pi_alloc import reload_summaries_and_status
from erpnext_moldova_efactura.utils.stock_alloc import (
	DN_SPEC,
	clear_stock_buyer_link,
	delete_allocations_for_stock,
	get_buyer_names_for_stock,
	validate_existing_stock_allocations,
)


def before_submit(doc, method=None):
	buyer_name = (get_buyer_names_for_stock(doc.name, DN_SPEC) or [None])[0]
	if not buyer_name and doc.meta.has_field("purchase_efactura"):
		buyer_name = doc.get("purchase_efactura")
	if not buyer_name or not frappe.db.exists("Purchase eFactura", buyer_name):
		return
	buyer = frappe.get_doc("Purchase eFactura", buyer_name)
	validate_existing_stock_allocations(buyer, doc, DN_SPEC, submit=True)


def on_cancel(doc, method=None):
	_clear_allocations(doc)


def on_trash(doc, method=None):
	_clear_allocations(doc)


def _clear_allocations(doc):
	parents = delete_allocations_for_stock(doc.name, DN_SPEC)
	clear_stock_buyer_link(doc.name, DN_SPEC)
	for name in parents:
		reload_summaries_and_status(name)
