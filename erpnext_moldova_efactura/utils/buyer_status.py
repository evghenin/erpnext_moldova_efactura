"""Map e-Factura InvoiceStatus codes to Purchase eFactura status labels."""

from __future__ import annotations

import frappe
from frappe.utils import cint

# SFS InvoiceStatus → UI label (buyer). Status 0 (Draft) does not apply to incoming.
BUYER_STATUS_MAP = {
	1: "Signed by Supplier",
	2: "Rejected",
	3: "Accepted",
	4: "Signing",
	5: "Canceled by Supplier",
	6: "Archived",
	7: "Sent to Buyer",
	8: "Signed by Buyer",
	9: "Sent to Buyer",
	10: "Transportation",
	11: "Cancellation Requested",
}

SFS_CANCELED_BY_SUPPLIER = 5
SFS_ARCHIVED = 6

# Statuses that typically appear in buyer inbox and should be fetched
BUYER_SEARCH_STATUSES = (7, 9, 1, 8, 3, 2, 10, 5, 11)

# Buyer still needs to accept/sign (SFS InvoiceStatus codes — API search only)
BUYER_ACTIONABLE_STATUSES = (1, 7, 9)
BUYER_SIGNABLE_STATUSES = (1, 7, 9, 3)
BUYER_ACTIONABLE_LABELS = ("Signed by Supplier", "Sent to Buyer")
BUYER_SIGNABLE_LABELS = ("Signed by Supplier", "Sent to Buyer", "Accepted")
CANCELED_BY_SUPPLIER_LABEL = "Canceled by Supplier"


def _status_int(value):
	if value is None or value == "":
		return None
	if isinstance(value, bool):
		return None
	if isinstance(value, int):
		return value
	text = str(value).strip()
	if text.lstrip("-").isdigit():
		return int(text)
	return None


def is_buyer_actionable_status(ef_status) -> bool:
	if status_label(ef_status) in BUYER_ACTIONABLE_LABELS:
		return True
	code = _status_int(ef_status)
	return code in BUYER_ACTIONABLE_STATUSES if code is not None else False


def is_buyer_signable_status(ef_status) -> bool:
	if status_label(ef_status) in BUYER_SIGNABLE_LABELS:
		return True
	code = _status_int(ef_status)
	return code in BUYER_SIGNABLE_STATUSES if code is not None else False


# Legacy suffix stripped from stored status; no longer written.
PI_LINKED_SUFFIX = " · Linked to PI"


def status_label(ef_status) -> str:
	"""Map SFS InvoiceStatus int (or leftover numeric string) to stored text."""
	if ef_status is None or ef_status == "":
		return ""
	code = _status_int(ef_status)
	if code is not None:
		return BUYER_STATUS_MAP.get(code, "")
	return base_status(str(ef_status).strip())


def compose_buyer_status(ef_status, purchase_invoice: str | None = None) -> str:
	"""SFS workflow state. `purchase_invoice` is ignored (kept for callers)."""
	return status_label(ef_status)


def base_status(status: str | None) -> str:
	"""Normalize stored status, including leftover PI linkage suffix."""
	value = status or ""
	if value.endswith(PI_LINKED_SUFFIX):
		return value[: -len(PI_LINKED_SUFFIX)]
	if value == "Linked to PI":
		return "Signed by Buyer"
	if value in ("Awaiting Action", "New"):
		return "Sent to Buyer"
	return value


def is_canceled_by_supplier(ef_status) -> bool:
	if status_label(ef_status) == CANCELED_BY_SUPPLIER_LABEL:
		return True
	try:
		return int(ef_status) == SFS_CANCELED_BY_SUPPLIER
	except (TypeError, ValueError):
		return False


def _setting_flag(fieldname: str, default: bool) -> bool:
	if not frappe.get_meta("eFactura Settings").has_field(fieldname):
		return default
	val = frappe.db.get_single_value("eFactura Settings", fieldname)
	if val is None:
		return default
	return bool(cint(val))


def do_not_create_cancelled_invoices() -> bool:
	"""Default on: skip inserting new PEF for invoices already cancelled in SFS."""
	return _setting_flag("do_not_create_cancelled_invoices", True)


def load_archived_purchase_efactura() -> bool:
	"""Default off: SearchInvoices does not fetch SFS status Archived (6) for PEF."""
	return _setting_flag("load_archived_purchase_efactura", False)


def load_archived_sales_efactura() -> bool:
	"""Default off: SearchInvoices does not fetch SFS status Archived (6) for SEF."""
	return _setting_flag("load_archived_sales_efactura", False)


def buyer_search_statuses() -> tuple[int, ...]:
	if load_archived_purchase_efactura():
		return BUYER_SEARCH_STATUSES + (SFS_ARCHIVED,)
	return BUYER_SEARCH_STATUSES


def should_create_incoming(ef_status) -> bool:
	if not do_not_create_cancelled_invoices():
		return True
	return not is_canceled_by_supplier(ef_status)
