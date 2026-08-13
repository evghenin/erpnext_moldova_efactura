# Copyright (c) 2026, Evgheni Nemerenco and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, now_datetime

from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from erpnext_moldova_efactura.utils.api_response import as_list, extract_invoices, invoice_status_map, invoice_xml
from erpnext_moldova_efactura.utils.buyer_status import BUYER_ACTIONABLE_STATUSES, compose_buyer_status
from erpnext_moldova_efactura.utils.buying_taxes import apply_buying_taxes
from erpnext_moldova_efactura.utils.invoice_xml import parse_invoice_xml, unescape_sfs_text
from erpnext_moldova_efactura.utils.item_map import resolve_item_code, upsert_item_map
from erpnext_moldova_efactura.utils.party import find_supplier_by_idno
from erpnext_moldova_efactura.utils.pi_match import validate_and_match
from erpnext_moldova_efactura.utils.uom_map import (
	apply_booking_defaults,
	apply_qty_defaults,
	apply_uom_to_buyer_row,
	compute_buyer_item_qtys,
	ensure_uom_map,
	get_item_uom_conversion,
)


class eFacturaBuyer(Document):
	def onload(self):
		self._unescape_xml_text_fields(persist=True)

	def validate(self):
		self._validate_unique_series_number()
		self._validate_items_immutable()
		self._unescape_xml_text_fields()
		if self.docstatus == 0:
			self.apply_item_maps()
			self._persist_learned_maps()
		self.set_status(update=False)

	def before_insert(self):
		if not self._is_system_insert_allowed():
			frappe.throw(
				_("Incoming e-Factura cannot be created manually. Use Fetch from e-Factura.")
			)
		if not self.currency:
			self.currency = frappe.db.get_single_value("eFactura Settings", "currency") or "MDL"

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
		unmapped = [str(r.idx) for r in self.items if not r.item_code]
		if unmapped:
			frappe.throw(
				_("Map all items before submit (unmapped rows: {0})").format(", ".join(unmapped))
			)
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
			"eFactura Buyer",
			{
				"company": self.company,
				"ef_series": self.ef_series,
				"ef_number": self.ef_number,
				"name": ["!=", self.name],
			},
		)
		if existing:
			frappe.throw(
				_("eFactura Buyer {0} already exists for {1}{2}").format(
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
			flt(row.rate),
			flt(row.amount),
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

	def set_status(self, update: bool = True):
		label = compose_buyer_status(self.ef_status, self.purchase_invoice)
		self.status = label
		if update and not self.is_new():
			self.db_set("status", label, update_modified=False)

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
		if parsed.get("delivery_date"):
			self.delivery_date = parsed["delivery_date"]

		self.total = parsed.get("total")
		self.vat_total = parsed.get("vat_total")
		self.net_total = parsed.get("net_total")

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
				}

		self.set("items", [])
		for item in parsed.get("items") or []:
			key = (item.get("supplier_item_code") or "", item.get("supplier_item_name") or "")
			row = self.append("items", item)
			prev = existing_maps.get(key) or {}
			if prev.get("item_code"):
				row.item_code = prev["item_code"]
			if prev.get("ef_uom"):
				row.ef_uom = prev["ef_uom"]
			if prev.get("uom"):
				row.uom = prev["uom"]

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


def _require_submitted(doc):
	if doc.docstatus != 1:
		frappe.throw(_("Submit eFactura Buyer before this action"))


def _require_mapped(doc, action_label: str | None = None):
	if not doc.supplier:
		frappe.throw(_("Supplier is required to create {0}").format(action_label or _("Purchase Invoice")))
	if not doc.items:
		frappe.throw(_("No items on eFactura Buyer — fetch details first"))
	unmapped = [str(r.idx) for r in doc.items if not r.item_code]
	if unmapped:
		frappe.throw(
			_("Map all items before creating {0} (rows: {1})").format(
				action_label or _("Purchase Invoice"),
				", ".join(unmapped),
			)
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
	doc = frappe.get_doc("eFactura Buyer", name)
	doc.refresh_from_api()
	return doc.as_dict()


@frappe.whitelist()
def accept_invoice(name: str):
	doc = frappe.get_doc("eFactura Buyer", name)
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
	return {"status": doc.status, "ef_status": doc.ef_status}


@frappe.whitelist()
def reject_invoice(name: str, reason: str | None = None):
	doc = frappe.get_doc("eFactura Buyer", name)
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
	return {"status": doc.status, "ef_status": doc.ef_status, "rejection_reason": comment}


@frappe.whitelist()
def download_xml(name: str):
	"""Download invoice XML from SFS (buyer)."""
	doc = frappe.get_doc("eFactura Buyer", name)
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
	doc = frappe.get_doc("eFactura Buyer", name)
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

	doc = frappe.get_doc("eFactura Buyer", name)
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

	doc = frappe.get_doc("eFactura Buyer", name)
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
	return {"status": doc.status, "ef_status": doc.ef_status}


def _refresh_status(doc):
	client = EFacturaAPIClient.from_settings()
	resp = client.check_invoices_status([{"Seria": doc.ef_series, "Number": doc.ef_number}])
	statuses = invoice_status_map(resp)
	key = (str(doc.ef_series), str(doc.ef_number))
	if key in statuses:
		doc.db_set("ef_status", statuses[key], update_modified=False)
		doc.ef_status = statuses[key]
	doc.db_set("last_status_check", now_datetime(), update_modified=False)
	doc.set_status(update=True)


@frappe.whitelist()
def update_status(name: str):
	"""Refresh InvoiceStatus from SFS into eFactura Buyer."""
	doc = frappe.get_doc("eFactura Buyer", name)
	if not doc.ef_series or not doc.ef_number:
		frappe.throw(_("eFactura Series/Number is required to update status"))
	_refresh_status(doc)
	return {"status": doc.status, "ef_status": doc.ef_status}


@frappe.whitelist()
def get_item_qty_fields(item_code=None, ef_uom=None, ef_qty=None, uom=None):
	"""UI helper: recompute stock_uom / stock_qty / qty; default empty ef_uom from Item Stock UOM."""
	item_code = item_code or None
	ef_uom = ef_uom or None
	uom = uom or None
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") if item_code else None
	if item_code and not ef_uom:
		ef_uom = stock_uom
	if item_code and not uom:
		uom = ef_uom or stock_uom
	out = compute_buyer_item_qtys(
		item_code=item_code,
		ef_uom=ef_uom,
		ef_qty=ef_qty,
		uom=uom,
	)
	out["ef_uom"] = ef_uom
	out["uom"] = uom
	return out


@frappe.whitelist()
def get_new_supplier_defaults(name: str | None = None, title: str | None = None, idno: str | None = None):
	"""Prefill values when creating a Supplier from eFactura Buyer."""
	if name:
		doc = frappe.get_doc("eFactura Buyer", name)
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

	doc = frappe.get_doc("eFactura Buyer", name)
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
	uom_cf = get_item_uom_conversion(row.item_code, row.uom) or 1
	ef_rate = flt(row.rate_with_vat) if vat_included else flt(row.rate)
	if flt(row.qty):
		rate = ef_rate * flt(row.ef_qty) / flt(row.qty)
	else:
		ef_cf = get_item_uom_conversion(row.item_code, row.ef_uom) or 1
		rate = ef_rate * flt(ef_cf) / flt(uom_cf)
	uom = row.uom if row.uom and frappe.db.exists("UOM", row.uom) else None
	return {
		"item_code": row.item_code,
		"item_name": row.item_name or row.supplier_item_name,
		"qty": row.qty,
		"uom": uom,
		"conversion_factor": uom_cf,
		"rate": rate,
		"stock_uom": frappe.db.get_value("Item", row.item_code, "stock_uom"),
	}


def _append_buying_items(target, source):
	vat_included = bool(frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate"))
	schedule = source.delivery_date or source.issue_date
	for row in source.items:
		vals = _buying_line_from_buyer(row, vat_included)
		item = target.append("items", {})
		item.item_code = vals["item_code"]
		item.item_name = vals["item_name"]
		item.qty = vals["qty"]
		if vals["uom"]:
			item.uom = vals["uom"]
		item.conversion_factor = vals["conversion_factor"]
		item.rate = vals["rate"]
		if vals["stock_uom"] and item.meta.has_field("stock_uom"):
			item.stock_uom = vals["stock_uom"]
		if schedule and item.meta.has_field("schedule_date"):
			item.schedule_date = schedule
	apply_buying_taxes(target, source)


def _set_efactura_buyer_link(target, source_name: str):
	if target.meta.has_field("efactura_buyer"):
		target.efactura_buyer = source_name


def _apply_pi_item_mapping(doc, pairs):
	"""Copy Item / UOM / qty from matched PI rows onto draft buyer lines."""
	for buyer_row, pi_row in pairs:
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


@frappe.whitelist()
def make_purchase_invoice(source_name: str, target_doc=None):
	source = frappe.get_doc("eFactura Buyer", source_name)
	_require_submitted(source)
	_require_mapped(source, _("Purchase Invoice"))
	if source.purchase_invoice:
		frappe.throw(_("Purchase Invoice {0} is already linked").format(source.purchase_invoice))

	if source.purchase_order:
		return _make_pi_from_purchase_order(source)

	pi = frappe.new_doc("Purchase Invoice")
	pi.company = source.company
	pi.supplier = source.supplier
	pi.currency = source.currency or "MDL"
	pi.bill_no = f"{source.ef_series}{source.ef_number}"
	if source.issue_date:
		pi.bill_date = source.issue_date
	_set_efactura_buyer_link(pi, source.name)
	_append_buying_items(pi, source)
	return pi


def _make_pi_from_purchase_order(source):
	po_name = source.purchase_order
	po_status = frappe.db.get_value("Purchase Order", po_name, "docstatus")
	if cint(po_status) != 1:
		frappe.throw(_("Submit Purchase Order {0} before creating Purchase Invoice").format(po_name))

	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_invoice as po_make_pi

	pi = po_make_pi(po_name)
	pi.bill_no = f"{source.ef_series}{source.ef_number}"
	if source.issue_date:
		pi.bill_date = source.issue_date
	_set_efactura_buyer_link(pi, source.name)
	return pi


@frappe.whitelist()
def make_purchase_order(source_name: str, target_doc=None):
	from frappe.utils import today

	source = frappe.get_doc("eFactura Buyer", source_name)
	_require_submitted(source)
	_require_mapped(source, _("Purchase Order"))
	if source.purchase_invoice:
		frappe.throw(_("Purchase Invoice {0} is already linked").format(source.purchase_invoice))
	if source.purchase_order:
		frappe.throw(_("Purchase Order {0} is already linked").format(source.purchase_order))

	po = frappe.new_doc("Purchase Order")
	po.company = source.company
	po.supplier = source.supplier
	po.currency = source.currency or "MDL"
	po.transaction_date = today()
	schedule = source.delivery_date or source.issue_date or today()
	if po.meta.has_field("schedule_date"):
		po.schedule_date = schedule
	_set_efactura_buyer_link(po, source.name)
	_append_buying_items(po, source)
	return po


@frappe.whitelist()
def link_purchase_invoice(name: str, purchase_invoice: str):
	doc = frappe.get_doc("eFactura Buyer", name)
	if doc.docstatus == 2:
		frappe.throw(_("Cannot link Purchase Invoice to a cancelled e-Factura"))
	if not doc.items:
		frappe.throw(_("Fetch invoice details from e-Factura before linking a Purchase Invoice"))
	if not frappe.db.exists("Purchase Invoice", purchase_invoice):
		frappe.throw(_("Purchase Invoice {0} not found").format(purchase_invoice))

	pi = frappe.get_doc("Purchase Invoice", purchase_invoice)
	pairs = validate_and_match(doc, pi)

	if doc.docstatus == 0:
		_apply_pi_item_mapping(doc, pairs)

	doc.purchase_invoice = purchase_invoice
	doc.set_status(update=False)
	if doc.docstatus == 1:
		doc.db_set(
			{"purchase_invoice": purchase_invoice, "status": doc.status},
			update_modified=False,
		)
	else:
		doc.save()

	if pi.meta.has_field("efactura_buyer"):
		frappe.db.set_value("Purchase Invoice", purchase_invoice, "efactura_buyer", name)
	return doc.as_dict()
