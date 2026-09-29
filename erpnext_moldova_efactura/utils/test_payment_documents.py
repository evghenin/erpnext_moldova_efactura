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
		paid_amount=amount + 5,
		allocated_amount=amount,
	)


class TestPaymentDocuments(unittest.TestCase):
	def test_print_stylesheet_is_inlined_from_disk(self):
		from erpnext_moldova_efactura.utils.payment_documents import _inline_print_styles

		html = _inline_print_styles(
			'<link rel="stylesheet" href="http://development.localhost:8000/assets/frappe/dist/css/print.bundle.css">'
			"<script>document.addEventListener('DOMContentLoaded', () => {})</script>"
			'<img src="https://mev.sfs.md/x">'
			'<img src="data:image/png;base64,aaa">'
		)
		self.assertNotIn("development.localhost", html)
		self.assertNotIn("mev.sfs.md", html)
		self.assertIn("<style>", html)
		self.assertIn("addEventListener", html)
		self.assertIn("data:image/png;base64,aaa", html)

	def test_payment_qr_returns_png_data_uri(self):
		from erpnext_moldova_efactura.utils.payment_documents import payment_qr

		self.assertTrue(payment_qr("https://mev.sfs.md/c/abc").startswith("data:image/png;base64,"))
		self.assertEqual(payment_qr(""), "")

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
		self.assertEqual(rows[0]["number"], "36")
		self.assertEqual(rows[0]["paid_amount"], 155)
		self.assertEqual(rows[0]["allocated_amount"], 150)
		self.assertEqual(rows[0]["url"], "https://mev.sfs.md/c/abc")
		self.assertEqual(rows[0]["date"], "2026-09-29T00:00:00")
		self.assertEqual(rows[1]["number"], "OP-9")
		self.assertEqual(rows[1]["paid_amount"], 15)
		self.assertEqual(rows[1]["allocated_amount"], 10)

	@patch("erpnext_moldova_efactura.utils.payment_documents.document_names_by_mode", return_value={})
	def test_checkbox_off_adds_nothing(self, _names):
		self.assertEqual(attached_document_rows("SINV-1"), [])

	@patch("erpnext_moldova_efactura.utils.payment_documents.frappe.db.get_single_value", return_value=1)
	def test_xml_uses_edited_child_rows(self, _setting):
		from erpnext_moldova_efactura.utils.payment_documents import xml_rows_from_doc

		row = SimpleNamespace(
					document_type="Bon fiscal (card)",
					document_number="7",
					paid_amount=25,
					allocated_amount=20,
					verification_url="https://mev.sfs.md/c/abc",
					document_date=date(2026, 9, 28),
			payment_entry="PE-CARD",
			file=None,
		)
		doc = SimpleNamespace(attached_documents=[row], get=lambda key, default=None: [row])
		rows = xml_rows_from_doc(doc)
		self.assertEqual(rows[0]["type"], "Bon fiscal (card)")
		self.assertEqual(rows[0]["number"], "7")
		self.assertEqual(rows[0]["date_label"], "28.09.2026")
		self.assertEqual(rows[0]["paid_amount_label"], "25.00")
		self.assertEqual(rows[0]["allocated_amount_label"], "20.00")
		self.assertEqual(rows[0]["url"], "https://mev.sfs.md/c/abc")
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

	def test_cover_html_has_bilingual_labels(self):
		from erpnext_moldova_efactura.utils.payment_documents import cover_html

		html = cover_html(
			[
				{
					"type": "Bon fiscal (numerar)",
					"number": "1",
					"date_label": "08.09.2026",
					"paid_amount_label": "1222.00",
					"allocated_amount_label": "1222.00",
					"url": "https://mev.sfs.md/c/abc",
				}
			]
		)
		self.assertIn("Tipul plății", html)
		self.assertIn("Тип оплаты", html)
		self.assertIn("Suma plătită", html)
		self.assertIn("Распределённая сумма", html)
		self.assertIn('href="https://mev.sfs.md/c/abc"', html)
		self.assertIn("class='ro'", html)
		self.assertIn("Bon fiscal (numerar)", html)
		self.assertNotIn("<h2>", html)

	def test_long_receipt_is_sliced_across_a4_columns(self):
		from io import BytesIO

		from PIL import Image
		from pypdf import PdfReader

		from erpnext_moldova_efactura.utils.payment_documents import _as_pdf

		# 80px wide and 30 times an A4 height at the 80mm scale: several columns.
		image = Image.new("RGB", (80, 80 * 30), "white")
		buffer = BytesIO()
		image.save(buffer, format="PNG")
		pdf = PdfReader(BytesIO(_as_pdf(buffer.getvalue(), "bon.png")))
		self.assertGreater(len(pdf.pages), 1)
		box = pdf.pages[0].mediabox
		self.assertAlmostEqual(float(box.width), 210 / 25.4 * 72, delta=2)
		self.assertAlmostEqual(float(box.height), 297 / 25.4 * 72, delta=2)

	def test_merge_pdfs_appends_source_pages(self):
		from io import BytesIO

		from pypdf import PdfReader, PdfWriter

		from erpnext_moldova_efactura.utils.payment_documents import merge_pdfs

		def one_page() -> bytes:
			writer = PdfWriter()
			writer.add_blank_page(width=200, height=200)
			out = BytesIO()
			writer.write(out)
			return out.getvalue()

		merged = merge_pdfs(one_page(), [one_page(), None])
		self.assertEqual(len(PdfReader(BytesIO(merged)).pages), 2)
