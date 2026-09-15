import unittest
from unittest.mock import Mock

from erpnext_moldova_efactura.api_client import EFacturaAPIError
from erpnext_moldova_efactura.utils.api_response import check_invoices_status_map


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


class TestCheckInvoicesStatusMap(unittest.TestCase):
	def test_uses_one_batch_check(self):
		client = Mock()
		client.check_invoices_status.return_value = _check_response("EBL", "000501857", 8)
		identifiers = [{"Seria": "EBL", "Number": "000501857"}]

		self.assertEqual(check_invoices_status_map(client, identifiers), {("EBL", "000501857"): 8})
		client.check_invoices_status.assert_called_once_with(seria_and_numbers=identifiers)

	def test_does_not_retry_per_identifier_when_batch_faults(self):
		client = Mock()
		fault = EFacturaAPIError("SOAP Fault in CheckInvoicesStatus: Unknown fault occured")
		client.check_invoices_status.side_effect = fault
		identifiers = [
			{"Seria": "EBL", "Number": "000501857"},
			{"Seria": "EBL", "Number": "bad"},
		]

		with self.assertRaises(EFacturaAPIError):
			check_invoices_status_map(client, identifiers)
		client.check_invoices_status.assert_called_once_with(seria_and_numbers=identifiers)
