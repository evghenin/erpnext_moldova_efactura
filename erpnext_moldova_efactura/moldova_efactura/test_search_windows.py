# Copyright (c) 2026, Evgheni Nemerenco and Contributors
# See license.txt

from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime

from erpnext_moldova_efactura.api_client import EFacturaAPIError
from erpnext_moldova_efactura.utils.search_windows import iter_search_invoices


class TestSearchWindows(FrappeTestCase):
	def test_search_calls_once_for_full_range(self):
		start = get_datetime("2026-01-01 00:00:00")
		end = get_datetime("2026-01-22 12:00:00")
		client = _FakeSearchClient()
		rows = list(
			iter_search_invoices(
				client,
				actor_role=2,
				invoice_status=7,
				date_from=start,
				date_to=end,
				error_title="test",
			)
		)
		self.assertEqual(len(client.calls), 1)
		self.assertEqual(client.calls[0]["IssuedOn"]["StartDate"], start)
		self.assertEqual(client.calls[0]["IssuedOn"]["EndDate"], end)
		self.assertEqual(len(rows), 1)

	def test_search_swaps_reversed_bounds(self):
		start = get_datetime("2026-03-03 10:00:00")
		end = get_datetime("2026-03-01 10:00:00")
		client = _FakeSearchClient()
		list(
			iter_search_invoices(
				client,
				actor_role=1,
				invoice_status=5,
				date_from=start,
				date_to=end,
				error_title="test",
			)
		)
		issued = client.calls[0]["IssuedOn"]
		self.assertEqual(issued["StartDate"], end)
		self.assertEqual(issued["EndDate"], start)

	def test_search_strips_issued_on_microseconds(self):
		start = get_datetime("2026-05-24 00:01:25.545771")
		end = get_datetime("2026-05-31 00:01:25.545771")
		client = _FakeSearchClient()
		list(
			iter_search_invoices(
				client,
				actor_role=1,
				invoice_status=5,
				date_from=start,
				date_to=end,
				error_title="test",
			)
		)
		issued = client.calls[0]["IssuedOn"]
		self.assertEqual(issued["StartDate"].microsecond, 0)
		self.assertEqual(issued["EndDate"].microsecond, 0)
		self.assertEqual(issued["StartDate"].replace(microsecond=0), start.replace(microsecond=0))

	def test_search_does_not_split_on_sfs_fault(self):
		start = get_datetime("2025-09-21 00:37:41")
		end = get_datetime("2026-09-21 00:37:41")
		client = _FailingSearchClient(EFacturaAPIError("SOAP Fault in SearchInvoices: Unknown fault occured"))
		rows = list(
			iter_search_invoices(
				client,
				actor_role=1,
				invoice_status=5,
				date_from=start,
				date_to=end,
				error_title="test",
			)
		)
		self.assertEqual(len(client.calls), 1)
		self.assertEqual(rows, [])
		span = client.calls[0]["IssuedOn"]["EndDate"] - client.calls[0]["IssuedOn"]["StartDate"]
		self.assertGreater(span.total_seconds(), 300 * 24 * 3600)


class _FakeSearchClient:
	def __init__(self):
		self.calls = []

	def search_invoices(self, actor_role, parameters, request_id=None):
		self.calls.append(parameters)
		return {"Results": {"Invoice": [{"Seria": "A", "Number": str(len(self.calls))}]}}


class _FailingSearchClient:
	def __init__(self, error):
		self.error = error
		self.calls = []

	def search_invoices(self, actor_role, parameters, request_id=None):
		self.calls.append(parameters)
		raise self.error
