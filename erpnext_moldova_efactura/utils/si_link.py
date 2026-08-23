"""Keep Sales eFactura header and item Sales Invoice links in sync."""

from __future__ import annotations

import frappe
from frappe import _


def sales_invoice_of(doc) -> str:
	return (getattr(doc, "sales_invoice", None) or "").strip()


def sync_sales_invoice_links(doc) -> None:
	"""One Sales Invoice per document: copy header ↔ rows, reject mixed invoices."""
	header = sales_invoice_of(doc)
	seen: list[str] = []
	for row in doc.get("items") or []:
		si = (getattr(row, "sales_invoice", None) or "").strip()
		if si and si not in seen:
			seen.append(si)

	if len(seen) > 1:
		frappe.throw(_("All rows must link to the same Sales Invoice."))

	if len(seen) == 1:
		row_si = seen[0]
		if header and header != row_si:
			frappe.throw(_("Sales Invoice on the document does not match the rows."))
		doc.sales_invoice = row_si
		header = row_si

	if not header:
		return

	for row in doc.get("items") or []:
		if not (getattr(row, "sales_invoice", None) or "").strip():
			row.sales_invoice = header
