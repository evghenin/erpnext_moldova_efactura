"""Sales eFactura Type / recipient party helpers."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, get_link_to_form

from erpnext_moldova_efactura.utils.party import (
	find_customer_by_idno,
	find_supplier_by_idno,
	get_supplier_idno,
	get_supplier_idno_field,
	normalize_idno,
	throw_if_customer_idno_mismatch,
)


def is_non_transfer(doc) -> bool:
	return (getattr(doc, "type", None) or "").strip() == "Non-Transfer"


def is_sef_return(doc) -> bool:
	from frappe.utils import cint

	return is_non_transfer(doc) and cint(getattr(doc, "is_return", 0))


def expected_party_type(doc) -> str:
	return "Supplier" if is_sef_return(doc) else "Customer"


def _doc_get(doc, field):
	if callable(getattr(doc, "get", None)):
		return doc.get(field)
	return getattr(doc, field, None)


def _has_party_fields(doc) -> bool:
	meta = getattr(doc, "meta", None)
	return bool(meta and meta.has_field("customer_party"))


def party_type(doc) -> str:
	return (_doc_get(doc, "customer_party_type") or expected_party_type(doc) or "").strip() or "Customer"


def sef_customer(doc) -> str | None:
	if party_type(doc) != "Customer":
		return None
	if _has_party_fields(doc):
		return _doc_get(doc, "customer_party") or None
	return _doc_get(doc, "customer") or None


def sef_supplier(doc) -> str | None:
	if party_type(doc) != "Supplier":
		return None
	return _doc_get(doc, "customer_party") or None


def find_party_by_idno(party_doctype: str, idno: str | None) -> str | None:
	if party_doctype == "Supplier":
		return find_supplier_by_idno(idno)
	return find_customer_by_idno(idno)


def resolve_xml_customer_party(doc) -> None:
	"""Set party type from Type and resolve customer_party by XML Buyer IDNO.

	On type change the previous party is replaced. A party of the same type is kept.
	"""
	if not _has_party_fields(doc):
		return
	expected = expected_party_type(doc)
	current = (_doc_get(doc, "customer_party_type") or "").strip()
	prev_type = ""
	if callable(getattr(doc, "is_new", None)) and not doc.is_new():
		prev = doc.get_doc_before_save() if callable(getattr(doc, "get_doc_before_save", None)) else None
		if prev:
			prev_type = (_doc_get(prev, "customer_party_type") or "").strip()
	real_switch = bool(current) and expected != current
	if bool(prev_type) and expected != prev_type:
		real_switch = True
	doc.customer_party_type = expected
	found = find_party_by_idno(expected, _doc_get(doc, "ef_customer_idno"))
	if real_switch:
		doc.customer_party = found or None
	elif not _doc_get(doc, "customer_party") and found:
		doc.customer_party = found


def throw_if_sef_party_idno_mismatch(doc) -> None:
	"""XML Buyer IDNO must match the linked Customer or Supplier."""
	idno = getattr(doc, "ef_customer_idno", None)
	if party_type(doc) != "Supplier":
		throw_if_customer_idno_mismatch(sef_customer(doc), idno)
		return
	supplier = sef_supplier(doc)
	if not supplier:
		return
	expected = normalize_idno(idno)
	if not expected or not get_supplier_idno_field():
		return
	actual = get_supplier_idno(supplier)
	if normalize_idno(actual) == expected:
		return
	frappe.throw(
		_("Supplier {0} IDNO ({1}) does not match e-Factura buyer IDNO {2}").format(
			get_link_to_form("Supplier", supplier),
			cstr(actual).strip() or _("not set"),
			cstr(idno).strip(),
		),
		title=_("Supplier IDNO mismatch"),
	)


def has_selling_or_stock_links(doc) -> bool:
	"""True when SEF is tied to a Sales Invoice, Delivery Note, or Purchase Receipt."""
	if (getattr(doc, "sales_invoice", None) or "").strip():
		return True
	for row in doc.get("items") or []:
		if (
			getattr(row, "sales_invoice", None)
			or getattr(row, "delivery_note", None)
			or getattr(row, "purchase_receipt", None)
		):
			return True
	name = getattr(doc, "name", None)
	if not name:
		return False
	if frappe.get_meta("Purchase Receipt").has_field("sales_efactura") and frappe.db.exists(
		"Purchase Receipt", {"sales_efactura": name, "docstatus": ["<", 2]}
	):
		return True
	return False
