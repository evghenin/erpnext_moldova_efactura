"""One SearchInvoices call for the full IssuedOn range.

SFS SOAP faults on this method come from service overload (minute 0 and midnight),
not from the length of the date range. Do not split the range on those faults.
HTTP 500 on the cancelled-invoice pass is retried once, then sliced by 90 days.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import timedelta

import frappe
from frappe.utils import get_datetime

from erpnext_moldova_efactura.api_client import EFacturaAPIError
from erpnext_moldova_efactura.utils.api_response import extract_invoices

HTTP_500_RETRY_SECONDS = 5
HTTP_500_SLICE_DAYS = 90


def _sfs_datetime(value):
	"""SFS DateTime fields reject microseconds (generic SOAP Fault)."""
	if value is None:
		return value
	parsed = value
	replace = getattr(parsed, "replace", None)
	if not callable(replace):
		try:
			parsed = get_datetime(value)
		except Exception:
			return value
		replace = getattr(parsed, "replace", None)
	if callable(replace):
		try:
			return replace(microsecond=0)
		except TypeError:
			return parsed
	return parsed


def iter_search_invoices(
	client,
	*,
	actor_role: int,
	invoice_status: int,
	date_from,
	date_to,
	error_title: str,
	recover_http_500: bool = False,
) -> Iterator[dict]:
	"""Call SearchInvoices once for [date_from, date_to] and yield invoice rows."""
	if not date_from or not date_to:
		return
	if date_from > date_to:
		date_from, date_to = date_to, date_from
	outcome = _search_once(client, actor_role, invoice_status, date_from, date_to, error_title)
	if isinstance(outcome, list):
		yield from outcome
		return
	if not recover_http_500 or not _is_http_500(outcome):
		return
	time.sleep(HTTP_500_RETRY_SECONDS)
	outcome = _search_once(client, actor_role, invoice_status, date_from, date_to, error_title)
	if isinstance(outcome, list):
		yield from outcome
		return
	if not _is_http_500(outcome):
		return
	if (date_to - date_from) <= timedelta(days=HTTP_500_SLICE_DAYS):
		return
	cursor = date_from
	while cursor < date_to:
		nxt = min(cursor + timedelta(days=HTTP_500_SLICE_DAYS), date_to)
		part = _search_once(client, actor_role, invoice_status, cursor, nxt, error_title)
		if isinstance(part, list):
			yield from part
		cursor = nxt


def _is_http_500(outcome) -> bool:
	return isinstance(outcome, EFacturaAPIError) and "HTTP 500" in str(outcome)


def _search_once(client, actor_role, invoice_status, date_from, date_to, error_title):
	params = {
		"InvoiceStatus": invoice_status,
		"IssuedOn": {
			"StartDate": _sfs_datetime(date_from),
			"EndDate": _sfs_datetime(date_to),
		},
	}
	try:
		resp = client.search_invoices(actor_role=actor_role, parameters=params)
	except EFacturaAPIError as exc:
		# api_client already wrote the SOAP fault to the Error Log.
		return exc
	except Exception:
		frappe.log_error(title=error_title, message=frappe.get_traceback())
		return None
	return extract_invoices(resp)
