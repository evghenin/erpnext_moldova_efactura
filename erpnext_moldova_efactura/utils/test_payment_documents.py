import unittest
import xml.etree.ElementTree as ET
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_moldova_efactura.utils.payment_documents import (
	append_attached_documents,
	attached_document_rows,
	mev_urls,
)


def _payment(name, mode, number, amount, remarks="", day=29):
	return SimpleNamespace(
		name=name,
		mode_of_payment=mode,
		reference_no=number,
		reference_date=date(2026, 9, day),
		remarks=remarks,
		allocated_amount=amount,
	)


class TestPaymentDocuments(unittest.TestCase):
	def test_mev_url_is_kept_whole(self):
		self.assertEqual(
			mev_urls("see https://mev.sfs.md/c/abc."),
			["https://mev.sfs.md/c/abc"],
		)

	@patch("erpnext_moldova_efactura.utils.payment_documents.comment_text", return_value="")
	@patch("erpnext_moldova_efactura.utils.payment_documents.payments_for_sales_invoice")
	@patch("erpnext_moldova_efactura.utils.payment_documents.document_names_by_mode")
	def test_cash_and_bank_rows_skip_unmapped_mode(self, names, payments, _comments):
		names.return_value = {
			"Cash": "Bon fiscal (numerar)",
			"Bank": "Ordin de plata",
		}
		payments.return_value = [
			_payment("PE-CASH", "Cash", "36", 150, "https://mev.sfs.md/c/abc"),
			_payment("PE-BANK", "Bank", "OP-9", 10, day=28),
			_payment("PE-OTHER", "Wire", "1", 5),
		]
		rows = attached_document_rows("SINV-1")
		self.assertEqual(
			[row["type"] for row in rows],
			["Bon fiscal (numerar)", "Ordin de plata"],
		)
		self.assertEqual(rows[0]["number"], "36 150.00 https://mev.sfs.md/c/abc")
		self.assertEqual(rows[0]["date"], "2026-09-29T00:00:00")
		self.assertEqual(rows[1]["number"], "OP-9 10.00")

	@patch("erpnext_moldova_efactura.utils.payment_documents.document_names_by_mode", return_value={})
	def test_checkbox_off_adds_nothing(self, _names):
		self.assertEqual(attached_document_rows("SINV-1"), [])

	@patch("erpnext_moldova_efactura.utils.payment_documents.frappe.db.get_single_value", return_value=1)
	def test_xml_uses_edited_child_rows(self, _setting):
		from erpnext_moldova_efactura.utils.payment_documents import xml_rows_from_doc

		row = SimpleNamespace(
			document_type="Bon fiscal (card)",
			document_number="7 20.00",
			document_date=date(2026, 9, 28),
			payment_entry="PE-CARD",
			file=None,
		)
		doc = SimpleNamespace(attached_documents=[row], get=lambda key, default=None: [row])
		rows = xml_rows_from_doc(doc)
		self.assertEqual(rows[0]["type"], "Bon fiscal (card)")
		self.assertEqual(rows[0]["number"], "7 20.00")
		self.assertEqual(rows[0]["payment_entry"], "PE-CARD")

	@patch("erpnext_moldova_efactura.utils.payment_documents.payment_files", return_value={})
	@patch("erpnext_moldova_efactura.utils.payment_documents.attached_document_rows")
	def test_existing_rows_are_not_replaced(self, source, _files):
		from erpnext_moldova_efactura.utils.payment_documents import sync_attached_documents

		doc = SimpleNamespace(
			docstatus=0,
			get=lambda key, default=None: [SimpleNamespace(document_type="Edited")]
			if key == "attached_documents"
			else default,
			append=lambda *_args, **_kwargs: self.fail("existing rows were replaced"),
		)
		sync_attached_documents(doc)
		source.assert_not_called()

	def test_xml_block_before_creation_motiv(self):
		supplier = ET.Element("SupplierInfo")
		append_attached_documents(
			supplier,
			[
				{"type": "Bon fiscal (numerar)", "number": "36 150.00", "date": "2026-09-29T00:00:00"},
				{"type": "Bon fiscal (card)", "number": "7 20.00", "date": "2026-09-28T00:00:00"},
			],
		)
		ET.SubElement(supplier, "CreationMotiv").text = "5"
		xml = ET.tostring(supplier, encoding="unicode")
		self.assertLess(xml.index("AttachedDocuments"), xml.index("CreationMotiv"))
		self.assertEqual(xml.count("<Document "), 2)
		self.assertIn('Type="Bon fiscal (numerar)"', xml)
		self.assertIn('Type="Bon fiscal (card)"', xml)
		self.assertNotIn("Seria", xml)
