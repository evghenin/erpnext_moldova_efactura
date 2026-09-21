"""One SearchInvoices call for the full IssuedOn range.

SFS faults on this method come from service overload (minute 0 and midnight),
not from the length of the date range. Do not split the range into windows.
"""

from __future__ import annotations

from collections.abc import Iterator

import frappe
from frappe.utils import get_datetime

from erpnext_moldova_efactura.api_client import EFacturaAPIError
from erpnext_moldova_efactura.utils.api_response import extract_invoices


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
) -> Iterator[dict]:
	"""Call SearchInvoices once for [date_from, date_to] and yield invoice rows."""
	if not date_from or not date_to:
		return
	if date_from > date_to:
		date_from, date_to = date_to, date_from
	params = {
		"InvoiceStatus": invoice_status,
		"IssuedOn": {
			"StartDate": _sfs_datetime(date_from),
			"EndDate": _sfs_datetime(date_to),
		},
	}
	try:
		resp = client.search_invoices(actor_role=actor_role, parameters=params)
	except EFacturaAPIError:
		# api_client already wrote the SOAP fault to the Error Log.
		return
	except Exception:
		frappe.log_error(title=error_title, message=frappe.get_traceback())
		return
	yield from extract_invoices(resp)
