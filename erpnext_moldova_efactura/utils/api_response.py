"""Normalize e-Factura SOAP response shapes."""

from __future__ import annotations

from typing import Any


def as_list(value) -> list:
	if not value:
		return []
	if isinstance(value, dict):
		return [value]
	if isinstance(value, list):
		return value
	return []


def extract_invoices(response: dict | None) -> list[dict[str, Any]]:
	"""
	SearchInvoices → Results.Invoice
	GetInvoicesBySeriaNumber → Results.XmlInvoice (field Xml)
	"""
	if not isinstance(response, dict):
		return []

	results = response.get("Results") or {}
	if not isinstance(results, dict):
		return []

	for key in ("XmlInvoice", "Invoice", "InvoiceHeader"):
		items = as_list(results.get(key))
		if items:
			return items
	return []


def invoice_xml(invoice: dict | None) -> str:
	if not invoice:
		return ""
	return invoice.get("Xml") or invoice.get("XML") or invoice.get("xml") or ""


def invoice_status_map(response: dict | None) -> dict[tuple[str, str], int]:
	"""Return {(Seria, Number): InvoiceStatus} from CheckInvoicesStatus / search."""
	out: dict[tuple[str, str], int] = {}
	for inv in extract_invoices(response):
		seria = str(inv.get("Seria") or "").strip()
		number = str(inv.get("Number") or "").strip()
		try:
			status = int(inv.get("InvoiceStatus"))
		except (TypeError, ValueError):
			continue
		if seria and number:
			out[(seria, number)] = status
	return out
