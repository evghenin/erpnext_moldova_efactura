import frappe
from frappe.utils import cint

from erpnext_moldova_efactura.utils.fiscal_status import sync_pr_fiscal_status
from erpnext_moldova_efactura.utils.pi_alloc import reload_summaries_and_status
from erpnext_moldova_efactura.utils.sef_pr_alloc import (
	clear_pr_sef_link,
	delete_allocations_for_pr,
	get_sef_names_for_pr,
	throw_if_submitted_sef_blocks_pr,
	validate_existing_pr_allocations,
)
from erpnext_moldova_efactura.utils.stock_alloc import (
	PR_SPEC,
	clear_stock_buyer_link,
	delete_allocations_for_stock,
	get_buyer_names_for_stock,
	validate_existing_stock_allocations,
)


def before_submit(doc, method=None):
	buyer_name = (get_buyer_names_for_stock(doc.name, PR_SPEC) or [None])[0]
	if not buyer_name and doc.meta.has_field("purchase_efactura"):
		buyer_name = doc.get("purchase_efactura")
	if buyer_name and frappe.db.exists("Purchase eFactura", buyer_name):
		buyer = frappe.get_doc("Purchase eFactura", buyer_name)
		validate_existing_stock_allocations(buyer, doc, PR_SPEC, submit=True)

	if cint(doc.is_return):
		for sef_name in get_sef_names_for_pr(doc.name):
			if not frappe.db.exists("Sales eFactura", sef_name):
				continue
			sef = frappe.get_doc("Sales eFactura", sef_name)
			validate_existing_pr_allocations(sef, doc, submit=True)


def on_submit(doc, method=None):
	sync_pr_fiscal_status(doc.name, pr=doc)


def before_cancel(doc, method=None):
	throw_if_submitted_sef_blocks_pr(doc.name)


def on_cancel(doc, method=None):
	_clear_allocations(doc)
	sync_pr_fiscal_status(doc.name, pr=doc)


def on_trash(doc, method=None):
	throw_if_submitted_sef_blocks_pr(doc.name)
	_clear_allocations(doc)


def _clear_allocations(doc):
	throw_if_submitted_sef_blocks_pr(doc.name)
	parents = delete_allocations_for_stock(doc.name, PR_SPEC)
	clear_stock_buyer_link(doc.name, PR_SPEC)
	for name in parents:
		reload_summaries_and_status(name)

	delete_allocations_for_pr(doc.name)
	clear_pr_sef_link(doc.name)
