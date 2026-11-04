import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import urllib3.util.connection as urllib3_connection
from urllib3.connection import HTTPSConnection
from zeep.exceptions import Fault, TransportError

from erpnext_moldova_efactura.api_client import (
	EFacturaAPIClient,
	EFacturaAPIError,
	_build_http_session,
	_create_ipv4_connection,
	_IPv4HTTPAdapter,
	_IPv4HTTPSConnection,
)


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


class _FakeSocket:
	def __init__(self):
		self.connected = None

	def setsockopt(self, *_args):
		return None

	def settimeout(self, _value):
		return None

	def connect(self, address):
		self.connected = address

	def close(self):
		return None


class TestForceIPv4(unittest.TestCase):
	def test_dns_lookup_is_ipv4_and_process_default_stays(self):
		family_before = urllib3_connection.allowed_gai_family
		seen = {}

		def getaddrinfo(host, port, family, socktype):
			seen["host"] = host
			seen["family"] = family
			seen["socktype"] = socktype
			return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

		with patch("erpnext_moldova_efactura.api_client.socket.getaddrinfo", getaddrinfo), patch(
			"erpnext_moldova_efactura.api_client.socket.socket", return_value=_FakeSocket()
		):
			_create_ipv4_connection(("efactura-api.sfs.md", 443), timeout=5)

		self.assertEqual(seen["host"], "efactura-api.sfs.md")
		self.assertEqual(seen["family"], socket.AF_INET)
		self.assertIs(urllib3_connection.allowed_gai_family, family_before)

	def test_tls_handshake_method_is_unchanged(self):
		self.assertIs(_IPv4HTTPSConnection.connect, HTTPSConnection.connect)

	def test_socket_uses_resolved_address_and_keeps_hostname(self):
		sock = _FakeSocket()

		def getaddrinfo(host, port, family, _socktype):
			self.assertEqual(host, "efactura-api.sfs.md")
			self.assertEqual(family, socket.AF_INET)
			return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

		conn = _IPv4HTTPSConnection(host="efactura-api.sfs.md", port=443)
		with patch("erpnext_moldova_efactura.api_client.socket.getaddrinfo", getaddrinfo), patch(
			"erpnext_moldova_efactura.api_client.socket.socket", return_value=sock
		):
			conn._new_conn()

		self.assertEqual(conn.host, "efactura-api.sfs.md")
		self.assertEqual(sock.connected, ("203.0.113.10", 443))

	def test_session_forces_ipv4_only_for_api_host(self):
		wsdl = "https://efactura-api.sfs.md/Service.svc?wsdl"
		session = _build_http_session(wsdl, verify_tls=True, force_ipv4=True)
		self.assertEqual(session.headers["User-Agent"], "erpnext-moldova-efactura/1.0")
		self.assertIs(session.verify, True)
		api_adapter = session.get_adapter("https://efactura-api.sfs.md/Service.svc?wsdl")
		self.assertIsInstance(api_adapter, _IPv4HTTPAdapter)
		self.assertNotIsInstance(
			session.get_adapter("https://efactura-api.sfs.md.example/Service.svc"),
			_IPv4HTTPAdapter,
		)
		self.assertNotIsInstance(
			session.get_adapter("https://generativelanguage.googleapis.com/v1beta"),
			_IPv4HTTPAdapter,
		)

		plain = _build_http_session(wsdl, verify_tls=True, force_ipv4=False)
		self.assertNotIsInstance(plain.get_adapter(wsdl), _IPv4HTTPAdapter)
		self.assertEqual(plain.headers["User-Agent"], "erpnext-moldova-efactura/1.0")

	def test_soap_operation_timeout_matches_load_timeout(self):
		wsdl = "https://efactura-api.sfs.md/Service.svc?wsdl"
		fake_client = Mock()
		fake_client.wsdl.services = {"Service": SimpleNamespace(ports={"BasicHttpBinding": object()})}
		fake_client.bind.return_value = Mock()
		with patch("erpnext_moldova_efactura.api_client.Client", return_value=fake_client) as client_cls:
			EFacturaAPIClient(wsdl, "user", "pass", timeout=20)
		transport = client_cls.call_args.kwargs["transport"]
		self.assertEqual(transport.load_timeout, 20)
		self.assertEqual(transport.operation_timeout, 20)
