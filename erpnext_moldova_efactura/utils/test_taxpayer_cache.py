from datetime import timedelta
from unittest.mock import Mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from erpnext_moldova_efactura.moldova_efactura.doctype.efactura_settings.efactura_settings import (
	clear_taxpayer_cache as clear_taxpayer_cache_api,
)
from erpnext_moldova_efactura.utils.taxpayer_cache import (
	CACHE_TTL_DAYS,
	_write,
	clear_taxpayer_cache,
)


def _taxpayer(fiscal_code, name):
	return {
		"IDNO": fiscal_code,
		"CodTVA": "",
		"Name": name,
		"Address": "Chisinau",
		"TaxpayerType": 1,
		"IsEFacturaActor": True,
	}


def _response(*rows, request_id="req-live"):
	return {"RequestId": request_id, "Status": 2, "Results": {"Taxpayer": list(rows)}}


def _client(response):
	client = EFacturaAPIClient.__new__(EFacturaAPIClient)
	client._call = Mock(return_value=response)
	client._new_request_id = lambda: "req-cached"
	return client


class TestTaxpayerCache(FrappeTestCase):
	def setUp(self):
		super().setUp()
		clear_taxpayer_cache()

	def tearDown(self):
		clear_taxpayer_cache()
		super().tearDown()

	def test_second_lookup_does_not_call_sfs(self):
		row = _taxpayer("1024600026571", "Hotel Life")
		client = _client(_response(row))

		first = client.get_taxpayers_info(["1024600026571"])
		second = client.get_taxpayers_info([" 1024600026571 "])

		client._call.assert_called_once()
		self.assertEqual(first["Results"]["Taxpayer"][0]["Name"], "Hotel Life")
		self.assertEqual(second["RequestId"], "req-cached")
		self.assertEqual(second["Results"]["Taxpayer"][0]["IDNO"], "1024600026571")

	def test_partial_hit_requests_only_missing_codes(self):
		_write("1000000000001", _taxpayer("1000000000001", "Cached"))
		client = _client(_response(_taxpayer("1000000000002", "Fresh"), request_id="req-2"))

		result = client.get_taxpayers_info(["1000000000001", "1000000000002"])

		requested = client._call.call_args.kwargs["request"]["FiscalCodes"]["string"]
		self.assertEqual(requested, ["1000000000002"])
		self.assertEqual(
			[row["Name"] for row in result["Results"]["Taxpayer"]],
			["Cached", "Fresh"],
		)
		self.assertEqual(result["RequestId"], "req-2")

	def test_failed_response_is_not_cached(self):
		client = _client({"RequestId": "req-err", "Status": 3, "ErrorMessage": "nope"})

		first = client.get_taxpayers_info(["1000000000003"])
		second = client.get_taxpayers_info(["1000000000003"])

		self.assertEqual(first["ErrorMessage"], "nope")
		self.assertEqual(client._call.call_count, 2)
		self.assertEqual(second["ErrorMessage"], "nope")

	def test_entry_older_than_ttl_is_fetched_again(self):
		_write(
			"1000000000004",
			_taxpayer("1000000000004", "Stale"),
			fetched_on=now_datetime() - timedelta(days=CACHE_TTL_DAYS + 1),
		)
		client = _client(_response(_taxpayer("1000000000004", "Fresh")))

		result = client.get_taxpayers_info(["1000000000004"])

		client._call.assert_called_once()
		self.assertEqual(result["Results"]["Taxpayer"][0]["Name"], "Fresh")

	def test_clear_button_drops_cached_rows(self):
		_write("1000000000005", _taxpayer("1000000000005", "One"))
		_write("1000000000006", _taxpayer("1000000000006", "Two"))

		result = clear_taxpayer_cache_api()

		self.assertEqual(result["cleared"], 2)
		client = _client(_response(_taxpayer("1000000000005", "Again")))
		client.get_taxpayers_info(["1000000000005"])
		client._call.assert_called_once()

	def test_cache_key_is_persistent_across_clear_cache(self):
		self.assertIn("efactura_taxpayer:*", frappe.get_hooks("persistent_cache_keys"))
