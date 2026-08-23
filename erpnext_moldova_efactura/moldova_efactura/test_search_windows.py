# Copyright (c) 2026, Evgheni Nemerenco and Contributors
# See license.txt

from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime

from erpnext_moldova_efactura.utils.search_windows import iter_issued_on_windows, iter_search_invoices


class TestSearchWindows(FrappeTestCase):
	def test_seven_day_windows_cover_lookback(self):
		start = get_datetime("2026-01-01 00:00:00")
		end = get_datetime("2026-01-22 12:00:00")
		windows = list(iter_issued_on_windows(start, end, days=7))
		self.assertEqual(windows[0][0], start)
		self.assertEqual(windows[-1][1], end)
		self.assertEqual(len(windows), 4)
		for i in range(1, len(windows)):
			self.assertEqual(windows[i][0], windows[i - 1][1])

	def test_short_range_is_one_window(self):
		start = get_datetime("2026-03-01 10:00:00")
		end = get_datetime("2026-03-03 10:00:00")
		windows = list(iter_issued_on_windows(start, end, days=7))
		self.assertEqual(windows, [(start, end)])

	def test_equal_bounds_still_yield(self):
		ts = get_datetime("2026-04-01 00:00:00")
		self.assertEqual(list(iter_issued_on_windows(ts, ts, days=7)), [(ts, ts)])

	def test_search_calls_once_per_window(self):
		start = get_datetime("2026-01-01 00:00:00")
		end = get_datetime("2026-01-15 12:00:00")
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
		self.assertEqual(len(client.calls), 3)
		self.assertEqual(client.calls[0]["IssuedOn"]["StartDate"], start)
		self.assertEqual(client.calls[-1]["IssuedOn"]["EndDate"], end)
		self.assertEqual(len(rows), 3)


class _FakeSearchClient:
	def __init__(self):
		self.calls = []

	def search_invoices(self, actor_role, parameters, request_id=None):
		self.calls.append(parameters)
		return {"Results": {"Invoice": [{"Seria": "A", "Number": str(len(self.calls))}]}}
