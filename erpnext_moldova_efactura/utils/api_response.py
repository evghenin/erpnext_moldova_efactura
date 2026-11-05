"""Normalize e-Factura SOAP response shapes."""

from __future__ import annotations

from typing import Any

from frappe import _


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
	GetInvoicesBySeriaNumber / GetInvoicesForSigning → Results.XmlInvoice (field Xml)

	Zeep may unwrap Results as a list, a `{Invoice: ...}` dict, or nest the
	payload under a `*Result` key (SearchInvoicesResult, etc.).
	"""
	if not isinstance(response, dict):
		return []

	payload = response
	if not payload.get("Results"):
		for key, inner in payload.items():
			if str(key).endswith("Result") and isinstance(inner, dict) and inner.get("Results") is not None:
				payload = inner
				break

	results = payload.get("Results")
	if isinstance(results, list):
		return [row for row in results if isinstance(row, dict)]
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


STATUS_TIMEOUT_CHUNK = 10


def is_sfs_read_timeout(exc: BaseException) -> bool:
	return "timed out" in str(exc).lower()


def check_invoices_status_map(client, identifiers: list[dict]) -> dict[tuple[str, str], int]:
	"""One CheckInvoicesStatus call for the whole list. No per-invoice retry."""
	if not identifiers:
		return {}
	return invoice_status_map(client.check_invoices_status(seria_and_numbers=identifiers))


def check_invoices_status_resilient(
	client, identifiers: list[dict], *, chunk_size: int = STATUS_TIMEOUT_CHUNK
) -> tuple[dict[tuple[str, str], int], list[dict]]:
	"""Full list first. On read timeout, retry in chunks.

	Returns the status map and the identifiers whose chunk still failed.
	"""
	if not identifiers:
		return {}, []
	try:
		return check_invoices_status_map(client, identifiers), []
	except Exception as exc:
		if not is_sfs_read_timeout(exc) or len(identifiers) <= chunk_size:
			raise
	merged: dict[tuple[str, str], int] = {}
	failed: list[dict] = []
	for start in range(0, len(identifiers), chunk_size):
		part = identifiers[start : start + chunk_size]
		try:
			merged.update(check_invoices_status_map(client, part))
		except Exception:
			failed.extend(part)
	return merged, failed


def sfs_action_error(resp) -> str | None:
	"""Parse PostAccepted/Rejected/Canceled SOAP result; None if the call succeeded."""
	if not resp:
		return _("empty response")
	if resp.get("ErrorMessage"):
		return resp.get("ErrorMessage")
	try:
		if int(resp.get("Status")) == 3:
			return _("e-Factura request failed")
	except (TypeError, ValueError):
		pass
	results = resp.get("Results") or {}
	if isinstance(results, list):
		items = results
	elif isinstance(results, dict):
		items = as_list(results.get("InvoiceResult") or results.get("Invoice"))
	else:
		items = []
	for item in items:
		try:
			if int(item.get("Status")) == 3:
				return item.get("Message") or _("e-Factura request failed")
		except (TypeError, ValueError):
			continue
	return None
