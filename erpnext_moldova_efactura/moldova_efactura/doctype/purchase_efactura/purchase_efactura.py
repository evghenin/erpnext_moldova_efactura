# Copyright (c) 2026, Evgheni Nemerenco and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, get_time, now_datetime

from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from erpnext_moldova_efactura.utils.api_response import (
	extract_invoices,
	invoice_status_map,
	invoice_xml,
	sfs_action_error as _sfs_action_error,
)
from erpnext_moldova_efactura.utils.buyer_status import (
	is_buyer_actionable_status,
	is_buyer_signable_status,
	status_label,
)
from erpnext_moldova_efactura.utils.buying_rate import (
	BUYING_RATE_PRECISION,
	buying_rate_for_row,
	line_amount,
)
from erpnext_moldova_efactura.utils.buying_taxes import apply_buying_taxes
from erpnext_moldova_efactura.utils.invoice_xml import parse_invoice_xml, unescape_sfs_text
from erpnext_moldova_efactura.utils.item_map import resolve_item_code, upsert_item_map
from erpnext_moldova_efactura.utils.pef_currency import (
	apply_document_amounts_from_ef,
	apply_supplier_or_default_currency,
	remap_xml_item_money,
	settings_ef_currency,
)
from erpnext_moldova_efactura.utils.pef_mode import (
	has_buying_or_stock_links,
	is_non_livrare,
	is_pef_return,
	pef_customer,
	pef_supplier,
	resolve_xml_supplier_party,
	throw_if_pef_party_idno_mismatch,
	throw_if_pi_path_blocked,
)
from erpnext_moldova_efactura.utils.pi_alloc import (
	apply_allocations,
	clear_pi_buyer_link,
	has_allocations,
	set_pi_buyer_link,
	throw_unallocated_items,
	unique_purchase_invoices,
	validate_allocation_qtys,
)
from erpnext_moldova_efactura.utils.pi_match import validate_and_match
from erpnext_moldova_efactura.utils.stock_alloc import (
	DN_SPEC,
	PR_SPEC,
	apply_stock_allocations,
	clear_stock_buyer_link,
	has_stock_allocations,
	set_stock_buyer_link,
	throw_unallocated_stock,
	unique_stock_docs,
	validate_and_match_stock,
)
from erpnext_moldova_efactura.utils.timeline import log_event, log_status_change
from erpnext_moldova_efactura.utils.uom_map import (
	apply_booking_defaults,
	apply_qty_defaults,
	apply_uom_to_buyer_row,
	compute_buyer_item_qtys,
	ensure_uom_map,
	get_item_uom_conversion,
)


def _get_purchase_efactura(name: str, ptype: str = "write"):
	if not name:
		frappe.throw(_("Missing eFactura document name."))
	doc = frappe.get_doc("Purchase eFactura", name)
	doc.check_permission(ptype)
	return doc


class PurchaseeFactura(Document):
	@property
	def supplier(self):
		"""Linked Supplier when party type is Supplier (PI/PO/PR path)."""
		return pef_supplier(self)

	@supplier.setter
	def supplier(self, value):
		if not self.supplier_party_type:
			self.supplier_party_type = "Supplier"
		if (self.supplier_party_type or "Supplier") == "Supplier":
			self.supplier_party = value

	def onload(self):
		self._unescape_xml_text_fields(persist=True)

	def validate(self):
		self._validate_unique_series_number()
		self._validate_items_immutable()
		self._unescape_xml_text_fields()
		self._lock_return_flag()
		resolve_xml_supplier_party(self)
		self._validate_allocations()
		throw_if_pef_party_idno_mismatch(self)
		if self.docstatus == 0:
			self.ef_currency = settings_ef_currency()
			apply_supplier_or_default_currency(self)
			apply_document_amounts_from_ef(self)
			self.apply_item_maps()
			self._persist_learned_maps()
		self.set_status(update=False, log=False)

	def _lock_return_flag(self):
		if self.flags.get("allow_mark_as_return") or self.is_new():
			return
		prev = self.get_doc_before_save()
		if prev and cint(prev.is_return) != cint(self.is_return):
			frappe.throw(_("Is Return cannot be changed manually"))

	def before_insert(self):
		if not self._is_system_insert_allowed():
			frappe.throw(
				_("Purchase eFactura cannot be created manually. Use Fetch from e-Factura.")
			)
		if not self.ef_currency:
			self.ef_currency = settings_ef_currency()
		apply_supplier_or_default_currency(self)
		if not self.naming_series:
			self.naming_series = "ACC-PEF-.YYYY.-"

	def _is_system_insert_allowed(self) -> bool:
		"""Only sync/tests/patches may create buyer documents."""
		if self.flags.get("from_efactura_sync"):
			return True
		if frappe.flags.in_test or frappe.flags.in_migrate or frappe.flags.in_install or frappe.flags.in_patch:
			return True
		return False

	def before_submit(self):
		if not self.supplier_party:
			if is_pef_return(self):
				frappe.throw(_("Customer is required before submit"))
			frappe.throw(_("Supplier is required before submit"))
		if not self.items:
			frappe.throw(_("Fetch invoice details from e-Factura before submit"))
		from erpnext_moldova_efactura.utils.pi_match import throw_unmapped_items

		throw_unmapped_items(self.items, _("Map all items before submit"), self.currency)
		for row in self.items:
			if not row.ef_uom:
				frappe.throw(
					_("eFactura UOM not matched for row {0} (Supplier UOM: {1})").format(
						row.idx, row.supplier_uom or _("empty")
					)
				)
			if not row.stock_uom:
				frappe.throw(_("Stock UOM is required for row {0}").format(row.idx))
			if not row.uom:
				frappe.throw(_("UOM is required for row {0}").format(row.idx))
			if not flt(row.qty):
				frappe.throw(_("Quantity is required for row {0}").format(row.idx))
		if is_pef_return(self):
			throw_unallocated_stock(
				self.items,
				_("Allocate all rows to a Delivery Note Return before submit"),
				DN_SPEC,
				self.currency,
			)
		elif is_non_livrare(self):
			throw_unallocated_stock(
				self.items,
				_("Allocate all rows to a Purchase Receipt before submit"),
				PR_SPEC,
				self.currency,
			)
		else:
			throw_unallocated_items(
				self.items,
				_("Allocate all rows to a Purchase Invoice before submit"),
				self.currency,
			)
		# Also persist on submit (covers manual grid mapping without Map Items dialog)
		self._persist_learned_maps()

	def _persist_learned_maps(self):
		"""Persist UOM map and, once Supplier is set, supplier item → Item map.

		Safe in any order: items can be mapped before Supplier; maps are written
		on the next save/submit that has both supplier and item_code.
		"""
		for row in self.items or []:
			if row.supplier_uom and row.ef_uom:
				ensure_uom_map(row.supplier_uom, row.ef_uom)
			if not (pef_supplier(self) and row.item_code):
				continue
			if not (row.supplier_item_name or row.supplier_item_code):
				continue
			upsert_item_map(
				pef_supplier(self),
				row.supplier_item_code,
				row.supplier_item_name or row.supplier_item_code,
				row.item_code,
				row.uom,
			)

	def _validate_unique_series_number(self):
		if not (self.company and self.ef_series and self.ef_number):
			return
		existing = frappe.db.exists(
			"Purchase eFactura",
			{
				"company": self.company,
				"ef_series": self.ef_series,
				"ef_number": self.ef_number,
				"name": ["!=", self.name],
			},
		)
		if existing:
			frappe.throw(
				_("Purchase eFactura {0} already exists for {1}{2}").format(
					existing, self.ef_series, self.ef_number
				)
			)

	def _item_fingerprint(self, row) -> tuple:
		"""SFS-immutable billing fields only (booking uom/qty may change before submit)."""
		return (
			cstr(row.supplier_item_code),
			cstr(row.supplier_item_name),
			cstr(row.supplier_uom),
			flt(row.ef_qty),
			flt(row.ef_rate),
			flt(row.ef_amount),
		)

	def _validate_items_immutable(self):
		"""SFS billing lines are static; UOM/qty derived fields editable before submit."""
		if self.is_new() or self.flags.get("allow_sfs_item_refresh"):
			return

		before = self.get_doc_before_save()
		if not before or not before.items:
			return

		old_fp = [self._item_fingerprint(r) for r in before.items]
		new_fp = [self._item_fingerprint(r) for r in (self.items or [])]
		if old_fp != new_fp:
			frappe.throw(_("Items from e-Factura cannot be added, removed, or changed"))

		if self.docstatus == 1:
			for row in self.items or []:
				old = next((r for r in before.items if r.idx == row.idx), None)
				if not old:
					continue
				if old.item_code != row.item_code:
					frappe.throw(_("Item mapping cannot be changed after submit"))
				if old.uom != row.uom or flt(old.qty) != flt(row.qty):
					frappe.throw(_("UOM/Quantity cannot be changed after submit"))
				if old.ef_uom != row.ef_uom:
					frappe.throw(_("eFactura UOM cannot be changed after submit"))

	_XML_TEXT_FIELDS = (
		"ef_supplier_name",
		"ef_supplier_address",
		"ef_supplier_bank_name",
		"ef_supplier_bank_account",
		"ef_customer_name",
		"ef_customer_address",
		"ef_transporter_name",
		"ef_transporter_address",
	)

	def _unescape_xml_text_fields(self, persist: bool = False):
		"""Fix SFS double-encoded entities already stored on the document."""
		for field in self._XML_TEXT_FIELDS:
			raw = getattr(self, field, None)
			if not raw:
				continue
			fixed = unescape_sfs_text(raw)
			if fixed == raw:
				continue
			setattr(self, field, fixed)
			if persist and self.name:
				self.db_set(field, fixed, update_modified=False)
		for row in self.items or []:
			for field in ("supplier_item_name", "supplier_item_code", "supplier_uom"):
				raw = getattr(row, field, None)
				if not raw:
					continue
				fixed = unescape_sfs_text(raw)
				if fixed == raw:
					continue
				setattr(row, field, fixed)
				if persist and row.name:
					frappe.db.set_value(row.doctype, row.name, field, fixed, update_modified=False)

	def _validate_allocations(self):
		validate_allocation_qtys(self)

	def save_version(self):
		from erpnext_moldova_efactura.utils.timeline import save_doc_version

		save_doc_version(self)

	def set_status(self, update: bool = True, log: bool = True):
		old_ef = self.ef_status
		self.ef_status = status_label(self.ef_status) or self.ef_status
		if cint(self.docstatus) == 0:
			self.status = "Draft"
		elif cint(self.docstatus) == 2:
			self.status = "Cancelled"
		elif cint(self.docstatus) == 1 and cint(self.is_return) == 1:
			self.status = "Return"
		else:
			self.status = "Submitted"
		if update and not self.is_new():
			self.db_set(
				{"status": self.status, "ef_status": self.ef_status},
				update_modified=False,
			)
		if log and not self.is_new():
			log_status_change(self, old_ef, self.ef_status)
		self._sync_linked_pi_fiscal()

	def on_submit(self):
		self._sync_linked_pi_fiscal()

	def on_cancel(self):
		self._sync_linked_pi_fiscal()

	def _sync_linked_pi_fiscal(self):
		from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status

		for pi_name in unique_purchase_invoices(self):
			sync_pi_fiscal_status(pi_name, buyer_override=self)

	def persist_sfs_status(self, ef_status=None):
		"""Write SFS status fields without a full save (safe after submit)."""
		if ef_status is not None:
			mapped = status_label(ef_status)
			if mapped:
				self.ef_status = mapped
		self.last_status_check = now_datetime()
		self.set_status(update=False)
		if self.is_new():
			return
		self.db_set(
			{
				"ef_status": self.ef_status,
				"status": self.status,
				"last_status_check": self.last_status_check,
			},
			update_modified=False,
		)

	def apply_item_maps(self):
		"""
		Auto-fill order per line:
		1) item_code — by supplier_item_code only if name also matches
		2) item_code — by supplier_item_name (past invoices / map / Item name)
		3) ef_uom (+ uom if empty) — Settings UOM Map, then system UOM search
		4) stock_uom / stock_qty / qty — from Item conversions when possible
		"""
		for row in self.items or []:
			if not row.item_code:
				mapped = resolve_item_code(
					pef_supplier(self), row.supplier_item_code, row.supplier_item_name
				)
				if mapped:
					row.item_code = mapped
			apply_uom_to_buyer_row(row)

	def fill_from_xml(self, xml_content: str, preserve_mapped_items: bool = True):
		"""Apply a freshly fetched SFS XML payload; XML itself is not stored.

		Item/UOM maps are kept in line order. Purchase Invoice allocations are
		cleared: SFS amounts may change, and identical supplier lines must not
		share one pi_detail.
		"""
		parsed = parse_invoice_xml(xml_content)

		if parsed.get("issue_date"):
			self.issue_date = parsed["issue_date"]
		if parsed.get("issue_time") is not None:
			self.issue_time = parsed["issue_time"]
		if parsed.get("delivery_date"):
			self.delivery_date = parsed["delivery_date"]

		self.ef_total = parsed.get("total")
		self.ef_vat_total = parsed.get("vat_total")
		self.ef_net_total = parsed.get("net_total")

		sup = parsed.get("supplier") or {}
		self.ef_supplier_idno = sup.get("idno") or self.ef_supplier_idno
		self.ef_supplier_name = sup.get("name") or self.ef_supplier_name
		self.ef_supplier_vat_id = sup.get("vat_id") or self.ef_supplier_vat_id
		self.ef_supplier_taxpayer_type = sup.get("taxpayer_type") or self.ef_supplier_taxpayer_type
		self.ef_supplier_address = sup.get("address") or self.ef_supplier_address
		self.ef_supplier_bank_account = sup.get("bank_account") or self.ef_supplier_bank_account
		self.ef_supplier_bank_name = sup.get("bank_name") or self.ef_supplier_bank_name
		self.ef_supplier_bank_code = sup.get("bank_code") or self.ef_supplier_bank_code

		buy = parsed.get("buyer") or {}
		self.ef_customer_idno = buy.get("idno") or self.ef_customer_idno
		self.ef_customer_name = buy.get("name") or self.ef_customer_name
		self.ef_customer_vat_id = buy.get("vat_id") or self.ef_customer_vat_id
		self.ef_customer_taxpayer_type = buy.get("taxpayer_type") or self.ef_customer_taxpayer_type
		self.ef_customer_address = buy.get("address") or self.ef_customer_address

		tr = parsed.get("transporter") or {}
		self.ef_transporter_idno = tr.get("idno") or self.ef_transporter_idno
		self.ef_transporter_name = tr.get("name") or self.ef_transporter_name
		self.ef_transporter_address = tr.get("address") or self.ef_transporter_address

		if parsed.get("creation_motiv") not in (None, ""):
			self.type = "Non-Transfer" if str(parsed["creation_motiv"]) == "5" else "Transfer"

		resolve_xml_supplier_party(self)

		# Keep Item/UOM maps; drop stock/buy links — amounts may change.
		linked_invoices = unique_purchase_invoices(self) if preserve_mapped_items else []
		linked_prs = unique_stock_docs(self, PR_SPEC) if preserve_mapped_items else []
		linked_dns = unique_stock_docs(self, DN_SPEC) if preserve_mapped_items else []
		existing_maps: dict[tuple, list] = {}
		if preserve_mapped_items:
			for row in self.items or []:
				key = (row.supplier_item_code or "", row.supplier_item_name or "")
				existing_maps.setdefault(key, []).append(
					{
						"item_code": row.item_code,
						"ef_uom": row.ef_uom,
						"uom": row.uom,
						"conversion_factor": row.conversion_factor,
						"ef_conversion_factor": row.ef_conversion_factor,
					}
				)

		self.set("items", [])
		for item in parsed.get("items") or []:
			key = (item.get("supplier_item_code") or "", item.get("supplier_item_name") or "")
			row = self.append("items", remap_xml_item_money(item))
			queue = existing_maps.get(key) or []
			prev = queue.pop(0) if queue else {}
			if prev.get("item_code"):
				row.item_code = prev["item_code"]
			if prev.get("ef_uom"):
				row.ef_uom = prev["ef_uom"]
			if prev.get("uom"):
				row.uom = prev["uom"]
			if flt(prev.get("conversion_factor")):
				row.conversion_factor = prev["conversion_factor"]
			if flt(prev.get("ef_conversion_factor")):
				row.ef_conversion_factor = prev["ef_conversion_factor"]

		for pi_name in linked_invoices:
			clear_pi_buyer_link(pi_name)
		for pr_name in linked_prs:
			clear_stock_buyer_link(pr_name, PR_SPEC)
		for dn_name in linked_dns:
			clear_stock_buyer_link(dn_name, DN_SPEC)

		self.ef_currency = settings_ef_currency()
		apply_supplier_or_default_currency(self, overwrite_company_default=True)
		apply_document_amounts_from_ef(self)

		self.apply_item_maps()

	def refresh_from_api(self):
		client = EFacturaAPIClient.from_settings(company=self.company)
		resp = client.get_invoices_by_seria_number(
			[{"Seria": self.ef_series, "Number": self.ef_number}]
		)
		invs = extract_invoices(resp)
		if not invs:
			frappe.throw(
				_("No invoice details returned from e-Factura for {0}{1}").format(
					self.ef_series, self.ef_number
				)
			)

		inv = invs[0]
		if inv.get("InvoiceStatus") is not None:
			self.ef_status = status_label(inv.get("InvoiceStatus"))

		# After submit, SFS remains source of truth for status only — items stay frozen
		if self.docstatus == 0:
			xml = invoice_xml(inv)
			if xml:
				self.flags.allow_sfs_item_refresh = True
				self.fill_from_xml(xml)
			self.last_status_check = now_datetime()
			self.set_status(update=False)
			self.save(ignore_permissions=True)
			return

		self.persist_sfs_status()


def _require_submitted(doc):
	if doc.docstatus != 1:
		frappe.throw(_("Submit Purchase eFactura before this action"))


def _require_not_cancelled(doc):
	if cint(doc.docstatus) == 2:
		frappe.throw(_("Cannot create documents from a cancelled Purchase eFactura"))


def _require_mapped(doc, action_label: str | None = None):
	party = pef_customer(doc) if is_pef_return(doc) else pef_supplier(doc)
	if not party:
		if is_pef_return(doc):
			frappe.throw(_("Customer is required to create {0}").format(action_label or _("document")))
		frappe.throw(_("Supplier is required to create {0}").format(action_label or _("Purchase Invoice")))
	if not doc.items:
		frappe.throw(_("No items on Purchase eFactura — fetch details first"))
	from erpnext_moldova_efactura.utils.pi_match import throw_unmapped_items

	throw_unmapped_items(
		doc.items,
		_("Map all items before creating {0}").format(action_label or _("Purchase Invoice")),
		doc.currency,
	)


def _require_actionable(doc):
	_require_submitted(doc)
	if not is_buyer_actionable_status(doc.ef_status):
		frappe.throw(
			_("eFactura can be accepted or rejected only in Sent to Buyer or Signed by Supplier status.")
		)


def _require_signable(doc):
	_require_submitted(doc)
	if not is_buyer_signable_status(doc.ef_status):
		frappe.throw(
			_("eFactura can be signed only in Sent to Buyer, Signed by Supplier, or Accepted status.")
		)


def _parse_names(names):
	if isinstance(names, str):
		names = frappe.parse_json(names)
	if not names:
		return []
	if not isinstance(names, (list, tuple)):
		names = [names]
	unique = []
	seen = set()
	for name in names:
		if not name or name in seen:
			continue
		seen.add(name)
		unique.append(name)
	return unique


def _pef_signable_skip_reason(row):
	if not row:
		return _("Not found")
	if cint(row.docstatus) != 1:
		return _("Not submitted")
	if not is_buyer_signable_status(row.ef_status):
		return _("Not eligible for signing")
	return None


def _pef_acceptable_skip_reason(row):
	if not row:
		return _("Not found")
	if cint(row.docstatus) != 1:
		return _("Not submitted")
	if not is_buyer_actionable_status(row.ef_status):
		return _("Not eligible for accepting")
	return None


def _filter_pef_names(names, skip_reason):
	names = _parse_names(names)
	if not names:
		return {"eligible": [], "skipped": []}
	rows = frappe.get_all(
		"Purchase eFactura",
		filters={"name": ["in", names]},
		fields=["name", "docstatus", "ef_status", "status"],
	)
	by_name = {row.name: row for row in rows}
	eligible = []
	skipped = []
	for name in names:
		row = by_name.get(name)
		reason = skip_reason(row)
		if reason:
			skipped.append({"name": name, "reason": reason})
			continue
		if not frappe.has_permission("Purchase eFactura", "write", name):
			skipped.append({"name": name, "reason": _("No write permission")})
			continue
		eligible.append({"name": name, "status": row.ef_status or row.status or ""})
	return {"eligible": eligible, "skipped": skipped}


@frappe.whitelist()
def fetch_details(name: str):
	doc = _get_purchase_efactura(name)
	doc.refresh_from_api()
	log_event(doc, _("Fetched invoice details from e-Factura."))
	return doc.as_dict()


@frappe.whitelist()
def accept_invoice(name: str):
	doc = _get_purchase_efactura(name)
	_require_actionable(doc)
	client = EFacturaAPIClient.from_settings(company=doc.company)
	try:
		resp = client.post_accepted_invoices([{"Seria": doc.ef_series, "Number": doc.ef_number}])
	except Exception as e:
		frappe.throw(_("e-Factura API Error: {0}").format(str(e)))
	err = _sfs_action_error(resp)
	if err:
		frappe.throw(_("e-Factura API Error: {0}").format(err))
	_refresh_status(doc)
	log_event(doc, _("Accepted invoice in e-Factura."))
	return {"status": doc.status, "ef_status": doc.ef_status}


@frappe.whitelist()
def reject_invoice(name: str, reason: str | None = None):
	doc = _get_purchase_efactura(name)
	_require_actionable(doc)
	comment = (reason or doc.rejection_reason or "").strip()
	if not comment:
		frappe.throw(_("Rejection Reason is required"))

	client = EFacturaAPIClient.from_settings(company=doc.company)
	try:
		resp = client.post_rejected_invoices(
			[
				{
					"Seria": doc.ef_series,
					"Number": doc.ef_number,
					"Comment": comment,
				}
			]
		)
	except Exception as e:
		frappe.throw(_("e-Factura API Error: {0}").format(str(e)))
	err = _sfs_action_error(resp)
	if err:
		frappe.throw(_("e-Factura API Error: {0}").format(err))

	doc.db_set("rejection_reason", comment, update_modified=False)
	doc.rejection_reason = comment
	_refresh_status(doc)
	log_event(doc, _("Rejected invoice in e-Factura: {0}").format(comment))
	return {"status": doc.status, "ef_status": doc.ef_status, "rejection_reason": comment}


@frappe.whitelist()
def download_xml(name: str):
	"""Download invoice XML from SFS (buyer)."""
	doc = _get_purchase_efactura(name, "read")
	if not doc.ef_series or not doc.ef_number:
		frappe.throw(_("eFactura Series/Number is required to download XML"))

	client = EFacturaAPIClient.from_settings(company=doc.company)
	resp = client.get_invoices_by_seria_number(
		[{"Seria": doc.ef_series, "Number": doc.ef_number}]
	)
	invs = extract_invoices(resp)
	xml = invoice_xml(invs[0]) if invs else ""
	if not xml:
		frappe.throw(_("No XML returned from e-Factura"))

	xml_content = xml.encode("utf-8") if isinstance(xml, str) else xml
	frappe.local.response.filename = f"{doc.ef_series}{doc.ef_number}.xml"
	frappe.local.response.filecontent = xml_content
	frappe.local.response.type = "download"
	frappe.local.response.content_type = "application/xml"


@frappe.whitelist()
def download_pdf(name: str):
	"""Download printable PDF from SFS (buyer, ActorRole=2)."""
	doc = _get_purchase_efactura(name, "read")
	if not doc.ef_series or not doc.ef_number:
		frappe.throw(_("eFactura Series/Number is required to download PDF"))

	client = EFacturaAPIClient.from_settings(company=doc.company)
	resp = client.get_invoices_content_for_print(
		seria_and_numbers={"Seria": doc.ef_series, "Number": doc.ef_number},
		actor_role=2,
	)
	pdf_content = (resp or {}).get("Result", {}).get("Content") or ""
	if not pdf_content.startswith(b"%PDF"):
		frappe.throw(_("e-Factura returned non-PDF content in Result.Content"))

	frappe.local.response.filename = f"{doc.ef_series}{doc.ef_number}.pdf"
	frappe.local.response.filecontent = pdf_content
	frappe.local.response.type = "download"
	frappe.local.response.content_type = "application/pdf"


@frappe.whitelist()
def filter_signable(names=None):
	"""Return selected Purchase eFactura names that can be signed by the buyer."""
	result = _filter_pef_names(names, _pef_signable_skip_reason)
	return {"signable": result["eligible"], "skipped": result["skipped"]}


@frappe.whitelist()
def filter_acceptable(names=None):
	"""Return selected Purchase eFactura names that can be accepted."""
	result = _filter_pef_names(names, _pef_acceptable_skip_reason)
	return {"acceptable": result["eligible"], "skipped": result["skipped"]}


@frappe.whitelist()
def get_xml_for_sign(name: str):
	"""Return invoice XML + C14N SHA1 hash from SFS for MoldSign (buyer)."""
	import base64
	import hashlib

	from lxml import etree

	doc = _get_purchase_efactura(name)
	_require_signable(doc)
	client = EFacturaAPIClient.from_settings(company=doc.company)
	resp = client.get_invoices_by_seria_number(
		[{"Seria": doc.ef_series, "Number": doc.ef_number}]
	)
	invs = extract_invoices(resp)
	xml = invoice_xml(invs[0]) if invs else ""
	if not xml:
		frappe.throw(_("No XML returned from e-Factura for signing"))

	xml_bytes = xml.encode("utf-8") if isinstance(xml, str) else xml
	parser = etree.XMLParser(remove_blank_text=True)
	root = etree.fromstring(xml_bytes, parser)
	canonical = etree.tostring(root, method="c14n", exclusive=False, with_comments=False)
	digest = hashlib.sha1(canonical).digest()

	return {
		"xml_base64": base64.b64encode(xml_bytes).decode("utf-8"),
		"hash_base64": base64.b64encode(digest).decode("utf-8"),
		"ef_series": doc.ef_series,
		"ef_number": doc.ef_number,
	}


@frappe.whitelist()
def process_signed_xml(name: str, signature: str, content: str):
	"""Post buyer-signed XML to SFS (ActorRole=2). Same envelope pattern as supplier."""
	import base64
	import re
	import uuid

	doc = _get_purchase_efactura(name)
	_require_signable(doc)
	if not signature:
		frappe.throw(_("Missing signature."))

	def _b64_to_text(value: str) -> str:
		raw = base64.b64decode(value)
		return raw.decode("utf-8")

	def _strip_xml_declaration(xml: str) -> str:
		return re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", xml or "", count=1, flags=re.I)

	content_xml = _strip_xml_declaration(_b64_to_text(content))
	signature_xml = _strip_xml_declaration(_b64_to_text(signature))
	wrapped = (
		'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
		"<Documents>\n"
		f'<hash Id="_{uuid.uuid4()}">Hash is incapsulated into the signature</hash>\n'
		f"{signature_xml}\n"
		f"{content_xml}\n"
		"</Documents>"
	)

	client = EFacturaAPIClient.from_settings(company=doc.company)
	client.post_invoices(
		actor_role=2,
		invoices_xml=wrapped,
		invoices_xml_status=1,
	)
	_refresh_status(doc)
	log_event(doc, _("Signed invoice in e-Factura (buyer)."))
	return {"status": doc.status, "ef_status": doc.ef_status}


def _refresh_status(doc):
	client = EFacturaAPIClient.from_settings(company=doc.company)
	resp = client.check_invoices_status([{"Seria": doc.ef_series, "Number": doc.ef_number}])
	statuses = invoice_status_map(resp)
	key = (str(doc.ef_series), str(doc.ef_number))
	doc.persist_sfs_status(statuses.get(key))


@frappe.whitelist()
def update_status(name: str):
	"""Refresh InvoiceStatus from SFS into Purchase eFactura."""
	doc = _get_purchase_efactura(name)
	if not doc.ef_series or not doc.ef_number:
		frappe.throw(_("eFactura Series/Number is required to update status"))
	_refresh_status(doc)
	return {"status": doc.status, "ef_status": doc.ef_status}


@frappe.whitelist()
def get_item_qty_fields(
	item_code=None,
	ef_uom=None,
	ef_qty=None,
	uom=None,
	conversion_factor=None,
	ef_conversion_factor=None,
):
	"""UI helper: recompute stock_uom / stock_qty / qty; default empty ef_uom from Item Stock UOM.

	Pass stored conversion factors to keep qty frozen; omit/zero to recapture from Item.
	"""
	item_code = item_code or None
	ef_uom = ef_uom or None
	uom = uom or None
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") if item_code else None
	if item_code and not ef_uom:
		ef_uom = stock_uom
	if item_code and not uom:
		uom = ef_uom or stock_uom
	if not flt(conversion_factor) and item_code and uom:
		conversion_factor = get_item_uom_conversion(item_code, uom) or 1
	if not flt(ef_conversion_factor) and item_code and ef_uom:
		ef_conversion_factor = get_item_uom_conversion(item_code, ef_uom) or 1
	conversion_factor = flt(conversion_factor) or 1
	ef_conversion_factor = flt(ef_conversion_factor) or 1
	out = compute_buyer_item_qtys(
		item_code=item_code,
		ef_uom=ef_uom,
		ef_qty=ef_qty,
		uom=uom,
		conversion_factor=conversion_factor,
		ef_conversion_factor=ef_conversion_factor,
	)
	out["ef_uom"] = ef_uom
	out["uom"] = uom
	out["conversion_factor"] = conversion_factor
	out["ef_conversion_factor"] = ef_conversion_factor
	return out


@frappe.whitelist()
def get_new_supplier_defaults(name: str | None = None, title: str | None = None, idno: str | None = None):
	"""Prefill values when creating a Supplier from Purchase eFactura."""
	if name:
		doc = _get_purchase_efactura(name, "read")
		title = title or doc.ef_supplier_name
		idno = idno or doc.ef_supplier_idno
	from erpnext_moldova_efactura.utils.party import new_supplier_defaults

	return new_supplier_defaults(title, idno)


@frappe.whitelist()
def get_new_customer_defaults(name: str | None = None, title: str | None = None, idno: str | None = None):
	"""Prefill values when creating a Customer from a Non-Transfer Purchase eFactura."""
	if name:
		doc = _get_purchase_efactura(name, "read")
		title = title or doc.ef_supplier_name
		idno = idno or doc.ef_supplier_idno
	from erpnext_moldova_efactura.utils.party import new_customer_defaults

	return new_customer_defaults(title, idno)


@frappe.whitelist()
def save_item_mappings(name: str, mappings: str | list | dict):
	"""mappings: [{idx/row_name, item_code}] or JSON string."""
	import json

	if isinstance(mappings, str):
		mappings = json.loads(mappings)

	doc = _get_purchase_efactura(name)
	if doc.docstatus != 0:
		frappe.throw(_("Item mapping is only allowed before submit"))

	by_idx = {str(m.get("idx")): m.get("item_code") for m in mappings if m.get("item_code")}
	for row in doc.items:
		item_code = by_idx.get(str(row.idx))
		if not item_code:
			continue
		row.item_code = item_code
		apply_booking_defaults(row, force=True)
	# UOM + supplier item maps: _persist_learned_maps on save (supplier may be set later)
	doc.save()
	return doc.as_dict()


def _buying_line_from_buyer(row, vat_included: bool) -> dict:
	uom_cf = flt(row.conversion_factor) or get_item_uom_conversion(row.item_code, row.uom) or 1
	if flt(row.qty):
		rate = buying_rate_for_row(row, vat_included)
	else:
		ef_rate = flt(row.rate_with_vat) if vat_included else flt(row.rate)
		ef_cf = flt(row.ef_conversion_factor) or get_item_uom_conversion(row.item_code, row.ef_uom) or 1
		rate = ef_rate * flt(ef_cf) / flt(uom_cf)
	uom = row.uom if row.uom and frappe.db.exists("UOM", row.uom) else None
	return {
		"item_code": row.item_code,
		"item_name": row.item_name or row.supplier_item_name,
		"qty": row.qty,
		"uom": uom,
		"conversion_factor": uom_cf,
		"rate": rate,
		"amount": line_amount(row, vat_included),
		"stock_uom": frappe.db.get_value("Item", row.item_code, "stock_uom"),
	}


def _append_buying_items(target, source):
	if target.meta.has_field("ignore_pricing_rule"):
		target.ignore_pricing_rule = 1
	vat_included = bool(frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate"))
	schedule = source.delivery_date or source.issue_date
	for row in source.items:
		vals = _buying_line_from_buyer(row, vat_included)
		item = target.append("items", {})
		_apply_buying_line_vals(item, vals, schedule)
	apply_buying_taxes(target, source)


def _apply_buying_line_vals(item, vals: dict, schedule) -> None:
	item.item_code = vals["item_code"]
	item.item_name = vals["item_name"]
	item.qty = vals["qty"]
	if vals["uom"]:
		item.uom = vals["uom"]
	item.conversion_factor = vals["conversion_factor"]
	item.rate = flt(vals["rate"], BUYING_RATE_PRECISION)
	if item.meta.has_field("price_list_rate"):
		item.price_list_rate = item.rate
	if item.meta.has_field("amount") and vals.get("amount"):
		item.amount = flt(vals["amount"], item.precision("amount") or 2)
	if vals["stock_uom"] and item.meta.has_field("stock_uom"):
		item.stock_uom = vals["stock_uom"]
	if schedule and item.meta.has_field("schedule_date"):
		item.schedule_date = schedule


def _prepare_mapped_buying_doc(target) -> None:
	"""Keep XML rates on the new PI/PO form.

	ERPNext onload fills empty price-list fields via set_value, which still
	calls apply_price_list even when load_after_mapping is set. Prefill so
	those handlers are no-ops, then skip apply_price_list on the client.
	"""
	currency = target.currency or "MDL"
	if target.meta.has_field("price_list_currency") and not target.price_list_currency:
		target.price_list_currency = currency
	if target.meta.has_field("conversion_rate"):
		company_cur = (
			frappe.get_cached_value("Company", target.company, "default_currency")
			if target.company
			else None
		)
		need_rate = not flt(target.conversion_rate) or (
			flt(target.conversion_rate) == 1
			and currency
			and company_cur
			and currency != company_cur
		)
		if need_rate:
			if currency and company_cur and currency != company_cur:
				from erpnext.setup.utils import get_exchange_rate

				date = (
					getattr(target, "posting_date", None)
					or getattr(target, "transaction_date", None)
					or frappe.utils.today()
				)
				target.conversion_rate = flt(get_exchange_rate(currency, company_cur, date)) or 1
			else:
				target.conversion_rate = 1
	if target.meta.has_field("plc_conversion_rate") and not flt(target.plc_conversion_rate):
		target.plc_conversion_rate = 1
	if target.meta.has_field("buying_price_list") and not target.buying_price_list:
		pl = frappe.db.get_single_value("Buying Settings", "buying_price_list")
		if pl:
			target.buying_price_list = pl
	target.set_onload("load_after_mapping", True)


def _posting_time_str(value) -> str | None:
	"""HH:MM:SS for PI Time control. Skip empty / 00:00:00 placeholders."""
	if value in (None, ""):
		return None
	try:
		t = get_time(value).replace(microsecond=0)
	except Exception:
		return None
	if t.hour == 0 and t.minute == 0 and t.second == 0:
		return None
	return t.strftime("%H:%M:%S")


def _apply_posting_from_factura(target, source) -> None:
	"""Copy e-Factura issue date onto PI posting date/time or PO transaction_date.

	frappe.new_doc() stamps Time fields with the current clock; always overwrite
	posting_time when the factura has a real issue_time.
	"""
	if not cint(frappe.db.get_single_value("eFactura Settings", "copy_date_from_factura")):
		return
	if not source.issue_date:
		return
	if target.meta.has_field("set_posting_time"):
		target.set_posting_time = 1
	if target.meta.has_field("posting_date"):
		target.posting_date = source.issue_date
	posting_time = _posting_time_str(getattr(source, "issue_time", None))
	if target.meta.has_field("posting_time") and posting_time:
		target.posting_time = posting_time
	if target.meta.has_field("transaction_date"):
		target.transaction_date = source.issue_date


def apply_factura_defaults_to_pi(pi, source) -> None:
	"""Same PI defaults as Create from Purchase eFactura (date, bill date, link, rates)."""
	if not pi or not source:
		return
	if source.issue_date:
		pi.bill_date = source.issue_date
	_apply_posting_from_factura(pi, source)
	if pi.meta.has_field("purchase_efactura"):
		pi.purchase_efactura = source.name
	_prepare_mapped_buying_doc(pi)


def apply_factura_defaults_from_po(pi, po_name: str | None = None) -> bool:
	"""If this PI comes from a PO created from Purchase eFactura, apply the same defaults."""
	from erpnext_moldova_efactura.utils.pi_alloc import find_buyer_by_po

	source_name = None
	if pi.meta.has_field("purchase_efactura") and pi.get("purchase_efactura"):
		source_name = pi.purchase_efactura
	if not source_name:
		source_name = find_buyer_by_po(pi, po_name)
	if not source_name or not frappe.db.exists("Purchase eFactura", source_name):
		return False
	apply_factura_defaults_to_pi(pi, frappe.get_doc("Purchase eFactura", source_name))
	return True


def _apply_pi_item_mapping(doc, allocs):
	"""Copy Item / UOM / qty from matched PI rows onto draft buyer lines."""
	seen: set[str] = set()
	for alloc in allocs:
		buyer_row = alloc["buyer_row"]
		pi_row = alloc["pi_row"]
		key = buyer_row.name or f"idx-{buyer_row.idx}"
		if key in seen:
			continue
		seen.add(key)
		buyer_row.item_code = pi_row.item_code
		if pi_row.uom and frappe.db.exists("UOM", pi_row.uom):
			buyer_row.uom = pi_row.uom
		apply_qty_defaults(buyer_row, force=True)
		ensure_uom_map(buyer_row.supplier_uom, buyer_row.ef_uom)
		if pef_supplier(doc) and buyer_row.item_code:
			upsert_item_map(
				pef_supplier(doc),
				buyer_row.supplier_item_code,
				buyer_row.supplier_item_name,
				buyer_row.item_code,
				buyer_row.uom,
			)


def _save_buyer_links(doc):
	if cint(doc.docstatus) == 1:
		doc.flags.ignore_validate_update_after_submit = True
	doc.save(ignore_permissions=True)


@frappe.whitelist()
def make_purchase_invoice(source_name: str, target_doc=None):
	frappe.has_permission("Purchase Invoice", "create", throw=True)
	source = _get_purchase_efactura(source_name)
	throw_if_pi_path_blocked(source)
	_require_not_cancelled(source)
	_require_mapped(source, _("Purchase Invoice"))
	if has_allocations(source):
		frappe.throw(_("e-Factura already has Purchase Invoice allocations"))

	pi = frappe.new_doc("Purchase Invoice")
	pi.company = source.company
	pi.supplier = pef_supplier(source)
	pi.currency = source.currency or "MDL"
	if source.issue_date:
		pi.bill_date = source.issue_date
	_apply_posting_from_factura(pi, source)
	_append_buying_items(pi, source)
	if pi.meta.has_field("purchase_efactura"):
		pi.purchase_efactura = source.name
	_prepare_mapped_buying_doc(pi)
	return pi


@frappe.whitelist()
def make_purchase_order(source_name: str, target_doc=None):
	from frappe.utils import today

	frappe.has_permission("Purchase Order", "create", throw=True)
	source = _get_purchase_efactura(source_name)
	throw_if_pi_path_blocked(source)
	_require_not_cancelled(source)
	_require_mapped(source, _("Purchase Order"))

	po = frappe.new_doc("Purchase Order")
	po.company = source.company
	po.supplier = pef_supplier(source)
	po.currency = source.currency or "MDL"
	po.transaction_date = today()
	schedule = source.delivery_date or source.issue_date or today()
	if po.meta.has_field("schedule_date"):
		po.schedule_date = schedule
	_apply_posting_from_factura(po, source)
	if po.meta.has_field("purchase_efactura"):
		po.purchase_efactura = source.name
	_append_buying_items(po, source)
	_prepare_mapped_buying_doc(po)
	return po


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def linkable_purchase_invoices(doctype, txt, searchfield, start, page_len, filters):
	"""Purchase Invoices that still have a line not linked to a live e-Factura."""
	from frappe.desk.reportview import get_match_cond

	filters = filters or {}
	conditions = ["`tabPurchase Invoice`.docstatus < 2"]
	values = {"txt": f"%{txt or ''}%", "start": start, "page_len": page_len}
	if filters.get("company"):
		conditions.append("`tabPurchase Invoice`.company = %(company)s")
		values["company"] = filters["company"]
	if filters.get("supplier"):
		conditions.append("`tabPurchase Invoice`.supplier = %(supplier)s")
		values["supplier"] = filters["supplier"]

	uncovered = ""
	if frappe.db.has_column("Purchase eFactura Item", "pi_detail"):
		uncovered = """
			AND EXISTS (
				SELECT 1
				FROM `tabPurchase Invoice Item` pii
				WHERE pii.parent = `tabPurchase Invoice`.name
					AND pii.parenttype = 'Purchase Invoice'
					AND ABS(IFNULL(pii.qty, 0)) > 0
					AND NOT EXISTS (
						SELECT 1
						FROM `tabPurchase eFactura Item` ei
						INNER JOIN `tabPurchase eFactura` pe ON pe.name = ei.parent
						WHERE ei.pi_detail = pii.name
							AND IFNULL(ei.pi_detail, '') != ''
							AND pe.docstatus < 2
					)
			)
		"""

	return frappe.db.sql(
		f"""
		SELECT `tabPurchase Invoice`.name, `tabPurchase Invoice`.supplier,
			`tabPurchase Invoice`.posting_date, `tabPurchase Invoice`.grand_total
		FROM `tabPurchase Invoice`
		WHERE {" AND ".join(conditions)}
			AND (
				`tabPurchase Invoice`.name LIKE %(txt)s
				OR IFNULL(`tabPurchase Invoice`.bill_no, '') LIKE %(txt)s
				OR IFNULL(`tabPurchase Invoice`.`{searchfield}`, '') LIKE %(txt)s
			)
			{uncovered}
			{get_match_cond("Purchase Invoice")}
		ORDER BY `tabPurchase Invoice`.modified DESC
		LIMIT %(page_len)s OFFSET %(start)s
		""",
		values,
	)


@frappe.whitelist()
def link_purchase_invoice(name: str, purchase_invoice: str):
	doc = _get_purchase_efactura(name)
	throw_if_pi_path_blocked(doc)
	if doc.docstatus == 2:
		frappe.throw(_("Cannot link Purchase Invoice to a cancelled e-Factura"))
	if not pef_supplier(doc):
		frappe.throw(_("Select a Supplier on the e-Factura first"))
	if not doc.items:
		frappe.throw(_("Fetch invoice details from e-Factura before linking a Purchase Invoice"))
	if not frappe.db.exists("Purchase Invoice", purchase_invoice):
		frappe.throw(_("Purchase Invoice {0} not found").format(purchase_invoice))

	pi = frappe.get_doc("Purchase Invoice", purchase_invoice)
	if doc.company and pi.company and doc.company != pi.company:
		frappe.throw(
			_("Company mismatch: e-Factura {0}, Purchase Invoice {1}").format(doc.company, pi.company)
		)
	supplier = pef_supplier(doc)
	if supplier and pi.supplier and supplier != pi.supplier:
		frappe.throw(
			_("Supplier mismatch: e-Factura {0}, Purchase Invoice {1}").format(supplier, pi.supplier)
		)

	allocs = validate_and_match(doc, pi)

	if doc.docstatus == 0:
		_apply_pi_item_mapping(doc, allocs)

	apply_allocations(doc, allocs, purchase_invoice)
	set_pi_buyer_link(purchase_invoice, doc.name)
	doc.set_status(update=False)
	_save_buyer_links(doc)

	from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status

	sync_pi_fiscal_status(purchase_invoice)
	log_event(doc, _("Linked Purchase Invoice {0}.").format(purchase_invoice))
	return doc.as_dict()


def _apply_stock_item_mapping(doc, allocs):
	seen: set[str] = set()
	for alloc in allocs:
		buyer_row = alloc["buyer_row"]
		stock_row = alloc["stock_row"]
		key = buyer_row.name or f"idx-{buyer_row.idx}"
		if key in seen:
			continue
		seen.add(key)
		buyer_row.item_code = stock_row.item_code
		if stock_row.uom and frappe.db.exists("UOM", stock_row.uom):
			buyer_row.uom = stock_row.uom
		apply_qty_defaults(buyer_row, force=True)
		ensure_uom_map(buyer_row.supplier_uom, buyer_row.ef_uom)
		if pef_supplier(doc) and buyer_row.item_code:
			upsert_item_map(
				pef_supplier(doc),
				buyer_row.supplier_item_code,
				buyer_row.supplier_item_name,
				buyer_row.item_code,
				buyer_row.uom,
			)


def _link_stock_document(name: str, target_name: str, spec):
	doc = _get_purchase_efactura(name)
	if doc.docstatus == 2:
		frappe.throw(_("Cannot link {0} to a cancelled e-Factura").format(_(spec.label)))
	if spec is PR_SPEC:
		if not is_non_livrare(doc) or is_pef_return(doc):
			frappe.throw(_("Purchase Receipt can be linked only for Non-Transfer that is not a return"))
		if not pef_supplier(doc):
			frappe.throw(_("Select a Supplier on the e-Factura first"))
	else:
		if not is_pef_return(doc):
			frappe.throw(_("Delivery Note Return can be linked only after marking the e-Factura as a return"))
		if not pef_customer(doc):
			frappe.throw(_("Select a Customer on the e-Factura first"))
	if not doc.items:
		frappe.throw(_("Fetch invoice details from e-Factura before linking {0}").format(_(spec.label)))
	if not frappe.db.exists(spec.doctype, target_name):
		frappe.throw(_("{0} {1} not found").format(_(spec.label), target_name))

	target = frappe.get_doc(spec.doctype, target_name)
	allocs = validate_and_match_stock(doc, target, spec)
	if doc.docstatus == 0:
		_apply_stock_item_mapping(doc, allocs)
	apply_stock_allocations(doc, allocs, target_name, spec)
	set_stock_buyer_link(target_name, doc.name, spec)
	doc.set_status(update=False)
	_save_buyer_links(doc)
	log_event(doc, _("Linked {0} {1}.").format(_(spec.label), target_name))
	return doc.as_dict()


@frappe.whitelist()
def link_purchase_receipt(name: str, purchase_receipt: str):
	return _link_stock_document(name, purchase_receipt, PR_SPEC)


@frappe.whitelist()
def link_delivery_note(name: str, delivery_note: str):
	return _link_stock_document(name, delivery_note, DN_SPEC)


def _require_draft_unlink(doc):
	if cint(doc.docstatus) != 0:
		frappe.throw(_("Unlink is allowed only before submitting Purchase eFactura"))


def _reverse_linked_docs(doc_name: str, doctype: str) -> list[str]:
	if not doc_name or not frappe.get_meta(doctype).has_field("purchase_efactura"):
		return []
	return frappe.get_all(
		doctype,
		filters={"purchase_efactura": doc_name, "docstatus": ["<", 2]},
		pluck="name",
	)


def _merge_unique(*groups) -> list[str]:
	names: list[str] = []
	seen: set[str] = set()
	for group in groups:
		for name in group or []:
			if name and name not in seen:
				seen.add(name)
				names.append(name)
	return names


def _clear_item_link_fields(doc, link_field: str, detail_field: str) -> None:
	for row in doc.items or []:
		setattr(row, link_field, None)
		setattr(row, detail_field, None)


@frappe.whitelist()
def unlink_purchase_invoice(name: str):
	doc = _get_purchase_efactura(name)
	_require_draft_unlink(doc)
	invoices = _merge_unique(unique_purchase_invoices(doc), _reverse_linked_docs(doc.name, "Purchase Invoice"))
	if not invoices:
		frappe.throw(_("No Purchase Invoice is linked"))
	_clear_item_link_fields(doc, "purchase_invoice", "pi_detail")
	_save_buyer_links(doc)
	from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status

	for pi_name in invoices:
		clear_pi_buyer_link(pi_name)
		if frappe.db.exists("Purchase Invoice", pi_name):
			sync_pi_fiscal_status(pi_name)
	log_event(doc, _("Unlinked Purchase Invoice."))
	return doc.as_dict()


def _unlink_stock_document(name: str, spec):
	doc = _get_purchase_efactura(name)
	_require_draft_unlink(doc)
	docs = _merge_unique(unique_stock_docs(doc, spec), _reverse_linked_docs(doc.name, spec.doctype))
	if not docs:
		frappe.throw(_("No {0} is linked").format(_(spec.label)))
	_clear_item_link_fields(doc, spec.link_field, spec.detail_field)
	_save_buyer_links(doc)
	for doc_name in docs:
		clear_stock_buyer_link(doc_name, spec)
	log_event(doc, _("Unlinked {0}.").format(_(spec.label)))
	return doc.as_dict()


@frappe.whitelist()
def unlink_purchase_receipt(name: str):
	return _unlink_stock_document(name, PR_SPEC)


@frappe.whitelist()
def unlink_delivery_note(name: str):
	return _unlink_stock_document(name, DN_SPEC)


@frappe.whitelist()
def unlink_purchase_order(name: str):
	doc = _get_purchase_efactura(name)
	_require_draft_unlink(doc)
	from erpnext_moldova_efactura.utils.po_link import get_linked_purchase_orders, remove_linked_purchase_order

	orders = _merge_unique(get_linked_purchase_orders(doc), _reverse_linked_docs(doc.name, "Purchase Order"))
	if not orders:
		frappe.throw(_("No Purchase Order is linked"))
	for po_name in orders:
		remove_linked_purchase_order(doc, po_name)
		if frappe.get_meta("Purchase Order").has_field("purchase_efactura"):
			if frappe.db.get_value("Purchase Order", po_name, "purchase_efactura") == doc.name:
				frappe.db.set_value("Purchase Order", po_name, "purchase_efactura", "", update_modified=False)
	if doc.meta.has_field("purchase_order"):
		doc.purchase_order = None
	_save_buyer_links(doc)
	log_event(doc, _("Unlinked Purchase Order."))
	return doc.as_dict()


def _set_pef_return(name: str, is_return: int):
	doc = _get_purchase_efactura(name)
	want_return = cint(is_return)
	if doc.docstatus == 2:
		frappe.throw(
			_("Cannot unmark a cancelled e-Factura as a return")
			if not want_return
			else _("Cannot mark a cancelled e-Factura as a return")
		)
	if not is_non_livrare(doc):
		frappe.throw(_("Only Non-Transfer e-Factura can be marked as a return"))
	if cint(doc.is_return) == want_return:
		frappe.throw(
			_("e-Factura is already marked as a return")
			if want_return
			else _("e-Factura is not marked as a return")
		)
	if has_buying_or_stock_links(doc):
		frappe.throw(
			_(
				"Cannot unmark as return: e-Factura is already linked to a Purchase Order, Invoice, Receipt, or Delivery Note"
			)
			if not want_return
			else _(
				"Cannot mark as return: e-Factura is already linked to a Purchase Order, Invoice, Receipt, or Delivery Note"
			)
		)
	doc.flags.allow_mark_as_return = True
	doc.is_return = want_return
	resolve_xml_supplier_party(doc)
	_save_buyer_links(doc)
	log_event(doc, _("Unmarked as return.") if not want_return else _("Marked as return."))
	return doc.as_dict()


@frappe.whitelist()
def mark_as_return(name: str):
	return _set_pef_return(name, 1)


@frappe.whitelist()
def unmark_as_return(name: str):
	return _set_pef_return(name, 0)


@frappe.whitelist()
def make_purchase_receipt(source_name: str, target_doc=None):
	frappe.has_permission("Purchase Receipt", "create", throw=True)
	source = _get_purchase_efactura(source_name)
	if not is_non_livrare(source) or is_pef_return(source):
		frappe.throw(_("Purchase Receipt can be created only for Non-Transfer that is not a return"))
	_require_not_cancelled(source)
	_require_mapped(source, _("Purchase Receipt"))
	if has_stock_allocations(source, PR_SPEC):
		frappe.throw(_("e-Factura already has Purchase Receipt allocations"))

	pr = frappe.new_doc("Purchase Receipt")
	pr.company = source.company
	pr.supplier = pef_supplier(source)
	pr.currency = source.currency or "MDL"
	_apply_posting_from_factura(pr, source)
	_append_buying_items(pr, source)
	if pr.meta.has_field("purchase_efactura"):
		pr.purchase_efactura = source.name
	_prepare_mapped_buying_doc(pr)
	return pr


@frappe.whitelist()
def make_delivery_note_return(source_name: str, target_doc=None):
	frappe.has_permission("Delivery Note", "create", throw=True)
	source = _get_purchase_efactura(source_name)
	if not is_pef_return(source):
		frappe.throw(_("Delivery Note Return can be created only after marking the e-Factura as a return"))
	_require_not_cancelled(source)
	_require_mapped(source, _("Delivery Note Return"))
	if has_stock_allocations(source, DN_SPEC):
		frappe.throw(_("e-Factura already has Delivery Note allocations"))

	dn = frappe.new_doc("Delivery Note")
	dn.company = source.company
	dn.customer = pef_customer(source)
	dn.currency = source.currency or "MDL"
	dn.is_return = 1
	_apply_posting_from_factura(dn, source)
	_append_return_items(dn, source)
	if dn.meta.has_field("purchase_efactura"):
		dn.purchase_efactura = source.name
	_prepare_mapped_buying_doc(dn)
	return dn


def _append_return_items(target, source):
	if target.meta.has_field("ignore_pricing_rule"):
		target.ignore_pricing_rule = 1
	vat_included = bool(frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate"))
	schedule = source.delivery_date or source.issue_date
	for row in source.items:
		vals = _buying_line_from_buyer(row, vat_included)
		vals["qty"] = -abs(flt(vals["qty"]))
		if vals.get("amount"):
			vals["amount"] = -abs(flt(vals["amount"]))
		item = target.append("items", {})
		_apply_buying_line_vals(item, vals, schedule)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def linkable_purchase_receipts(doctype, txt, searchfield, start, page_len, filters):
	return _linkable_stock_docs(PR_SPEC, txt, searchfield, start, page_len, filters)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def linkable_delivery_notes(doctype, txt, searchfield, start, page_len, filters):
	return _linkable_stock_docs(DN_SPEC, txt, searchfield, start, page_len, filters)


def _linkable_stock_docs(spec, txt, searchfield, start, page_len, filters):
	from frappe.desk.reportview import get_match_cond

	filters = filters or {}
	table = f"`tab{spec.doctype}`"
	item_table = f"`tab{spec.doctype} Item`"
	conditions = [f"{table}.docstatus < 2"]
	values = {"txt": f"%{txt or ''}%", "start": start, "page_len": page_len}
	if filters.get("company"):
		conditions.append(f"{table}.company = %(company)s")
		values["company"] = filters["company"]
	party = filters.get("supplier") or filters.get("customer")
	if party:
		conditions.append(f"{table}.{spec.party_field} = %(party)s")
		values["party"] = party
	if spec.require_is_return is True:
		conditions.append(f"{table}.is_return = 1")
	elif spec.require_is_return is False:
		conditions.append(f"ifnull({table}.is_return, 0) = 0")

	uncovered = ""
	if frappe.db.has_column("Purchase eFactura Item", spec.detail_field):
		uncovered = f"""
			AND EXISTS (
				SELECT 1
				FROM {item_table} si
				WHERE si.parent = {table}.name
					AND si.parenttype = '{spec.doctype}'
					AND ABS(IFNULL(si.qty, 0)) > 0
					AND NOT EXISTS (
						SELECT 1
						FROM `tabPurchase eFactura Item` ei
						INNER JOIN `tabPurchase eFactura` pe ON pe.name = ei.parent
						WHERE ei.{spec.detail_field} = si.name
							AND IFNULL(ei.{spec.detail_field}, '') != ''
							AND pe.docstatus < 2
					)
			)
		"""

	party_col = spec.party_field
	return frappe.db.sql(
		f"""
		SELECT {table}.name, {table}.{party_col},
			{table}.posting_date, {table}.grand_total
		FROM {table}
		WHERE {" AND ".join(conditions)}
			AND (
				{table}.name LIKE %(txt)s
				OR IFNULL({table}.`{searchfield}`, '') LIKE %(txt)s
			)
			{uncovered}
			{get_match_cond(spec.doctype)}
		ORDER BY {table}.modified DESC
		LIMIT %(page_len)s OFFSET %(start)s
		""",
		values,
	)


@frappe.whitelist()
def get_linked_buyers(purchase_invoice: str):
	from erpnext_moldova_efactura.utils.pi_alloc import get_buyer_names_for_pi

	if not frappe.has_permission("Purchase Invoice", "read", purchase_invoice):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not frappe.has_permission("Purchase eFactura", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return get_buyer_names_for_pi(purchase_invoice)
