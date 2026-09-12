import unittest
from unittest.mock import Mock

from erpnext_moldova_efactura.api_client import EFacturaAPIError
from erpnext_moldova_efactura.utils.api_response import status_map_with_fallback


def _check_response(seria, number, status):
	return {
		"Results": {
			"Invoice": {
				"Seria": seria,
				"Number": number,
				"InvoiceStatus": status,
			}
		}
	}


class TestStatusMapWithFallback(unittest.TestCase):
	def test_uses_batch_check_when_sfs_accepts_it(self):
		client = Mock()
		client.check_invoices_status.return_value = _check_response("EBL", "000501857", 8)
		identifiers = [{"Seria": "EBL", "Number": "000501857"}]

		self.assertEqual(status_map_with_fallback(client, identifiers), {("EBL", "000501857"): 8})
		client.check_invoices_status.assert_called_once_with(seria_and_numbers=identifiers)
		client.get_invoices_by_seria_number.assert_not_called()

	def test_does_not_download_xml_when_single_check_faults(self):
		client = Mock()
		client.check_invoices_status.side_effect = EFacturaAPIError(
			"SOAP Fault in CheckInvoicesStatus: Unknown fault occured"
		)
		identifiers = [{"Seria": "EBL", "Number": "000501857"}]

		self.assertEqual(status_map_with_fallback(client, identifiers), {})
		client.get_invoices_by_seria_number.assert_not_called()

	def test_retries_check_per_identifier_when_batch_faults(self):
		client = Mock()
		fault = EFacturaAPIError("SOAP Fault in CheckInvoicesStatus: Unknown fault occured")

		def check(seria_and_numbers=None, **_kwargs):
			if len(seria_and_numbers) != 1:
				raise fault
			ident = seria_and_numbers[0]
			if ident["Number"] == "bad":
				raise fault
			return _check_response(ident["Seria"], ident["Number"], 8)

		client.check_invoices_status.side_effect = check
		identifiers = [
			{"Seria": "EBL", "Number": "000501857"},
			{"Seria": "EBL", "Number": "bad"},
		]

		self.assertEqual(
			status_map_with_fallback(client, identifiers),
			{("EBL", "000501857"): 8},
		)
		client.get_invoices_by_seria_number.assert_not_called()
