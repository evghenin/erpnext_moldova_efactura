"""Map e-Factura InvoiceStatus codes to Purchase eFactura status labels."""

from __future__ import annotations

# SFS InvoiceStatus → UI label (buyer). Status 0 (Draft) does not apply to incoming.
BUYER_STATUS_MAP = {
	1: "Signed by Supplier",
	2: "Rejected",
	3: "Accepted",
	4: "Signing",
	5: "Canceled by Supplier",
	7: "Sent to Buyer",
	8: "Signed by Buyer",
	9: "Sent to Buyer",
	10: "Transportation",
	11: "Cancellation Requested",
}

# Statuses that typically appear in buyer inbox and should be fetched
BUYER_SEARCH_STATUSES = (7, 9, 1, 8, 3, 2, 10, 5, 11)

# Buyer still needs to accept/sign
BUYER_ACTIONABLE_STATUSES = (1, 7, 9)
BUYER_SIGNABLE_STATUSES = (1, 7, 9, 3)

# Legacy suffix stripped from stored status; no longer written.
PI_LINKED_SUFFIX = " · Linked to PI"


def status_label(ef_status) -> str:
	if ef_status is None or ef_status == "":
		return ""
	try:
		code = int(ef_status)
	except (TypeError, ValueError):
		return ""
	return BUYER_STATUS_MAP.get(code, "")


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
