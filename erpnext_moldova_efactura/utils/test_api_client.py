import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from zeep.exceptions import Fault, TransportError

from erpnext_moldova_efactura.api_client import EFacturaAPIClient, EFacturaAPIError


def _client(service_method, http_status=0):
	client = EFacturaAPIClient.__new__(EFacturaAPIClient)
	client.password = "secret-pass"
	client._history = SimpleNamespace(last_sent=None, last_received=None)
	client._transport = SimpleNamespace(last_status_code=http_status)
	client.service = SimpleNamespace(PostInvoices=service_method)
	return client


class TestApiClientFailureLogging(unittest.TestCase):
	@patch("erpnext_moldova_efactura.api_client.frappe.log_error")
	def test_soap_fault_logs_payload_and_redacts_password(self, log_error):
		method = Mock(side_effect=Fault("Unknown fault occured"))
		client = _client(method)

		with self.assertRaises(EFacturaAPIError):
			client._call("PostInvoices", request={"RequestId": "EF-1", "InvoicesXml": "xml secret-pass"})

		log_error.assert_called_once()
		kwargs = log_error.call_args.kwargs
		self.assertEqual(kwargs["title"], "SFS API PostInvoices failed")
		self.assertIn("Unknown fault occured", kwargs["message"])
		self.assertIn("EF-1", kwargs["message"])
		self.assertNotIn("secret-pass", kwargs["message"])
		self.assertIn("***", kwargs["message"])

	@patch("erpnext_moldova_efactura.api_client.frappe.log_error")
	def test_error_message_response_is_logged(self, log_error):
		method = Mock(return_value={"ErrorMessage": "nope", "Status": 3})
		client = _client(method)

		resp = client._call("PostInvoices", request={"RequestId": "EF-2"})

		self.assertEqual(resp["ErrorMessage"], "nope")
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.kwargs["title"], "SFS API PostInvoices failed")
		self.assertIn("nope", log_error.call_args.kwargs["message"])
		self.assertIn("EF-2", log_error.call_args.kwargs["message"])

	@patch("erpnext_moldova_efactura.api_client.frappe.log_error")
	def test_successful_response_is_not_logged(self, log_error):
		method = Mock(return_value={"Status": 1, "TotalInvoicesPosted": 1})
		client = _client(method)

		resp = client._call("PostInvoices", request={"RequestId": "EF-3"})

		self.assertEqual(resp["Status"], 1)
		log_error.assert_not_called()

	def test_error_title_includes_single_seria_number(self):
		self.assertEqual(
			EFacturaAPIClient._request_ident_label(
				{
					"SeriaAndNumbers": {
						"InvoiceIndentificator": [{"Seria": "EBF", "Number": "000000143"}]
					}
				}
			),
			"EBF000000143",
		)
		self.assertEqual(
			EFacturaAPIClient._request_ident_label(
				{
					"SeriaAndNumbers": {
						"InvoiceIndentificator": [
							{"Seria": "EBF", "Number": "1"},
							{"Seria": "EBF", "Number": "2"},
						]
					}
				}
			),
			"(2 invoices)",
		)

	@patch("erpnext_moldova_efactura.api_client.frappe.log_error")
	def test_soap_fault_title_includes_invoice_id(self, log_error):
		method = Mock(side_effect=Fault("Unknown fault occured"))
		client = _client(method)
		client.service = SimpleNamespace(CheckInvoicesStatus=method)

		with self.assertRaises(EFacturaAPIError):
			client._call(
				"CheckInvoicesStatus",
				request={
					"RequestId": "EF-4",
					"SeriaAndNumbers": {
						"InvoiceIndentificator": [{"Seria": "EBF", "Number": "999999999"}]
					},
				},
			)

		self.assertEqual(
			log_error.call_args.kwargs["title"],
			"SFS API CheckInvoicesStatus failed EBF999999999",
		)

	@patch("erpnext_moldova_efactura.api_client.frappe.log_error")
	def test_soap_fault_over_http_500_omits_response_body(self, log_error):
		method = Mock(side_effect=Fault("Unknown fault occured"))
		client = _client(method, http_status=500)
		client.service = SimpleNamespace(CheckInvoicesStatus=method)
		client._history = SimpleNamespace(
			last_sent={"envelope": None},
			last_received={"envelope": object()},
		)
		client._dump_soap_envelope = Mock(return_value="<Fault>huge body</Fault>")

		with self.assertRaises(EFacturaAPIError):
			client._call(
				"CheckInvoicesStatus",
				request={
					"RequestId": "EF-7",
					"SeriaAndNumbers": {
						"InvoiceIndentificator": [{"Seria": "EBI", "Number": "000834516"}]
					},
				},
			)

		message = log_error.call_args.kwargs["message"]
		self.assertEqual(
			log_error.call_args.kwargs["title"],
			"SFS API CheckInvoicesStatus failed EBI000834516",
		)
		self.assertIn("Unknown fault occured", message)
		self.assertNotIn("huge body", message)
		self.assertNotIn("SOAP RESPONSE", message)

	@patch("erpnext_moldova_efactura.api_client.frappe.log_error")
	def test_http_500_omits_response_body_from_error_log(self, log_error):
		html = b"<html>boom</html>"
		method = Mock(
			side_effect=TransportError(
				f"Server returned response (500) with invalid XML: nope.\nContent: {html!r}",
				status_code=500,
				content=html,
			)
		)
		client = _client(method, http_status=500)
		client._history = SimpleNamespace(
			last_sent={"envelope": None},
			last_received={"envelope": object()},
		)
		client._dump_soap_envelope = Mock(return_value="<html>boom</html>")

		with self.assertRaises(EFacturaAPIError):
			client._call("PostInvoices", request={"RequestId": "EF-5"})

		log_error.assert_called_once()
		message = log_error.call_args.kwargs["message"]
		self.assertIn("HTTP 500", message)
		self.assertIn("EF-5", message)
		self.assertNotIn("<html>boom</html>", message)
		self.assertNotIn("SOAP RESPONSE", message)

	@patch("erpnext_moldova_efactura.api_client.frappe.log_error")
	def test_http_400_still_logs_soap_response(self, log_error):
		method = Mock(side_effect=TransportError("Bad request", status_code=400))
		client = _client(method, http_status=400)
		client._history = SimpleNamespace(
			last_sent={"envelope": None},
			last_received={"envelope": object()},
		)
		client._dump_soap_envelope = Mock(return_value="<Fault>bad</Fault>")

		with self.assertRaises(EFacturaAPIError):
			client._call("PostInvoices", request={"RequestId": "EF-6"})

		message = log_error.call_args.kwargs["message"]
		self.assertIn("SOAP RESPONSE", message)
		self.assertIn("<Fault>bad</Fault>", message)
