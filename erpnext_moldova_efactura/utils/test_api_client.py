import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from zeep.exceptions import Fault

from erpnext_moldova_efactura.api_client import EFacturaAPIClient, EFacturaAPIError


def _client(service_method):
	client = EFacturaAPIClient.__new__(EFacturaAPIClient)
	client.password = "secret-pass"
	client._history = SimpleNamespace(last_sent=None, last_received=None)
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
