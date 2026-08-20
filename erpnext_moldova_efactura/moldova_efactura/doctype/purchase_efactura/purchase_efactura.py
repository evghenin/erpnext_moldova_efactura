# Copyright (c) 2026, Evgheni Nemerenco and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, get_time, now_datetime

from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from erpnext_moldova_efactura.utils.api_response import as_list, extract_invoices, invoice_status_map, invoice_xml
from erpnext_moldova_efactura.utils.buyer_status import BUYER_ACTIONABLE_STATUSES, compose_buyer_status
from erpnext_moldova_efactura.utils.buying_rate import (
	BUYING_RATE_PRECISION,
	buying_rate_for_row,
	line_amount,
)
from erpnext_moldova_efactura.utils.buying_taxes import apply_buying_taxes
from erpnext_moldova_efactura.utils.invoice_xml import parse_invoice_xml, unescape_sfs_text
from erpnext_moldova_efactura.utils.item_map import resolve_item_code, upsert_item_map
from erpnext_moldova_efactura.utils.party import find_supplier_by_idno, throw_if_supplier_idno_mismatch
from erpnext_moldova_efactura.utils.pef_currency import (
	apply_document_amounts_from_ef,
	apply_supplier_or_default_currency,
	remap_xml_item_money,
	settings_ef_currency,
)
from erpnext_moldova_efactura.utils.pi_alloc import (
	apply_allocations,
	has_allocations,
	set_pi_buyer_link,
	throw_unallocated_items,
	unique_purchase_invoices,
	validate_allocation_qtys,
)
from erpnext_moldova_efactura.utils.pi_match import validate_and_match
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
	def onload(self):
		self._unescape_xml_text_fields(persist=True)

	def validate(self):
		self._validate_unique_series_number()
		self._validate_items_immutable()
		self._unescape_xml_text_fields()
		self._validate_allocations()
		throw_if_supplier_idno_mismatch(self.supplier, self.ef_supplier_idno)
		if self.docstatus == 0:
			self.ef_currency = settings_ef_currency()
			apply_supplier_or_default_currency(self)
			apply_document_amounts_from_ef(self)
			self.apply_item_maps()
			self._persist_learned_maps()
		self.set_status(update=False, log=False)

	def before_insert(self):
		if not self._is_system_insert_allowed():
			frappe.throw(
				_("Incoming e-Factura cannot be created manually. Use Fetch from e-Factura.")
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
		if not self.supplier:
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
			if not (self.supplier and row.item_code):
				continue
			if not (row.supplier_item_name or row.supplier_item_code):
				continue
			upsert_item_map(
				self.supplier,
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
		old_status = self.status
		label = compose_buyer_status(self.ef_status)
		self.status = label
		self.efactura_status = label
		if update and not self.is_new():
			self.db_set({"status": label, "efactura_status": label}, update_modified=False)
		if log and not self.is_new():
			log_status_change(self, old_status, label)
		for pi_name in unique_purchase_invoices(self):
			from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status

			sync_pi_fiscal_status(pi_name)

	def persist_sfs_status(self, ef_status=None):
		"""Write SFS status fields without a full save (safe after submit)."""
		if ef_status is not None:
			try:
				self.ef_status = int(ef_status)
			except (TypeError, ValueError):
				pass
		self.last_status_check = now_datetime()
		self.set_status(update=False)
		if self.is_new():
			return
		self.db_set(
			{
				"ef_status": self.ef_status,
				"status": self.status,
				"efactura_status": self.efactura_status,
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
					self.supplier, row.supplier_item_code, row.supplier_item_name
				)
				if mapped:
					row.item_code = mapped
			apply_uom_to_buyer_row(row)

	def fill_from_xml(self, xml_content: str, preserve_mapped_items: bool = True):
		"""Apply a freshly fetched SFS XML payload; XML itself is not stored."""
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

		if not self.supplier and self.ef_supplier_idno:
			self.supplier = find_supplier_by_idno(self.ef_supplier_idno)

		existing_maps = {}
		if preserve_mapped_items:
			for row in self.items or []:
				key = (row.supplier_item_code or "", row.supplier_item_name or "")
				existing_maps[key] = {
					"item_code": row.item_code,
					"ef_uom": row.ef_uom,
					"uom": row.uom,
					"conversion_factor": row.conversion_factor,
					"ef_conversion_factor": row.ef_conversion_factor,
					"purchase_invoice": row.purchase_invoice,
					"pi_detail": row.pi_detail,
				}

		self.set("items", [])
		for item in parsed.get("items") or []:
			key = (item.get("supplier_item_code") or "", item.get("supplier_item_name") or "")
			row = self.append("items", remap_xml_item_money(item))
			prev = existing_maps.get(key) or {}
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
			if prev.get("purchase_invoice"):
				row.purchase_invoice = prev["purchase_invoice"]
				row.pi_detail = prev.get("pi_detail")

		self.ef_currency = settings_ef_currency()
		apply_supplier_or_default_currency(self, overwrite_company_default=True)
		apply_document_amounts_from_ef(self)

		self.apply_item_maps()

	def refresh_from_api(self):
		client = EFacturaAPIClient.from_settings()
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
			self.ef_status = int(inv.get("InvoiceStatus"))

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
	if not doc.supplier:
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
	try:
		code = int(doc.ef_status)
	except (TypeError, ValueError):
		code = None
	if code not in BUYER_ACTIONABLE_STATUSES:
		frappe.throw(
			_("eFactura can be accepted or rejected only in Sent to Buyer or Signed by Supplier status.")
		)


def _sfs_action_error(resp) -> str | None:
	if not resp:
		return _("empty response")
	if resp.get("ErrorMessage"):
		return resp.get("ErrorMessage")
	try:
		if int(resp.get("Status")) == 3:
			return _("e-Factura request failed")
	except (TypeError, ValueError):
		pass
	results = resp.get("Results") or {}
	if isinstance(results, list):
		items = results
	elif isinstance(results, dict):
		items = as_list(results.get("InvoiceResult") or results.get("Invoice"))
	else:
		items = []
	for item in items:
		try:
			if int(item.get("Status")) == 3:
				return item.get("Message") or _("e-Factura request failed")
		except (TypeError, ValueError):
			continue
	return None


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
	client = EFacturaAPIClient.from_settings()
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

	client = EFacturaAPIClient.from_settings()
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

	client = EFacturaAPIClient.from_settings()
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

	client = EFacturaAPIClient.from_settings()
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
def get_xml_for_sign(name: str):
	"""Return invoice XML + C14N SHA1 hash from SFS for MoldSign (buyer)."""
	import base64
	import hashlib

	from lxml import etree

	doc = _get_purchase_efactura(name)
	_require_submitted(doc)
	client = EFacturaAPIClient.from_settings()
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
	_require_submitted(doc)
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

	client = EFacturaAPIClient.from_settings()
	client.post_invoices(
		actor_role=2,
		invoices_xml=wrapped,
		invoices_xml_status=1,
	)
	_refresh_status(doc)
	log_event(doc, _("Signed invoice in e-Factura (buyer)."))
	return {"status": doc.status, "ef_status": doc.ef_status}


def _refresh_status(doc):
	client = EFacturaAPIClient.from_settings()
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
		if doc.supplier and buyer_row.item_code:
			upsert_item_map(
				doc.supplier,
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
	_require_not_cancelled(source)
	_require_mapped(source, _("Purchase Invoice"))
	if has_allocations(source):
		frappe.throw(_("e-Factura already has Purchase Invoice allocations"))

	pi = frappe.new_doc("Purchase Invoice")
	pi.company = source.company
	pi.supplier = source.supplier
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
	_require_not_cancelled(source)
	_require_mapped(source, _("Purchase Order"))

	po = frappe.new_doc("Purchase Order")
	po.company = source.company
	po.supplier = source.supplier
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
def link_purchase_invoice(name: str, purchase_invoice: str):
	doc = _get_purchase_efactura(name)
	if doc.docstatus == 2:
		frappe.throw(_("Cannot link Purchase Invoice to a cancelled e-Factura"))
	if not doc.supplier:
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
	if doc.supplier and pi.supplier and doc.supplier != pi.supplier:
		frappe.throw(
			_("Supplier mismatch: e-Factura {0}, Purchase Invoice {1}").format(doc.supplier, pi.supplier)
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


@frappe.whitelist()
def get_linked_buyers(purchase_invoice: str):
	from erpnext_moldova_efactura.utils.pi_alloc import get_buyer_names_for_pi

	if not frappe.has_permission("Purchase Invoice", "read", purchase_invoice):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not frappe.has_permission("Purchase eFactura", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return get_buyer_names_for_pi(purchase_invoice)
