from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from erpnext_moldova_efactura.utils.factura_pdf import (
	FacturaImportError,
	decimal,
	is_pdf_image_scan,
	money,
	parse_pdf,
)
from erpnext_moldova_efactura.utils.factura_pdf_signature import verify_pdf_signatures
from erpnext_moldova_efactura.utils.party import normalize_idno
from erpnext_moldova_efactura.utils.pf_amounts import (
	apply_quantities,
	convert_amounts,
	imported_fields,
	prepare_currency,
)
from erpnext_moldova_efactura.utils.timeline import log_event

ORIGINAL_FIELDS = (
	"provider",
	"original_format",
	"original_file",
	"f_series",
	"f_number",
	"issue_date",
	"delivery_date",
	"f_supplier_idno",
	"f_supplier_name",
	"f_supplier_vat_id",
	"f_supplier_taxpayer_type",
	"f_supplier_address",
	"f_supplier_bank_account",
	"f_supplier_bank_name",
	"f_supplier_bank_code",
	"f_customer_idno",
	"f_customer_name",
	"f_customer_vat_id",
	"f_customer_taxpayer_type",
	"f_customer_address",
	"f_customer_bank_account",
	"f_customer_bank_name",
	"f_customer_bank_code",
	"f_currency",
	"f_provider_reference",
	"f_provider_account",
	"f_contract_reference",
	"f_related_document_type",
	"f_related_document_number",
	"f_related_document_date",
	"file_hash",
)
ORIGINAL_ITEM_FIELDS = (
	"supplier_item_code",
	"supplier_item_name",
	"supplier_uom",
	"f_qty",
	"f_rate",
	"f_net_amount",
	"f_vat_rate",
	"f_vat_amount",
)


def _get_pf(name):
	doc = frappe.get_doc("Purchase Factura", name)
	doc.check_permission("write")
	return doc


def _lock_company(company):
	# Serialise duplicate checks with PF imports and PEF-to-PI actions in this Company.
	frappe.db.sql("select name from `tabCompany` where name=%s for update", company)


def _party_idno(doctype, name):
	field = frappe.db.get_single_value("eFactura Settings", f"{doctype.lower()}_idno_field") or "tax_id"
	if not frappe.get_meta(doctype).has_field(field):
		frappe.throw(_("Configure the {0} IDNO field in eFactura Settings").format(doctype))
	return normalize_idno(frappe.db.get_value(doctype, name, field))


def _identity(company, f_supplier_idno, f_series, f_number):
	parts = [
		company or "",
		normalize_idno(f_supplier_idno),
		(f_series or "").strip().upper(),
		(f_number or "").strip().upper(),
	]
	return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _changed(current, previous, key):
	field = current.meta.get_field(key)
	if key in {"f_supplier_idno", "f_customer_idno"}:
		return normalize_idno(current.get(key)) != normalize_idno(previous.get(key))
	if field and field.fieldtype in ("Currency", "Float", "Percent", "Int", "Check"):
		return flt(current.get(key), 9) != flt(previous.get(key), 9)
	return str(current.get(key) or "") != str(previous.get(key) or "")


class PurchaseFactura(Document):
	def _validate_links(self):
		# Frappe's Amend copies even no_copy fields and validates links before before_insert.
		if self.is_new() and self.amended_from:
			self.purchase_invoice = None
			for row in self.items:
				row.purchase_invoice = None
				row.pi_detail = None
		super()._validate_links()

	def before_insert(self):
		for row in self.items:
			row.purchase_invoice = self.purchase_invoice
			if not self.purchase_invoice:
				row.pi_detail = None

	def after_insert(self):
		if not self.original_file:
			return
		from erpnext_moldova_efactura.utils.pf_original import copy_original_file

		copied = copy_original_file(self)
		if copied and copied != self.original_file:
			self.db_set("original_file", copied, update_modified=False)
			self.original_file = copied

	def validate(self):
		if not self.company:
			frappe.throw(_("Select a Company"))
		_lock_company(self.company)
		self.f_series = (self.f_series or "").strip().upper()
		self.f_number = (self.f_number or "").strip()
		self.f_supplier_idno = normalize_idno(self.f_supplier_idno)
		self.f_customer_idno = normalize_idno(self.f_customer_idno) or _party_idno("Company", self.company)
		self.supplier_party_type = "Supplier"
		if self.supplier_party and not self.f_supplier_idno:
			self.f_supplier_idno = _party_idno("Supplier", self.supplier_party)
		if self.f_customer_idno != _party_idno("Company", self.company):
			frappe.throw(_("Buyer IDNO does not match the selected Company"))
		if self.supplier_party and self.f_supplier_idno != _party_idno("Supplier", self.supplier_party):
			frappe.throw(_("Supplier IDNO does not match the original factura"))
		prepare_currency(self)
		self._validate_original()
		self._calculate()
		for row in self.items:
			row.purchase_invoice = self.purchase_invoice
		self.identity_key = _identity(self.company, self.f_supplier_idno, self.f_series, self.f_number)
		duplicates = frappe.get_all(
			"Purchase Factura",
			filters={"identity_key": self.identity_key, "name": ["!=", self.name], "docstatus": ["<", 2]},
			fields=["name", "docstatus"],
		)
		if duplicates:
			frappe.throw(_("This factura is already registered as {0}").format(duplicates[0].name))
		previous = self.get_doc_before_save()
		if not self.flags.get("linking_pi") and self.purchase_invoice != (
			previous.purchase_invoice if previous else None
		):
			frappe.throw(_("Use the Purchase Invoice create/link actions to change this link"))
		if self.purchase_invoice:
			from erpnext_moldova_efactura.utils.pf_invoice import match_invoice

			match_invoice(self, frappe.get_doc("Purchase Invoice", self.purchase_invoice))

	def _validate_original(self):
		previous = self.get_doc_before_save()
		if not previous and self.amended_from:
			previous = frappe.get_doc("Purchase Factura", self.amended_from)
		if previous and previous.provider:
			changed_fields = [
				self.meta.get_field(key).label or key
				for key in ORIGINAL_FIELDS
				if _changed(self, previous, key)
			]
			if changed_fields:
				frappe.throw(
					_("Imported original fields changed: {0}; register a corrected original separately").format(
						", ".join(changed_fields)
					)
				)
			if len(self.items) != len(previous.items):
				frappe.throw(_("Imported original item values cannot be changed"))
			for current, old in zip(self.items, previous.items, strict=True):
				changed_item_fields = [
					current.meta.get_field(key).label or key
					for key in ORIGINAL_ITEM_FIELDS
					if _changed(current, old, key)
				]
				if changed_item_fields:
					frappe.throw(
						_("Imported original item field changed: {0}; register a corrected original separately").format(
							", ".join(changed_item_fields)
						)
					)
		elif self.provider and not self.flags.get("pdf_import"):
			frappe.throw(_("Use Import PDF to set imported original metadata"))
		if self.original_format == "Paper":
			self.signature_status = "Not Applicable"
			self.signature_integrity = self.signature_integrity or "Not Applicable"
		elif not self.signature_status:
			self.signature_status = "Not Checked"
		if self.original_format != "Paper" and not self.original_file:
			frappe.throw(_("Attach the electronic original"))
		if self.original_file:
			if self.file_hash and _file_sha256(_read_original(self.original_file)) != self.file_hash:
				frappe.throw(_("The original file no longer matches its imported content"))

	def _calculate(self):
		if not self.items:
			frappe.throw(_("Add at least one factura item"))
		for row in self.items:
			try:
				qty, rate, vat_rate = map(decimal, (row.f_qty or 0, row.f_rate or 0, row.f_vat_rate or 0))
				net_preview = money(row.f_net_amount) if row.f_net_amount else money(qty * rate)
				if qty <= 0 or not 0 <= vat_rate <= 100 or (rate < 0 and net_preview >= 0):
					frappe.throw(
						_(
							"Row {0}: positive quantities are required; negative rates are only for printed discounts"
						).format(row.idx)
					)
				net = money(row.f_net_amount) if row.f_net_amount else money(qty * rate)
				vat = money(row.f_vat_amount) if row.f_vat_amount else money(net * vat_rate / 100)
				# Paper Pret unitar is often rounded independently of Valoarea fara TVA
				# (e.g. 5 × 124.17 vs printed net 620.83). Digital originals stay at 1 ban.
				qty_rate_tol = decimal("0.05") if self.original_format == "Paper" else decimal("0.01")
				vat_ok = abs(vat - money(net * vat_rate / 100)) <= decimal("0.05")
				qty_rate_ok = abs(net - money(qty * rate)) <= qty_rate_tol
				# METRO Reducere is on the charged row: Pret unitar × Cant. stays list, net is after Reducere.
				if (
					not qty_rate_ok
					and self.original_format == "Paper"
					and qty > 0
					and rate > 0
					and net > 0
					and money(qty * rate) > net
				):
					qty_rate_ok = True
				if not qty_rate_ok or not vat_ok:
					frappe.throw(
						_("Row {0}: original quantity, rate, net and VAT amounts do not reconcile").format(
							row.idx
						)
					)
				row.f_net_amount, row.f_vat_amount, row.f_amount = float(net), float(vat), float(net + vat)
			except FacturaImportError as exc:
				frappe.throw(str(exc))
			row.f_rate_with_vat = flt(row.f_amount) / flt(row.f_qty)
			apply_quantities(self, row)
		item_net = money(sum(decimal(r.f_net_amount or 0) for r in self.items))
		item_vat = money(sum(decimal(r.f_vat_amount or 0) for r in self.items))
		item_total = money(sum(decimal(r.f_amount or 0) for r in self.items))
		printed_net = money(self.f_net_total) if self.f_net_total else item_net
		if self.original_format == "Paper" and abs(printed_net - item_net) <= decimal("0.20"):
			self.f_net_total = float(printed_net)
			self.f_total = float(item_total)
			self.f_vat_total = float(money(item_total - printed_net))
		else:
			self.f_net_total = float(item_net)
			self.f_vat_total = float(item_vat)
			self.f_total = float(item_total)
		convert_amounts(self)

	def before_submit(self):
		if self.docstatus == 2 or not self.supplier_party:
			frappe.throw(_("Select a Supplier before submitting this factura"))
		if (
			not self.purchase_invoice
			or frappe.db.get_value("Purchase Invoice", self.purchase_invoice, "docstatus") != 1
		):
			frappe.throw(_("Link and submit the Purchase Invoice before submitting this factura"))

	def on_update(self):
		self._refresh_fiscal_status()

	def on_submit(self):
		self._refresh_fiscal_status()

	def on_cancel(self):
		self._clear_purchase_invoice_link()

	def on_trash(self):
		frappe.flags.pf_deleting = self.name
		self._clear_purchase_invoice_link()

	def _clear_purchase_invoice_link(self):
		from erpnext_moldova_efactura.utils.doc_unlink import clear_header_fields, clear_item_fields
		from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status

		pi_name = self.purchase_invoice
		if pi_name:
			frappe.db.set_value(
				"Purchase Invoice", pi_name, "purchase_factura", None, update_modified=False
			)
		clear_item_fields(self, ("purchase_invoice", "pi_detail"))
		clear_header_fields(self, ("purchase_invoice",))
		if pi_name:
			sync_pi_fiscal_status(pi_name)

	def _refresh_fiscal_status(self):
		if self.purchase_invoice:
			from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status

			sync_pi_fiscal_status(self.purchase_invoice)


SIGNATURE_FIELDS = (
	"signature_status",
	"signature_integrity",
	"signature_format",
	"signature_field",
	"signature_declared_time",
	"signature_coverage",
	"signature_certificate_trust",
	"signature_revocation",
	"signature_timestamp_check",
	"signature_evidence",
	"signature_checked_on",
	"signature_checked_by",
)


def _verified_signature_values(content: bytes) -> dict:
	result = verify_pdf_signatures(content)
	from frappe.utils import now_datetime

	result["signature_checked_on"] = now_datetime()
	result["signature_checked_by"] = frappe.session.user
	return result


def _file_bytes(file_doc):
	# Prefer the on-disk original. File.get_content() can return the in-memory upload
	# payload, or a UTF-8 string, neither of which is what later saves will hash.
	path = file_doc.get_full_path()
	try:
		with open(path, "rb") as handle:
			return handle.read()
	except OSError:
		content = file_doc.get_content()
		return content.encode("utf-8") if isinstance(content, str) else content


def _file_sha256(file_doc):
	return hashlib.sha256(_file_bytes(file_doc)).hexdigest()


def _read_original(file_url):
	# Resolve a Frappe File, never a caller-supplied filesystem path or remote URL.
	files = frappe.get_all("File", filters={"file_url": file_url}, pluck="name", limit=2)
	if not files:
		frappe.throw(_("Original file not found or access denied"), frappe.PermissionError)
	file_doc = frappe.get_doc("File", files[0])
	file_doc.check_permission("read")
	if not file_doc.is_private:
		frappe.throw(_("Upload the original as a private file"))
	return file_doc


def _swap_inverted_parties(data: dict, company: str):
	"""Gemini often swaps METRO till columns: company IDNO lands on the supplier side."""
	company_idno = _party_idno("Company", company)
	data["f_customer_idno"] = normalize_idno(data.get("f_customer_idno"))
	data["f_supplier_idno"] = normalize_idno(data.get("f_supplier_idno"))
	if data.get("f_customer_idno") == company_idno:
		return
	if data.get("f_supplier_idno") != company_idno:
		frappe.throw(_("The PDF recipient IDNO does not match the selected Company"))
	for left, right in (
		("f_supplier_idno", "f_customer_idno"),
		("f_supplier_name", "f_customer_name"),
		("f_supplier_vat_id", "f_customer_vat_id"),
		("f_supplier_taxpayer_type", "f_customer_taxpayer_type"),
		("f_supplier_address", "f_customer_address"),
		("f_supplier_bank_account", "f_customer_bank_account"),
		("f_supplier_bank_name", "f_customer_bank_name"),
		("f_supplier_bank_code", "f_customer_bank_code"),
	):
		data[left], data[right] = data.get(right), data.get(left)


@frappe.whitelist()
def import_pdf(file_url: str, company: str, use_ai: int | str | None = None):
	frappe.has_permission("Purchase Factura", "create", throw=True)
	frappe.get_doc("Company", company).check_permission("read")
	file_doc = _read_original(file_url)
	try:
		content = _file_bytes(file_doc)
		if cint(use_ai):
			from erpnext_moldova_efactura.utils.factura_ai import parse_image

			parsed = parse_image(content)
		else:
			if not content.startswith(b"%PDF-"):
				raise FacturaImportError("PDF Orange / Arax import accepts only PDF files")
			parsed = parse_pdf(content)
		data = imported_fields(parsed)
	except FacturaImportError as exc:
		frappe.throw(_(str(exc)), title=_("Cannot read factura"))
	_swap_inverted_parties(data, company)
	_lock_company(company)
	key = _identity(company, data["f_supplier_idno"], data["f_series"], data["f_number"])
	existing = frappe.db.get_value("Purchase Factura", {"identity_key": key, "docstatus": ["<", 2]}, "name")
	if existing:
		frappe.get_doc("Purchase Factura", existing).check_permission("read")
		return existing
	check_signatures = content.startswith(b"%PDF-") and not (
		cint(use_ai) and is_pdf_image_scan(content)
	)
	if check_signatures:
		try:
			data.update(_verified_signature_values(content))
		except FacturaImportError as exc:
			frappe.throw(_(str(exc)))
		if cint(use_ai):
			data["original_format"] = (
				"Digitally Signed PDF"
				if data.get("signature_status") != "Not Applicable"
				else "Other Electronic"
			)
	field = frappe.db.get_single_value("eFactura Settings", "supplier_idno_field") or "tax_id"
	suppliers = frappe.get_all(
		"Supplier", filters={field: data["f_supplier_idno"]}, pluck="name", limit_page_length=2
	)
	data["supplier_party_type"] = "Supplier"
	data["supplier_party"] = suppliers[0] if len(suppliers) == 1 else None
	from erpnext_moldova_efactura.utils.pef_currency import default_document_currency

	data["currency"] = default_document_currency(data["supplier_party"], company)
	if data["supplier_party"]:
		from erpnext_moldova_efactura.utils.item_map import resolve_item_and_uom
		from erpnext_moldova_efactura.utils.uom_map import resolve_uom

		for row in data["items"]:
			item_code, mapped_uom = resolve_item_and_uom(
				data["supplier_party"], row.get("supplier_item_code"), row["supplier_item_name"]
			)
			if not item_code:
				continue
			item = frappe.get_cached_value("Item", item_code, ["purchase_uom", "stock_uom"], as_dict=True)
			row["item_code"] = item_code
			row["uom"] = mapped_uom or resolve_uom(row["supplier_uom"]) or item.purchase_uom or item.stock_uom
	doc = frappe.get_doc(dict(data, doctype="Purchase Factura", company=company, original_file=file_url))
	doc.flags.pdf_import = True
	doc.insert()
	if check_signatures:
		log_event(
			doc,
			"checked the PDF signature: integrity {0}, overall {1}",
			doc.signature_integrity,
			doc.signature_status,
		)
	return doc.name


@frappe.whitelist()
def verify_pdf_signature(name: str):
	doc = _get_pf(name)
	if doc.original_format == "Paper":
		frappe.throw(_("Paper originals have no PDF signature to verify"))
	if not doc.original_file:
		frappe.throw(_("Attach the electronic original"))
	content = _read_original(doc.original_file).get_content()
	if isinstance(content, str):
		content = content.encode("utf-8")
	if not content.startswith(b"%PDF-"):
		frappe.throw(_("PDF signature verification requires the original PDF"))
	try:
		result = _verified_signature_values(content)
	except FacturaImportError as exc:
		frappe.throw(_(str(exc)))
	for field in SIGNATURE_FIELDS:
		doc.db_set(field, result.get(field), update_modified=True)
	log_event(
		doc,
		"checked the PDF signature: integrity {0}, overall {1}",
		result["signature_integrity"],
		result["signature_status"],
	)
	return {field: result.get(field) for field in SIGNATURE_FIELDS}


@frappe.whitelist()
def party_defaults(company=None, supplier_party=None):
	frappe.has_permission("Purchase Factura", "create", throw=True)
	result = {}
	for doctype, name, key in (
		("Company", company, "f_customer_idno"),
		("Supplier", supplier_party, "f_supplier_idno"),
	):
		if name:
			frappe.get_doc(doctype, name).check_permission("read")
			result[key] = _party_idno(doctype, name)
	from erpnext_moldova_efactura.utils.pef_currency import default_document_currency

	result["currency"] = default_document_currency(supplier_party, company)
	return result


@frappe.whitelist()
def preview_amounts(document):
	payload = frappe.parse_json(document)
	payload["doctype"] = "Purchase Factura"
	doc = frappe.get_doc(payload)
	if doc.name and frappe.db.exists("Purchase Factura", doc.name):
		previous = _get_pf(doc.name)
		doc._doc_before_save = previous
	else:
		frappe.has_permission("Purchase Factura", "create", throw=True)
	for doctype, name in (("Company", doc.company), ("Supplier", doc.supplier_party)):
		if name:
			frappe.get_doc(doctype, name).check_permission("read")
	prepare_currency(doc)
	doc._calculate()
	return {
		"header": {
			key: doc.get(key)
			for key in (
				"currency",
				"f_currency",
				"f_conversion_rate",
				"net_total",
				"vat_total",
				"total",
				"f_net_total",
				"f_vat_total",
				"f_total",
			)
		},
		"items": [row.as_dict() for row in doc.items],
	}
