"""Purchase eFactura CreationMotiv / return / party helpers."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from erpnext_moldova_efactura.utils.party import (
	find_customer_by_idno,
	find_supplier_by_idno,
	get_customer_idno,
	get_customer_idno_field,
	normalize_idno,
	throw_if_supplier_idno_mismatch,
)


def is_non_livrare(doc) -> bool:
	return (getattr(doc, "type", None) or "").strip() == "Non-Transfer"


def is_pef_return(doc) -> bool:
	return is_non_livrare(doc) and cint(getattr(doc, "is_return", 0))


def expected_party_type(doc) -> str:
	return "Customer" if cint(getattr(doc, "is_return", 0)) else "Supplier"


def party_type(doc) -> str:
	return (getattr(doc, "supplier_party_type", None) or expected_party_type(doc)).strip() or "Supplier"


def pef_supplier(doc) -> str | None:
	if party_type(doc) == "Supplier":
		return getattr(doc, "supplier_party", None) or None
	return None


def pef_customer(doc) -> str | None:
	if party_type(doc) == "Customer":
		return getattr(doc, "supplier_party", None) or None
	return None


def find_party_by_idno(party_doctype: str, idno: str | None) -> str | None:
	if party_doctype == "Customer":
		return find_customer_by_idno(idno)
	return find_supplier_by_idno(idno)


def resolve_xml_supplier_party(doc) -> None:
	"""Set party type from is_return and resolve supplier_party by XML Supplier IDNO.

	Does not change is_return. On type change the previous party is replaced.
	A party of the same type is kept when already set.
	"""
	expected = expected_party_type(doc)
	current = (getattr(doc, "supplier_party_type", None) or "").strip()
	prev_type = ""
	if callable(getattr(doc, "is_new", None)) and not doc.is_new():
		prev = doc.get_doc_before_save() if callable(getattr(doc, "get_doc_before_save", None)) else None
		if prev:
			prev_type = (prev.supplier_party_type or "").strip()
	type_changed = expected != current or (bool(prev_type) and expected != prev_type)
	doc.supplier_party_type = expected
	if type_changed or not getattr(doc, "supplier_party", None):
		doc.supplier_party = find_party_by_idno(expected, getattr(doc, "ef_supplier_idno", None))


def throw_if_pef_party_idno_mismatch(doc) -> None:
	"""XML Supplier IDNO must match the linked Supplier or Customer."""
	idno = getattr(doc, "ef_supplier_idno", None)
	if party_type(doc) != "Customer":
		throw_if_supplier_idno_mismatch(pef_supplier(doc), idno)
		return
	customer = pef_customer(doc)
	if not customer:
		return
	expected = normalize_idno(idno)
	if not expected or not get_customer_idno_field():
		return
	actual = get_customer_idno(customer)
	if normalize_idno(actual) == expected:
		return
	from frappe.utils import cstr, get_link_to_form

	frappe.throw(
		_("Customer {0} IDNO ({1}) does not match e-Factura supplier IDNO {2}").format(
			get_link_to_form("Customer", customer),
			cstr(actual).strip() or _("not set"),
			cstr(idno).strip(),
		),
		title=_("Customer IDNO mismatch"),
	)


def has_buying_or_stock_links(doc) -> bool:
	"""True when PEF is already tied to PI, PR, DN, or PO."""
	for row in doc.get("items") or []:
		if (
			getattr(row, "purchase_invoice", None)
			or getattr(row, "purchase_receipt", None)
			or getattr(row, "delivery_note", None)
		):
			return True
	name = getattr(doc, "name", None)
	if not name:
		return False
	for dt in ("Purchase Order", "Purchase Receipt", "Delivery Note"):
		if frappe.get_meta(dt).has_field("purchase_efactura") and frappe.db.exists(
			dt, {"purchase_efactura": name, "docstatus": ["<", 2]}
		):
			return True
	return False


def throw_if_pi_path_blocked(doc) -> None:
	if is_non_livrare(doc):
		frappe.throw(
			_("Purchase Invoice and Purchase Order are not used for Non-Transfer e-Factura.")
		)
