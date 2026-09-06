from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate, now_datetime

from erpnext_moldova_efactura.utils.factura_pdf import FacturaImportError, decimal, money, parse_pdf
from erpnext_moldova_efactura.utils.party import normalize_idno

ORIGINAL_FIELDS = (
	"provider",
	"original_format",
	"original_file",
	"series",
	"number",
	"issue_date",
	"delivery_date",
	"supplier_idno",
	"supplier_name",
	"supplier_vat_id",
	"buyer_idno",
	"buyer_vat_id",
	"currency",
	"provider_reference",
	"provider_account",
	"contract_reference",
	"related_document_type",
	"related_document_number",
	"related_document_date",
	"signature_details",
	"extracted_text",
	"file_hash",
)
ORIGINAL_ITEM_FIELDS = (
	"description",
	"source_uom",
	"source_qty",
	"source_rate",
	"net_amount",
	"vat_rate",
	"vat_amount",
)
MAPPING_FIELDS = ("item_code", "uom", "qty", "conversion_factor", "expense_account", "cost_center")


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


def _identity(company, supplier_idno, series, number):
	parts = [
		company or "",
		normalize_idno(supplier_idno),
		(series or "").strip().upper(),
		(number or "").strip().upper(),
	]
	return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class PurchaseFactura(Document):
	def before_insert(self):
		if self.amended_from:
			self.reviewed = 0
			self.reviewed_by = None
			self.reviewed_on = None

	def validate(self):
		if not self.company:
			frappe.throw(_("Select a Company"))
		_lock_company(self.company)
		self.series = (self.series or "").strip().upper()
		self.number = (self.number or "").strip()
		self.supplier_idno = normalize_idno(self.supplier_idno)
		self.buyer_idno = normalize_idno(self.buyer_idno) or _party_idno("Company", self.company)
		if self.supplier and not self.supplier_idno:
			self.supplier_idno = _party_idno("Supplier", self.supplier)
		if self.buyer_idno != _party_idno("Company", self.company):
			frappe.throw(_("Buyer IDNO does not match the selected Company"))
		if self.supplier and self.supplier_idno != _party_idno("Supplier", self.supplier):
			frappe.throw(_("Supplier IDNO does not match the original factura"))
		if (
			self.service_period_start
			and self.service_period_end
			and getdate(self.service_period_start) > getdate(self.service_period_end)
		):
			frappe.throw(_("Service period end must not precede its start"))
		self._validate_original()
		self._calculate()
		self.identity_key = _identity(self.company, self.supplier_idno, self.series, self.number)
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
				str(a.get(k) or "") != str(b.get(k) or "")
				for a, b in zip(self.items, previous.items, strict=True)
				for k in ORIGINAL_ITEM_FIELDS + MAPPING_FIELDS
			)
			content_changed = changed_items or any(
				str(self.get(k) or "") != str(previous.get(k) or "")
				for k in (*ORIGINAL_FIELDS, "company", "supplier")
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
			if any(str(self.get(k) or "") != str(previous.get(k) or "") for k in ORIGINAL_FIELDS):
				frappe.throw(
					_("Imported original fields cannot be changed; register a corrected original separately")
				)
			if len(self.items) != len(previous.items) or any(
				str(a.get(k) or "") != str(b.get(k) or "")
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
				qty, rate, vat_rate = map(
					decimal, (row.source_qty or 0, row.source_rate or 0, row.vat_rate or 0)
				)
				if qty <= 0 or rate < 0 or not 0 <= vat_rate <= 100:
					frappe.throw(
						_(
							"Row {0}: positive quantities and non-negative rates are required; returns are not supported yet"
						).format(row.idx)
					)
				net = money(row.net_amount) if row.net_amount else money(qty * rate)
				vat = money(row.vat_amount) if row.vat_amount else money(net * vat_rate / 100)
				if abs(net - money(qty * rate)) > decimal("0.01") or abs(
					vat - money(net * vat_rate / 100)
				) > decimal("0.01"):
					frappe.throw(
						_("Row {0}: original quantity, rate, net and VAT amounts do not reconcile").format(
							row.idx
						)
					)
				row.net_amount, row.vat_amount, row.amount = float(net), float(vat), float(net + vat)
			except FacturaImportError as exc:
				frappe.throw(str(exc))
			if row.item_code and row.uom:
				from erpnext.stock.get_item_details import get_conversion_factor

				conversion = get_conversion_factor(row.item_code, row.uom) or {}
				row.conversion_factor = flt(conversion.get("conversion_factor"))
				if row.conversion_factor <= 0 or flt(row.qty) <= 0:
					frappe.throw(_("Row {0}: set a valid ERP UOM and positive ERP quantity").format(row.idx))
		self.net_total = sum(flt(r.net_amount) for r in self.items)
		self.vat_total = sum(flt(r.vat_amount) for r in self.items)
		self.total = sum(flt(r.amount) for r in self.items)

	def before_submit(self):
		self.require_reviewed()
		if (
			not self.purchase_invoice
			or frappe.db.get_value("Purchase Invoice", self.purchase_invoice, "docstatus") != 1
		):
			frappe.throw(_("Link and submit the Purchase Invoice before submitting this factura"))

	def require_reviewed(self):
		if self.docstatus == 2 or not self.reviewed or not self.supplier:
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
		data = parse_pdf(file_doc.get_content())
	except FacturaImportError as exc:
		frappe.throw(_(str(exc)), title=_("Cannot import factura"))
	if data["buyer_idno"] != _party_idno("Company", company):
		frappe.throw(_("The PDF recipient IDNO does not match the selected Company"))
	_lock_company(company)
	key = _identity(company, data["supplier_idno"], data["series"], data["number"])
	existing = frappe.db.get_value("Purchase Factura", {"identity_key": key, "docstatus": ["<", 2]}, "name")
	if existing:
		frappe.get_doc("Purchase Factura", existing).check_permission("read")
		return existing
	field = frappe.db.get_single_value("eFactura Settings", "supplier_idno_field") or "tax_id"
	suppliers = frappe.get_list(
		"Supplier", filters={field: data["supplier_idno"]}, pluck="name", limit_page_length=2
	)
	data["supplier"] = suppliers[0] if len(suppliers) == 1 else None
	if data["supplier"]:
		from erpnext_moldova_efactura.utils.item_map import resolve_item_and_uom
		from erpnext_moldova_efactura.utils.uom_map import resolve_uom

		for row in data["items"]:
			item_code, mapped_uom = resolve_item_and_uom(data["supplier"], None, row["description"])
			if not item_code:
				continue
			item = frappe.get_cached_value("Item", item_code, ["purchase_uom", "stock_uom"], as_dict=True)
			row["item_code"] = item_code
			row["uom"] = mapped_uom or resolve_uom(row["source_uom"]) or item.purchase_uom or item.stock_uom
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
def party_defaults(company=None, supplier=None):
	frappe.has_permission("Purchase Factura", "create", throw=True)
	result = {}
	for doctype, name, key in (("Company", company, "buyer_idno"), ("Supplier", supplier, "supplier_idno")):
		if name:
			frappe.get_doc(doctype, name).check_permission("read")
			result[key] = _party_idno(doctype, name)
	return result
