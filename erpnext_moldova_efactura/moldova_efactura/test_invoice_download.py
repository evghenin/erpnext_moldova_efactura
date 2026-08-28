from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_moldova_efactura.utils.invoice_download import download_sfs_pdf


class TestInvoiceDownload(FrappeTestCase):
	def test_download_sfs_pdf_uses_requested_actor_role(self):
		doc = SimpleNamespace(
			company="Test Company",
			ef_series="EBL",
			ef_number="000000001",
		)
		client = Mock()
		client.get_invoices_content_for_print.return_value = {
			"Result": {"Content": b"%PDF-1.7 test"}
		}

		with patch(
			"erpnext_moldova_efactura.utils.invoice_download.EFacturaAPIClient.from_settings",
			return_value=client,
		):
			download_sfs_pdf(doc, actor_role=2)

		client.get_invoices_content_for_print.assert_called_once_with(
			seria_and_numbers={"Seria": "EBL", "Number": "000000001"},
			actor_role=2,
		)
		self.assertEqual(frappe.local.response.filename, "EBL000000001.pdf")
		self.assertEqual(frappe.local.response.filecontent, b"%PDF-1.7 test")
		self.assertEqual(frappe.local.response.content_type, "application/pdf")
