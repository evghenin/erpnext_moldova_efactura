import unittest
from unittest.mock import Mock

from erpnext_moldova_efactura.api_client import EFacturaAPIError
from erpnext_moldova_efactura.utils.api_response import status_map_with_fallback


class TestStatusMapWithFallback(unittest.TestCase):
	def test_falls_back_to_get_invoices_by_seria_number(self):
		client = Mock()
		client.check_invoices_status.side_effect = EFacturaAPIError(
			"SOAP Fault in CheckInvoicesStatus: Unknown fault occured"
		)
		client.get_invoices_by_seria_number.return_value = {
			"Results": {
				"XmlInvoice": {
					"Seria": "EBL",
					"Number": "000501857",
					"InvoiceStatus": 8,
				}
			}
		}
		identifiers = [{"Seria": "EBL", "Number": "000501857"}]

		self.assertEqual(status_map_with_fallback(client, identifiers), {("EBL", "000501857"): 8})
		client.get_invoices_by_seria_number.assert_called_once_with(identifiers)

	def test_falls_back_per_identifier_when_batch_details_also_fault(self):
		client = Mock()
		fault = EFacturaAPIError("SOAP Fault in CheckInvoicesStatus: Unknown fault occured")
		client.check_invoices_status.side_effect = fault

		def details(idents):
			if len(idents) != 1:
				raise EFacturaAPIError("SOAP Fault in GetInvoicesBySeriaNumber: Unknown fault occured")
			ident = idents[0]
			if ident["Number"] == "bad":
				raise EFacturaAPIError("SOAP Fault in GetInvoicesBySeriaNumber: Unknown fault occured")
			return {
				"Results": {
					"XmlInvoice": {
						"Seria": ident["Seria"],
						"Number": ident["Number"],
						"InvoiceStatus": 8,
					}
				}
			}

		client.get_invoices_by_seria_number.side_effect = details
		identifiers = [
			{"Seria": "EBL", "Number": "000501857"},
			{"Seria": "EBL", "Number": "bad"},
		]

		self.assertEqual(
			status_map_with_fallback(client, identifiers),
			{("EBL", "000501857"): 8},
		)
