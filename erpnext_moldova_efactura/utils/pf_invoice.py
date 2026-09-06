"""Purchase Factura -> Purchase Invoice, with one-to-one, server-validated links."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura import (
	_get_pf,
	_identity,
	_lock_company,
)
from erpnext_moldova_efactura.utils.factura_pdf import decimal, money
from erpnext_moldova_efactura.utils.pf_amounts import tax_source


def assert_pi_available(pi, pf_name=None):
	if pi.get("purchase_efactura"):
		frappe.throw(_("This Purchase Invoice is already linked to Purchase eFactura"))
	if pi.get("purchase_factura") and pi.purchase_factura != pf_name:
		frappe.throw(_("This Purchase Invoice is already linked to another Purchase Factura"))
	if not pi.is_new():
		rows = frappe.db.sql(
			"""
			select i.parent from `tabPurchase eFactura Item` i
			join `tabPurchase eFactura` p on p.name=i.parent
			where i.purchase_invoice=%s and p.docstatus < 2 limit 1
		""",
			pi.name,
		)
		if rows:
			frappe.throw(_("This Purchase Invoice already has Purchase eFactura allocations"))
		other = frappe.db.get_value(
			"Purchase Factura",
			{"purchase_invoice": pi.name, "docstatus": ["<", 2], "name": ["!=", pf_name or ""]},
			"name",
		)
		if other:
			frappe.throw(_("This Purchase Invoice already has Purchase Factura allocations"))


def assert_no_pef(pf):
	rows = frappe.get_all(
		"Purchase eFactura",
		filters={
			"company": pf.company,
			"docstatus": ["<", 2],
			"ef_series": pf.f_series,
			"ef_number": pf.f_number,
		},
		fields=["name", "ef_supplier_idno"],
	)
	from erpnext_moldova_efactura.utils.party import normalize_idno

	if any(normalize_idno(row.ef_supplier_idno) == pf.f_supplier_idno for row in rows):
		frappe.throw(
			_(
				"The same original exists in Purchase eFactura. Reconcile the documents before creating or linking a Purchase Invoice."
			)
		)


def assert_no_pf_for_pef(pef):
	"""Called by PEF accounting actions; SFS fetch itself remains unaffected."""
	if not frappe.db.table_exists("Purchase Factura"):
		return
	_lock_company(pef.company)
	key = _identity(pef.company, pef.ef_supplier_idno, pef.ef_series, pef.ef_number)
	if frappe.db.exists("Purchase Factura", {"identity_key": key, "docstatus": ["<", 2]}):
		frappe.throw(
			_(
				"The same original is registered as Purchase Factura. Cancel/unlink the duplicate route before allocating it through SFS."
			)
		)


def assert_no_pf_for_pi(pi_name):
	if frappe.db.table_exists("Purchase Factura") and frappe.db.exists(
		"Purchase Factura", {"purchase_invoice": pi_name, "docstatus": ["<", 2]}
	):
		frappe.throw(_("Purchase Invoice is already allocated to Purchase Factura"))


def _close(a, b):
	return abs(money(a or 0) - money(b or 0)) <= decimal("0.01")


def match_invoice(pf, pi):
	"""Validate every row and total; return pairs without trusting submitted child links."""
	assert_pi_available(pi, pf.name)
	if pi.docstatus == 2 or pi.is_return:
		frappe.throw(_("Cancelled invoices and returns cannot be linked to Purchase Factura in this version"))
	if (pf.company, pf.supplier_party, pf.currency) != (pi.company, pi.supplier, pi.currency):
		frappe.throw(
			_("Purchase Factura and Purchase Invoice must have the same Company, Supplier and currency")
		)
	if len(pf.items) != len(pi.items):
		frappe.throw(_("Purchase Factura and Purchase Invoice must have the same item count"))
	for value, other, label in (
		(pf.net_total, pi.net_total, "Net Total"),
		(pf.vat_total, pi.total_taxes_and_charges, "VAT Total"),
		(pf.total, pi.grand_total, "Grand Total"),
	):
		if not _close(value, other):
			frappe.throw(
				_("{0} does not match the original factura ({1} vs {2})").format(_(label), value, other)
			)
	remaining = list(pi.items)
	pairs = []
	for row in pf.items:
		matches = [
			target
			for target in remaining
			if (
				(not row.item_code or row.item_code == target.item_code)
				and (not row.uom or row.uom == target.uom)
				and flt(row.qty, 6) == flt(target.qty, 6)
				and _close(row.net_amount, target.net_amount)
			)
		]
		if not matches:
			frappe.throw(
				_("Row {0}: no matching Purchase Invoice item, UOM, quantity and net amount").format(row.idx)
			)
		target = matches[0]
		if row.conversion_factor and flt(row.conversion_factor, 9) != flt(target.conversion_factor, 9):
			frappe.throw(_("Row {0}: UOM conversion factor changed").format(row.idx))
		pairs.append((row, target))
		remaining.remove(target)
	return pairs


def validate_pi(doc, method=None):
	"""Protect both creation and subsequent edits, including changes through the REST API."""
	previous = doc.get_doc_before_save()
	if (
		previous
		and previous.get("purchase_factura") != doc.get("purchase_factura")
		and not doc.flags.get("pf_link_action")
	):
		frappe.throw(_("Use Purchase Factura link/unlink actions to change the fiscal source"))
	if not doc.get("purchase_factura"):
		return
	pf = _get_pf(doc.purchase_factura)
	_lock_company(pf.company)
	pf.reload()
	pf.require_reviewed()
	if pf.purchase_invoice and pf.purchase_invoice != doc.name:
		frappe.throw(_("Purchase Factura already has a Purchase Invoice"))
	assert_no_pef(pf)
	match_invoice(pf, doc)
	if doc.bill_no != pf.f_series + pf.f_number or getdate(doc.bill_date) != getdate(pf.issue_date):
		frappe.throw(_("Supplier invoice number/date must match the original factura"))


def sync_pi_link(doc, method=None):
	if not doc.get("purchase_factura") or doc.docstatus == 2:
		return
	pf = _get_pf(doc.purchase_factura)
	if pf.docstatus == 1:
		pf._refresh_fiscal_status()
		return
	pf.purchase_invoice = doc.name
	for row, target in match_invoice(pf, doc):
		row.item_code, row.uom, row.qty = target.item_code, target.uom, target.qty
		row.conversion_factor, row.pi_detail = target.conversion_factor, target.name
		row.purchase_invoice = doc.name
	pf.flags.linking_pi = True
	pf.save()


def before_cancel_pi(doc, method=None):
	if doc.get("purchase_factura"):
		pf = _get_pf(doc.purchase_factura)
		if pf.docstatus == 1:
			frappe.throw(_("Cancel the submitted Purchase Factura before cancelling its Purchase Invoice"))


def clear_pi_link(doc, method=None):
	if not doc.get("purchase_factura"):
		return
	pf = _get_pf(doc.purchase_factura)
	if pf.docstatus == 0:
		pf.purchase_invoice = None
		for row in pf.items:
			row.pi_detail = None
			row.purchase_invoice = None
		pf.flags.linking_pi = True
		pf.save()


@frappe.whitelist()
def make_purchase_invoice(source_name, target_doc=None):
	frappe.has_permission("Purchase Invoice", "create", throw=True)
	pf = _get_pf(source_name)
	_lock_company(pf.company)
	pf.reload()
	pf.require_reviewed()
	if pf.purchase_invoice:
		pi = frappe.get_doc("Purchase Invoice", pf.purchase_invoice)
		pi.check_permission("read")
		return pi
	assert_no_pef(pf)
	# Do not create an additional PI if the original has already been booked manually.
	if frappe.db.exists(
		"Purchase Invoice",
		{
			"company": pf.company,
			"supplier": pf.supplier_party,
			"bill_no": pf.f_series + pf.f_number,
			"docstatus": ["<", 2],
		},
	):
		frappe.throw(
			_("A Purchase Invoice with this supplier invoice number exists. Use Link Purchase Invoice.")
		)
	from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
		_prepare_mapped_buying_doc,
	)
	from erpnext_moldova_efactura.utils.buying_taxes import apply_buying_taxes

	pi = frappe.new_doc("Purchase Invoice")
	pi.update(
		{
			"company": pf.company,
			"supplier": pf.supplier_party,
			"currency": pf.currency,
			"purchase_factura": pf.name,
			"bill_no": pf.f_series + pf.f_number,
			"bill_date": pf.issue_date,
			"ignore_pricing_rule": 1,
		}
	)
	if cint(frappe.db.get_single_value("eFactura Settings", "copy_date_from_factura")):
		pi.posting_date = pf.issue_date
	from erpnext_moldova_efactura.utils.buying_rate import buying_rate_for_row

	vat_included = cint(frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate"))
	for row in pf.items:
		if not row.item_code or not row.uom or flt(row.qty) <= 0:
			frappe.throw(_("Row {0}: map an ERP Item, UOM and quantity first").format(row.idx))
		pi.append(
			"items",
			{
				"item_code": row.item_code,
				"description": frappe.utils.escape_html(row.supplier_item_name),
				"uom": row.uom,
				"qty": row.qty,
				"conversion_factor": row.conversion_factor,
				"rate": flt(buying_rate_for_row(row, vat_included), 6),
			},
		)
	company_currency = frappe.get_cached_value("Company", pf.company, "default_currency")
	if pf.currency == company_currency:
		pi.conversion_rate = 1
	elif pf.f_currency == company_currency:
		pi.conversion_rate = pf.f_conversion_rate
	else:
		from erpnext.setup.utils import get_exchange_rate

		pi.conversion_rate = get_exchange_rate(
			pf.currency, company_currency, pi.posting_date or pf.issue_date
		)
	if flt(pi.conversion_rate) <= 0:
		frappe.throw(_("Set an exchange rate from Purchase Invoice currency to Company currency"))
	_prepare_mapped_buying_doc(pi)
	pi.set_missing_values()
	# Reuse configured purchasing tax rules, with the original VAT rates available to the helper.
	pi.set("taxes", [])
	apply_buying_taxes(pi, tax_source(pf))
	pi.calculate_taxes_and_totals()
	pi.set_onload("load_after_mapping", True)
	return pi


@frappe.whitelist()
def link_purchase_invoice(name, purchase_invoice):
	pf = _get_pf(name)
	_lock_company(pf.company)
	pf.reload()
	pf.require_reviewed()
	if pf.docstatus != 0 or (pf.purchase_invoice and pf.purchase_invoice != purchase_invoice):
		frappe.throw(_("Only an unallocated draft Purchase Factura can be linked"))
	pi = frappe.get_doc("Purchase Invoice", purchase_invoice)
	pi.check_permission("write")
	assert_no_pef(pf)
	pairs = match_invoice(pf, pi)
	if pi.bill_no and pi.bill_no != pf.f_series + pf.f_number:
		frappe.throw(_("Supplier invoice number differs from the factura"))
	if pi.bill_date and getdate(pi.bill_date) != getdate(pf.issue_date):
		frappe.throw(_("Supplier invoice date differs from the factura"))
	if not pi.bill_no or not pi.bill_date:
		frappe.throw(_("Fill Supplier Invoice No and Date on the Purchase Invoice before linking"))
	frappe.db.set_value("Purchase Invoice", pi.name, "purchase_factura", pf.name)
	pf.purchase_invoice = pi.name
	for row, target in pairs:
		row.item_code, row.uom, row.qty = target.item_code, target.uom, target.qty
		row.conversion_factor, row.pi_detail = target.conversion_factor, target.name
		row.purchase_invoice = pi.name
	pf.flags.linking_pi = True
	pf.save()
	return pf.name


@frappe.whitelist()
def unlink_purchase_invoice(name):
	pf = _get_pf(name)
	_lock_company(pf.company)
	pf.reload()
	if pf.docstatus != 0:
		frappe.throw(_("Only draft Purchase Factura links can be removed"))
	pi_name = pf.purchase_invoice
	if pi_name:
		frappe.get_doc("Purchase Invoice", pi_name).check_permission("write")
		frappe.db.set_value("Purchase Invoice", pi_name, "purchase_factura", None)
	pf.purchase_invoice = None
	for row in pf.items:
		row.pi_detail = None
		row.purchase_invoice = None
	pf.flags.linking_pi = True
	pf.save()
	from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status

	sync_pi_fiscal_status(pi_name)
	return pf.name
