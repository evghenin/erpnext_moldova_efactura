"""Party / IDNO helpers for Purchase eFactura."""

from __future__ import annotations

import re

import frappe
from frappe import _

_QUOTE_CHARS = '"\'«»„“”‘’`'
_LEADING_SC = re.compile(r"^\s*(?:S\s*\.\s*C\s*\.?|SC\b)\s*", flags=re.IGNORECASE)
_SRL_DOTTED = re.compile(r"\bS\s*\.\s*R\s*\.\s*L\s*\.?", flags=re.IGNORECASE)
_SRL_WORD = re.compile(r"\bSRL\b", flags=re.IGNORECASE)
_SA_DOTTED = re.compile(r"\bS\s*\.\s*A\s*\.?", flags=re.IGNORECASE)
_SA_WORD = re.compile(r"\bSA\b", flags=re.IGNORECASE)


def normalize_supplier_title(name: str | None) -> str:
	"""Uppercase supplier title: strip quotes, drop leading S.C./SC, move S.R.L./S.A. to the end."""
	if not name:
		return ""
	text = str(name).translate({ord(ch): None for ch in _QUOTE_CHARS})
	text = _LEADING_SC.sub("", text, count=1)
	had_srl = bool(_SRL_DOTTED.search(text) or _SRL_WORD.search(text))
	had_sa = bool(_SA_DOTTED.search(text) or _SA_WORD.search(text))
	text = _SRL_DOTTED.sub(" ", text)
	text = _SRL_WORD.sub(" ", text)
	text = _SA_DOTTED.sub(" ", text)
	text = _SA_WORD.sub(" ", text)
	text = " ".join(text.split()).upper()
	suffixes = []
	if had_srl:
		suffixes.append("SRL")
	if had_sa:
		suffixes.append("SA")
	if suffixes:
		return " ".join([p for p in (text, *suffixes) if p])
	return text


def normalize_idno(idno: str | None) -> str:
	"""Digits only, for comparing Moldova IDNO values with spaces or punctuation."""
	if not idno:
		return ""
	return re.sub(r"\D+", "", str(idno))


def get_supplier_idno_field() -> str | None:
	fieldname = frappe.db.get_single_value("eFactura Settings", "supplier_idno_field")
	if not fieldname or not frappe.get_meta("Supplier").has_field(fieldname):
		return None
	return fieldname


def get_supplier_idno(supplier: str | None) -> str | None:
	fieldname = get_supplier_idno_field()
	if not supplier or not fieldname:
		return None
	return frappe.db.get_value("Supplier", supplier, fieldname)


def throw_if_supplier_idno_mismatch(supplier: str | None, factura_idno: str | None) -> None:
	"""Selected Supplier must carry the same IDNO as the e-Factura XML supplier."""
	if not supplier:
		return
	expected = normalize_idno(factura_idno)
	if not expected:
		return
	fieldname = get_supplier_idno_field()
	if not fieldname:
		return
	actual_raw = get_supplier_idno(supplier)
	if normalize_idno(actual_raw) == expected:
		return
	from frappe.utils import cstr, get_link_to_form

	frappe.throw(
		_("Supplier {0} IDNO ({1}) does not match e-Factura supplier IDNO {2}").format(
			get_link_to_form("Supplier", supplier),
			cstr(actual_raw).strip() or _("not set"),
			cstr(factura_idno).strip(),
		),
		title=_("Supplier IDNO mismatch"),
	)


def get_customer_idno(customer: str | None) -> str | None:
	fieldname = get_customer_idno_field()
	if not customer or not fieldname:
		return None
	return frappe.db.get_value("Customer", customer, fieldname)


def throw_if_customer_idno_mismatch(customer: str | None, factura_idno: str | None) -> None:
	"""Selected Customer must carry the same IDNO as the e-Factura XML buyer."""
	if not customer:
		return
	expected = normalize_idno(factura_idno)
	if not expected:
		return
	fieldname = get_customer_idno_field()
	if not fieldname:
		return
	actual_raw = get_customer_idno(customer)
	if normalize_idno(actual_raw) == expected:
		return
	from frappe.utils import cstr, get_link_to_form

	frappe.throw(
		_("Customer {0} IDNO ({1}) does not match e-Factura buyer IDNO {2}").format(
			get_link_to_form("Customer", customer),
			cstr(actual_raw).strip() or _("not set"),
			cstr(factura_idno).strip(),
		),
		title=_("Customer IDNO mismatch"),
	)


def get_fiscal_territory(doctype: str | None = None) -> str | None:
	"""Territory from eFactura Settings, if it still exists and the doctype has the field."""
	territory = frappe.db.get_single_value("eFactura Settings", "fiscal_territory")
	if not territory:
		return None
	if doctype and not frappe.get_meta(doctype).has_field("territory"):
		return None
	if not frappe.db.exists("Territory", territory):
		return None
	return territory


def new_supplier_defaults(title: str | None = None, idno: str | None = None) -> dict:
	"""Values for a new Supplier created from an incoming e-Factura."""
	defaults: dict = {}
	supplier_name = normalize_supplier_title(title)
	if supplier_name:
		defaults["supplier_name"] = supplier_name

	fieldname = frappe.db.get_single_value("eFactura Settings", "supplier_idno_field")
	idno = str(idno or "").strip()
	if fieldname and idno and frappe.get_meta("Supplier").has_field(fieldname):
		defaults[fieldname] = idno
	territory = get_fiscal_territory("Supplier")
	if territory:
		defaults["territory"] = territory
	return defaults


def new_customer_defaults(
	title: str | None = None, idno: str | None = None, taxpayer_type: str | None = None
) -> dict:
	"""Values for a new Customer created from an outgoing e-Factura loaded from SFS."""
	defaults: dict = {}
	customer_name = normalize_supplier_title(title)
	if customer_name:
		defaults["customer_name"] = customer_name

	fieldname = frappe.db.get_single_value("eFactura Settings", "customer_idno_field")
	idno = str(idno or "").strip()
	if fieldname and idno and frappe.get_meta("Customer").has_field(fieldname):
		defaults[fieldname] = idno

	if taxpayer_type == "Individual":
		defaults["customer_type"] = "Individual"
	elif taxpayer_type in ("Company", "Non-Resident"):
		defaults["customer_type"] = "Company"
	territory = get_fiscal_territory("Customer")
	if territory:
		defaults["territory"] = territory
	return defaults


def get_customer_idno_field() -> str | None:
	fieldname = frappe.db.get_single_value("eFactura Settings", "customer_idno_field")
	if not fieldname or not frappe.get_meta("Customer").has_field(fieldname):
		return None
	return fieldname


def find_customer_by_idno(idno: str) -> str | None:
	"""Return Customer name matching IDNO field from eFactura Settings, or None."""
	idno = str(idno or "").strip()
	if not idno:
		return None

	fieldname = get_customer_idno_field()
	if not fieldname:
		return None

	name = frappe.db.get_value("Customer", {fieldname: idno}, "name")
	if name:
		return name
	compact = normalize_idno(idno)
	if compact and compact != idno:
		return frappe.db.get_value("Customer", {fieldname: compact}, "name")
	return None


def find_supplier_by_idno(idno: str) -> str | None:
	"""Return Supplier name matching IDNO field from eFactura Settings, or None."""
	idno = str(idno or "").strip()
	if not idno:
		return None

	fieldname = get_supplier_idno_field()
	if not fieldname:
		return None

	return frappe.db.get_value("Supplier", {fieldname: idno}, "name")


def get_default_company() -> str | None:
	"""Best-effort default company for buyer sync."""
	user_default = frappe.defaults.get_user_default("Company")
	if user_default:
		return user_default
	companies = frappe.get_all("Company", pluck="name", limit=1)
	return companies[0] if companies else None
