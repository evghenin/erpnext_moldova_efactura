"""Purchase Factura -> Purchase Invoice, with one-to-one, server-validated links."""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura import (
	_get_pf,
	_identity,
	_lock_company,
)
from erpnext_moldova_efactura.utils.buying_taxes import ensure_purchase_tax_row_defaults
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


ROUND_OFF_LIMIT = decimal("0.20")


def _close(a, b, tol=None):
	limit = money(tol) if tol is not None else decimal("0.01")
	return abs(money(a or 0) - money(b or 0)) <= limit


def _uom_eq(a, b):
	return not a or not b or str(a).strip().casefold() == str(b).strip().casefold()


def _line_identity(row, target):
	return (
		(not row.item_code or row.item_code == target.item_code)
		and _uom_eq(row.uom, target.uom)
		and flt(row.qty, 6) == flt(target.qty, 6)
	)


def _item_uom_ok(row, target):
	return (not row.item_code or row.item_code == target.item_code) and _uom_eq(row.uom, target.uom)


def _pi_line_net(target):
	return money(getattr(target, "net_amount", None) or getattr(target, "amount", None) or 0)


def _cover_pf_pi_items(row, candidates):
	if not candidates:
		return None
	need_qty = flt(row.qty, 6)
	need_net = money(row.net_amount or 0)
	need_amt = money(row.amount or 0)
	n = len(candidates)

	def _ok(combo):
		qty = sum(flt(candidates[i].qty, 6) for i in combo)
		net = sum(_pi_line_net(candidates[i]) for i in combo)
		if flt(qty, 6) != need_qty:
			return False
		return _close(net, need_net, ROUND_OFF_LIMIT) or _close(net, need_amt, ROUND_OFF_LIMIT)

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


def _line_amounts_ok(row, target):
	"""qty * rate on the PI can differ from the printed line; document round-off books the rest."""
	pi_amount = getattr(target, "amount", None) or 0
	return (
		_close(row.net_amount, getattr(target, "net_amount", None) or 0, ROUND_OFF_LIMIT)
		or _close(row.amount, pi_amount, ROUND_OFF_LIMIT)
		or _close(row.net_amount, pi_amount, ROUND_OFF_LIMIT)
	)


def _signed_tax_amount(tax):
	amount = money(tax.tax_amount or 0)
	if (getattr(tax, "add_deduct_tax", None) or "Add") == "Deduct" and amount > 0:
		return -amount
	return amount


def _vat_excluding_round_off(pi):
	account = frappe.get_cached_value("Company", pi.company, "round_off_account")
	total = money(0)
	for tax in pi.get("taxes") or []:
		if account and tax.account_head == account:
			continue
		total += _signed_tax_amount(tax)
	return total


def apply_factura_round_off(pi, pf):
	"""Book qty*rate vs printed factura payable on Company Round Off Account."""
	account = frappe.get_cached_value("Company", pi.company, "round_off_account")
	cost_center = frappe.get_cached_value("Company", pi.company, "round_off_cost_center")
	if not cost_center:
		cost_center = frappe.get_cached_value("Company", pi.company, "cost_center")
	for tax in list(pi.get("taxes") or []):
		if account and tax.account_head == account:
			pi.remove(tax)
	ensure_purchase_tax_row_defaults(pi)
	if hasattr(pi, "calculate_taxes_and_totals"):
		pi.calculate_taxes_and_totals()
	delta = money(pf.total or 0) - money(pi.grand_total or 0)
	if abs(delta) <= decimal("0.01"):
		return
	if abs(delta) > ROUND_OFF_LIMIT:
		frappe.throw(
			_("Purchase Invoice differs from the factura by {0}; that is more than rounding").format(delta)
		)
	if not account:
		frappe.throw(
			_("Set Round Off Account on Company {0} to book the factura rounding difference").format(pi.company)
		)
	row = pi.append("taxes", {})
	row.charge_type = "Actual"
	row.account_head = account
	row.description = _("Factura rounding")
	if row.meta.has_field("add_deduct_tax"):
		if delta < 0:
			row.add_deduct_tax = "Deduct"
			row.tax_amount = float(-delta)
		else:
			row.add_deduct_tax = "Add"
			row.tax_amount = float(delta)
	else:
		row.tax_amount = float(delta)
	if row.meta.has_field("category"):
		row.category = "Total"
	if row.meta.has_field("included_in_print_rate"):
		row.included_in_print_rate = 0
	if cost_center and row.meta.has_field("cost_center"):
		row.cost_center = cost_center
	pi.calculate_taxes_and_totals()


def match_invoice(pf, pi):
	"""Validate every row and total; return (pf_row, pi_row) pairs. Several PI rows may cover one PF row."""
	assert_pi_available(pi, pf.name)
	if pi.docstatus == 2 or pi.is_return:
		frappe.throw(_("Cancelled invoices and returns cannot be linked to Purchase Factura in this version"))
	if (pf.company, pf.supplier_party, pf.currency) != (pi.company, pi.supplier, pi.currency):
		frappe.throw(
			_("Purchase Factura and Purchase Invoice must have the same Company, Supplier and currency")
		)
	if not _close(pf.total, pi.grand_total):
		frappe.throw(
			_("Grand Total does not match the original factura ({0} vs {1})").format(pf.total, pi.grand_total)
		)
	if not _close(pf.vat_total, _vat_excluding_round_off(pi)):
		frappe.throw(
			_("VAT Total does not match the original factura ({0} vs {1})").format(
				pf.vat_total, _vat_excluding_round_off(pi)
			)
		)
	remaining = list(pi.items)
	pairs = []
	for row in pf.items:
		candidates = [target for target in remaining if _item_uom_ok(row, target)]
		chosen = _cover_pf_pi_items(row, candidates)
		if not chosen:
			frappe.throw(
				_(
					"Row {0}: no matching Purchase Invoice item, UOM, quantity and net amount ({1} {2}, net {3})"
				).format(row.idx, row.qty, row.uom or "", row.net_amount)
			)
		for target in chosen:
			if row.conversion_factor and flt(row.conversion_factor, 9) != flt(target.conversion_factor or 0, 9):
				frappe.throw(_("Row {0}: UOM conversion factor changed").format(row.idx))
			pairs.append((row, target))
			remaining.remove(target)
	if remaining:
		frappe.throw(
			_("Purchase Invoice has {0} extra item(s) that do not belong to this factura").format(len(remaining))
		)
	return pairs


def _write_pf_pi_pairs(pf, pi_name, pairs):
	from erpnext_moldova_efactura.utils.pi_match import join_child_names

	groups = defaultdict(list)
	for row, target in pairs:
		groups[id(row)].append(target)
	for row in pf.items:
		targets = groups.get(id(row))
		if not targets:
			continue
		first = targets[0]
		if first.item_code:
			row.item_code = first.item_code
		if first.uom:
			row.uom = first.uom
		if len(targets) == 1:
			row.qty = first.qty
			if first.conversion_factor:
				row.conversion_factor = first.conversion_factor
		row.pi_detail = join_child_names(t.name for t in targets)
		row.purchase_invoice = pi_name


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
	if not pf.supplier_party:
		frappe.throw(_("Select a Supplier before creating or linking a Purchase Invoice"))
	if pf.purchase_invoice and pf.purchase_invoice != doc.name:
		frappe.throw(_("Purchase Factura already has a Purchase Invoice"))
	assert_no_pef(pf)
	apply_factura_round_off(doc, pf)
	match_invoice(pf, doc)


def sync_pi_link(doc, method=None):
	if not doc.get("purchase_factura") or doc.docstatus == 2:
		return
	pf = _get_pf(doc.purchase_factura)
	if pf.docstatus == 1:
		pf._refresh_fiscal_status()
		return
	pf.purchase_invoice = doc.name
	_write_pf_pi_pairs(pf, doc.name, match_invoice(pf, doc))
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
	if not pf.supplier_party:
		frappe.throw(_("Select a Supplier before creating or linking a Purchase Invoice"))
	if pf.purchase_invoice:
		pi = frappe.get_doc("Purchase Invoice", pf.purchase_invoice)
		pi.check_permission("read")
		return pi
	assert_no_pef(pf)
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
	apply_factura_round_off(pi, pf)
	pi.set_onload("load_after_mapping", True)
	return pi


@frappe.whitelist()
def make_purchase_order(source_name, target_doc=None):
	from frappe.utils import today

	frappe.has_permission("Purchase Order", "create", throw=True)
	pf = _get_pf(source_name)
	_lock_company(pf.company)
	pf.reload()
	if not pf.supplier_party:
		frappe.throw(_("Select a Supplier before creating a Purchase Order"))
	if flt(pf.total) < 0:
		frappe.throw(_("Purchase Order cannot be created from a Purchase Factura with a negative total"))

	from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
		_apply_buying_line_vals,
		_buying_line_from_buyer,
		_apply_posting_from_factura,
		_prepare_mapped_buying_doc,
	)
	from erpnext_moldova_efactura.utils.buying_taxes import apply_buying_taxes

	po = frappe.new_doc("Purchase Order")
	po.company = pf.company
	po.supplier = pf.supplier_party
	po.currency = pf.currency
	po.transaction_date = today()
	schedule = pf.delivery_date or pf.issue_date or today()
	if po.meta.has_field("schedule_date"):
		po.schedule_date = schedule
	_apply_posting_from_factura(po, pf)
	if po.meta.has_field("ignore_pricing_rule"):
		po.ignore_pricing_rule = 1
	vat_included = bool(frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate"))
	for row in pf.items:
		if not row.item_code or not row.uom or flt(row.qty) <= 0:
			frappe.throw(_("Row {0}: map an ERP Item, UOM and quantity first").format(row.idx))
		item = po.append("items", {})
		_apply_buying_line_vals(item, _buying_line_from_buyer(row, vat_included), schedule)
	apply_buying_taxes(po, tax_source(pf))
	_prepare_mapped_buying_doc(po)
	return po


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def linkable_purchase_invoices(doctype, txt, searchfield, start, page_len, filters):
	"""Draft or submitted invoices for this supplier that are not already allocated."""
	from frappe.desk.reportview import get_match_cond

	filters = filters or {}
	conditions = [
		"`tabPurchase Invoice`.docstatus < 2",
		"`tabPurchase Invoice`.is_return = 0",
	]
	values = {"txt": f"%{txt or ''}%", "start": start, "page_len": page_len}
	if filters.get("company"):
		conditions.append("`tabPurchase Invoice`.company = %(company)s")
		values["company"] = filters["company"]
	if filters.get("supplier"):
		conditions.append("`tabPurchase Invoice`.supplier = %(supplier)s")
		values["supplier"] = filters["supplier"]
	if frappe.db.has_column("Purchase Invoice", "purchase_factura"):
		conditions.append("IFNULL(`tabPurchase Invoice`.purchase_factura, '') = ''")
	if frappe.db.has_column("Purchase Invoice", "purchase_efactura"):
		conditions.append("IFNULL(`tabPurchase Invoice`.purchase_efactura, '') = ''")

	from erpnext_moldova_efactura.utils.si_link import format_invoice_link_dropdown

	return format_invoice_link_dropdown(
		frappe.db.sql(
			f"""
			SELECT `tabPurchase Invoice`.name, `tabPurchase Invoice`.posting_date,
				`tabPurchase Invoice`.supplier, `tabPurchase Invoice`.bill_no,
				`tabPurchase Invoice`.grand_total
			FROM `tabPurchase Invoice`
			WHERE {" AND ".join(conditions)}
				AND (
					`tabPurchase Invoice`.name LIKE %(txt)s
					OR IFNULL(`tabPurchase Invoice`.bill_no, '') LIKE %(txt)s
					OR IFNULL(`tabPurchase Invoice`.`{searchfield}`, '') LIKE %(txt)s
				)
				{get_match_cond("Purchase Invoice")}
			ORDER BY `tabPurchase Invoice`.modified DESC
			LIMIT %(page_len)s OFFSET %(start)s
			""",
			values,
		)
	)


@frappe.whitelist()
def link_purchase_invoice(name, purchase_invoice):
	pf = _get_pf(name)
	_lock_company(pf.company)
	pf.reload()
	if not pf.supplier_party:
		frappe.throw(_("Select a Supplier before creating or linking a Purchase Invoice"))
	if pf.docstatus != 0 or (pf.purchase_invoice and pf.purchase_invoice != purchase_invoice):
		frappe.throw(_("Only an unallocated draft Purchase Factura can be linked"))
	pi = frappe.get_doc("Purchase Invoice", purchase_invoice)
	pi.check_permission("write")
	assert_no_pef(pf)
	if cint(pi.docstatus) == 0:
		apply_factura_round_off(pi, pf)
		pairs = match_invoice(pf, pi)
		pi.flags.pf_link_action = True
		pi.purchase_factura = pf.name
		pi.save()
	else:
		# Recalculating taxes on a submitted PI can rewrite Outstanding Amount.
		pairs = match_invoice(pf, pi)
		frappe.db.set_value("Purchase Invoice", pi.name, "purchase_factura", pf.name)
		pi.purchase_factura = pf.name
	from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status

	sync_pi_fiscal_status(pi.name)
	pf.purchase_invoice = pi.name
	_write_pf_pi_pairs(pf, pi.name, pairs)
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
