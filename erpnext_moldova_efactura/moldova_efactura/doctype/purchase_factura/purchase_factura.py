from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate, now_datetime

from erpnext_moldova_efactura.utils.factura_pdf import FacturaImportError, decimal, money, parse_pdf
from erpnext_moldova_efactura.utils.party import normalize_idno
from erpnext_moldova_efactura.utils.pf_amounts import (
	apply_quantities,
	convert_amounts,
	imported_fields,
	prepare_currency,
)

ORIGINAL_FIELDS = (
	"provider",
	"original_format",
	"original_file",
	"f_series",
	"f_number",
	"issue_date",
	"issue_time",
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
	"f_service_period_start",
	"f_service_period_end",
	"f_related_document_type",
	"f_related_document_number",
	"f_related_document_date",
	"signature_details",
	"extracted_text",
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
MAPPING_FIELDS = (
	"item_code",
	"f_uom",
	"uom",
	"qty",
	"conversion_factor",
	"f_conversion_factor",
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
		if self.amended_from:
			self.reviewed = 0
			self.reviewed_by = None
			self.reviewed_on = None

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
		if (
			self.f_service_period_start
			and self.f_service_period_end
			and getdate(self.f_service_period_start) > getdate(self.f_service_period_end)
		):
			frappe.throw(_("Service period end must not precede its start"))
		prepare_currency(self)
		self._validate_original()
		self._calculate()
		for row in self.items:
			row.purchase_invoice = self.purchase_invoice
		self.identity_key = _identity(self.company, self.f_supplier_idno, self.f_series, self.f_number)
		duplicates = frappe.get_all(
			"Purchase Factura",
			filters={"identity_key": self.identity_key, "name": ["!=", self.name]},
			fields=["name", "docstatus"],
		)
		active = next((row for row in duplicates if cint(row.docstatus) < 2), None)
		valid_amendment = self.amended_from and any(
			row.name == self.amended_from and cint(row.docstatus) == 2 for row in duplicates
		)
		if active or (duplicates and not valid_amendment):
			frappe.throw(
				_("This factura is already registered as {0}").format(
					active.name if active else duplicates[0].name
				)
			)
		previous = self.get_doc_before_save()
		if not self.flags.get("linking_pi") and self.purchase_invoice != (
			previous.purchase_invoice if previous else None
		):
			frappe.throw(_("Use the Purchase Invoice create/link actions to change this link"))
		if self.purchase_invoice:
			from erpnext_moldova_efactura.utils.pf_invoice import match_invoice

			match_invoice(self, frappe.get_doc("Purchase Invoice", self.purchase_invoice))
		if previous and not self.flags.get("linking_pi"):
			changed_items = len(self.items) != len(previous.items) or any(
				_changed(a, b, k)
				for a, b in zip(self.items, previous.items, strict=True)
				for k in ORIGINAL_ITEM_FIELDS + MAPPING_FIELDS
			)
			content_changed = changed_items or any(
				_changed(self, previous, k)
				for k in (*ORIGINAL_FIELDS, "company", "supplier_party", "currency", "f_conversion_rate")
			)
			new_review = cint(self.reviewed) and not cint(previous.reviewed)
			if content_changed and not new_review:
				self.reviewed = 0
		if cint(self.reviewed) and (not previous or not cint(previous.reviewed)):
			self.reviewed_by = frappe.session.user
			self.reviewed_on = now_datetime()
		elif not cint(self.reviewed):
			self.reviewed_by = None
			self.reviewed_on = None

	def _validate_original(self):
		previous = self.get_doc_before_save()
		if not previous and self.amended_from:
			previous = frappe.get_doc("Purchase Factura", self.amended_from)
		if previous and previous.provider:
			if any(_changed(self, previous, k) for k in ORIGINAL_FIELDS):
				frappe.throw(
					_("Imported original fields cannot be changed; register a corrected original separately")
				)
			if len(self.items) != len(previous.items) or any(
				_changed(a, b, k)
				for a, b in zip(self.items, previous.items, strict=True)
				for k in ORIGINAL_ITEM_FIELDS
			):
				frappe.throw(_("Imported original item values cannot be changed"))
		elif self.provider and not self.flags.get("pdf_import"):
			frappe.throw(_("Use Import PDF to set imported original metadata"))
		self.signature_status = "Not Applicable" if self.original_format == "Paper" else "Not Checked"
		if self.original_format != "Paper" and not self.original_file:
			frappe.throw(_("Attach the electronic original"))
		if self.original_file:
			file_doc = _read_original(self.original_file)
			if self.file_hash and hashlib.sha256(file_doc.get_content()).hexdigest() != self.file_hash:
				frappe.throw(_("The original file no longer matches its imported content"))

	def _calculate(self):
		if not self.items:
			frappe.throw(_("Add at least one factura item"))
		for row in self.items:
			try:
				qty, rate, vat_rate = map(decimal, (row.f_qty or 0, row.f_rate or 0, row.f_vat_rate or 0))
				if qty <= 0 or rate < 0 or not 0 <= vat_rate <= 100:
					frappe.throw(
						_(
							"Row {0}: positive quantities and non-negative rates are required; returns are not supported yet"
						).format(row.idx)
					)
				net = money(row.f_net_amount) if row.f_net_amount else money(qty * rate)
				vat = money(row.f_vat_amount) if row.f_vat_amount else money(net * vat_rate / 100)
				if abs(net - money(qty * rate)) > decimal("0.01") or abs(
					vat - money(net * vat_rate / 100)
				) > decimal("0.01"):
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
		self.f_net_total = sum(flt(r.f_net_amount) for r in self.items)
		self.f_vat_total = sum(flt(r.f_vat_amount) for r in self.items)
		self.f_total = sum(flt(r.f_amount) for r in self.items)
		convert_amounts(self)

	def before_submit(self):
		self.require_reviewed()
		if (
			not self.purchase_invoice
			or frappe.db.get_value("Purchase Invoice", self.purchase_invoice, "docstatus") != 1
		):
			frappe.throw(_("Link and submit the Purchase Invoice before submitting this factura"))

	def require_reviewed(self):
		if self.docstatus == 2 or not self.reviewed or not self.supplier_party:
			frappe.throw(_("Select a Supplier and review the original and mapping first"))

	def on_update(self):
		self._refresh_fiscal_status()

	def on_submit(self):
		self._refresh_fiscal_status()

	def on_cancel(self):
		if self.purchase_invoice:
			frappe.db.set_value(
				"Purchase Invoice", self.purchase_invoice, "purchase_factura", None, update_modified=False
			)
		self._refresh_fiscal_status()

	def on_trash(self):
		if self.purchase_invoice:
			frappe.throw(_("Unlink the Purchase Invoice before deleting this draft"))

	def _refresh_fiscal_status(self):
		if self.purchase_invoice:
			from erpnext_moldova_efactura.utils.fiscal_status import sync_pi_fiscal_status

			sync_pi_fiscal_status(self.purchase_invoice)


def _read_original(file_url):
	# Resolve a Frappe File, never a caller-supplied filesystem path or remote URL.
	files = frappe.get_list("File", filters={"file_url": file_url}, pluck="name", limit_page_length=2)
	if not files:
		frappe.throw(_("Original file not found or access denied"), frappe.PermissionError)
	file_doc = frappe.get_doc("File", files[0])
	file_doc.check_permission("read")
	if not file_doc.is_private:
		frappe.throw(_("Upload the original as a private file"))
	return file_doc


@frappe.whitelist()
def import_pdf(file_url: str, company: str):
	frappe.has_permission("Purchase Factura", "create", throw=True)
	frappe.get_doc("Company", company).check_permission("read")
	file_doc = _read_original(file_url)
	try:
		data = imported_fields(parse_pdf(file_doc.get_content()))
	except FacturaImportError as exc:
		frappe.throw(_(str(exc)), title=_("Cannot import factura"))
	if data["f_customer_idno"] != _party_idno("Company", company):
		frappe.throw(_("The PDF recipient IDNO does not match the selected Company"))
	_lock_company(company)
	key = _identity(company, data["f_supplier_idno"], data["f_series"], data["f_number"])
	existing = frappe.db.get_value("Purchase Factura", {"identity_key": key, "docstatus": ["<", 2]}, "name")
	if existing:
		frappe.get_doc("Purchase Factura", existing).check_permission("read")
		return existing
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
				data["supplier_party"], None, row["supplier_item_name"]
			)
			if not item_code:
				continue
			item = frappe.get_cached_value("Item", item_code, ["purchase_uom", "stock_uom"], as_dict=True)
			row["item_code"] = item_code
			row["uom"] = mapped_uom or resolve_uom(row["supplier_uom"]) or item.purchase_uom or item.stock_uom
	data["signature_details"] = frappe.as_json(data["signature_details"])
	doc = frappe.get_doc(dict(data, doctype="Purchase Factura", company=company, original_file=file_url))
	doc.flags.pdf_import = True
	doc.insert()
	# Attach a separate File reference if this original already belongs to another document.
	frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_doc.file_name,
			"file_url": file_doc.file_url,
			"is_private": 1,
			"attached_to_doctype": "Purchase Factura",
			"attached_to_name": doc.name,
			"attached_to_field": "original_file",
		}
	).insert(ignore_permissions=True)
	return doc.name


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
