"""Split SearchInvoices date filters into short IssuedOn windows.

SFS SearchInvoices has no page/skip fields. Chunking by IssuedOn keeps each
SOAP response small when the lookback is months long.
"""

from __future__ import annotations

from collections.abc import Iterator

import frappe
from frappe.utils import add_days

from erpnext_moldova_efactura.utils.api_response import extract_invoices

SEARCH_WINDOW_DAYS = 7


def iter_issued_on_windows(date_from, date_to, days: int = SEARCH_WINDOW_DAYS):
	"""Yield (start, end) windows covering [date_from, date_to].

	Adjacent windows share the boundary timestamp so an inclusive IssuedOn
	filter cannot skip an invoice that sits exactly on the cut.
	"""
	if not date_from or not date_to:
		return
	if date_from > date_to:
		date_from, date_to = date_to, date_from
	days = max(int(days or SEARCH_WINDOW_DAYS), 1)
	if date_from == date_to:
		yield date_from, date_to
		return

	cursor = date_from
	while cursor < date_to:
		window_end = add_days(cursor, days)
		if window_end > date_to:
			window_end = date_to
		yield cursor, window_end
		if window_end >= date_to:
			return
		cursor = window_end


def iter_search_invoices(
	client,
	*,
	actor_role: int,
	invoice_status: int,
	date_from,
	date_to,
	error_title: str,
) -> Iterator[dict]:
	"""Call SearchInvoices once per IssuedOn window and yield invoice rows."""
	for start, end in iter_issued_on_windows(date_from, date_to):
		params = {
			"InvoiceStatus": invoice_status,
			"IssuedOn": {"StartDate": start, "EndDate": end},
		}
		try:
			resp = client.search_invoices(actor_role=actor_role, parameters=params)
		except Exception:
			frappe.log_error(
				title=f"{error_title} {start}..{end}",
				message=frappe.get_traceback(),
			)
			continue
		yield from extract_invoices(resp)
