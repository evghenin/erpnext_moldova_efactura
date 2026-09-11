import hashlib
import io
import os
import re
import unittest
import urllib.error
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, flt, getdate, nowdate

from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura import (
	import_pdf,
	verify_pdf_signature,
)
from erpnext_moldova_efactura.utils.factura_ai import OCR_ERROR, parse_image
from erpnext_moldova_efactura.utils.factura_pdf import FacturaImportError, decimal, parse_pdf, parse_text
from erpnext_moldova_efactura.utils.pf_invoice import (
	assert_no_pf_for_pef,
	assert_no_pf_for_pi,
	link_purchase_invoice,
	make_purchase_invoice,
	unlink_purchase_invoice,
)

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
ARAX_TEXT = """Factură fiscală
Seria, Nr. AAY9977940
Data eliberării / data livrării 31.08.2026 / 31.08.2026
1. Furnizor ARAX-IMPEX 1002600041697/0501989
2. Cumpărător TEST 1024600026571/0211775
Act №365411 din 31.08.2026
"""
ARAX_LAYOUT = """10.1  10.2  10.3  10.4  10.5  10.6  10.7  10.8
PACHET SERVICII INTERNET
Access internet prin linie dedicata  GB  1.00  225.00  225.00  20.00  45.00  270.00
---  ---  ---  ---  ---  ---  ---  ---
12. TOTAL (pe factura fiscală) /  225.00  X  45.00  270.00
"""


class TestFacturaPDF(TestCase):
	def test_arax_layout_and_references(self):
		data = parse_text(ARAX_TEXT, ARAX_LAYOUT)
		self.assertEqual((data["series"], data["number"], data["total"]), ("AAY", "9977940", "270.00"))
		self.assertEqual(len(data["items"]), 1)
		self.assertEqual(data["items"][0]["source_uom"], "GB")
		self.assertEqual(data["related_document_number"], "365411")
		self.assertEqual(data["supplier_name"], "ARAX-IMPEX SRL")

	def test_arax_header_can_come_from_layout(self):
		data = parse_text("", "Factură fiscală\n" + ARAX_TEXT + "\n" + ARAX_LAYOUT)
		self.assertEqual((data["series"], data["number"], data["total"]), ("AAY", "9977940", "270.00"))

	def test_signed_arax_example_pdf(self):
		path = EXAMPLES / "AAY9977940.signed.pdf"
		if not path.exists():
			self.skipTest("Private example PDF is not available")
		data = parse_pdf(path.read_bytes())
		self.assertEqual(data["series"], "AAY")
		self.assertEqual(data["number"], "9977940")
		self.assertEqual((data["provider"], data["total"]), ("ARAX", "270.00"))

	def test_bad_totals_or_missing_rows_fail(self):
		for layout in (
			ARAX_LAYOUT.replace("270.00", "280.00"),
			ARAX_LAYOUT.replace(
				"Access internet prin linie dedicata  GB  1.00  225.00  225.00  20.00  45.00  270.00", ""
			),
		):
			with self.assertRaises(FacturaImportError):
				parse_text(ARAX_TEXT, layout)

	def test_unknown_supplier_or_missing_identity_fails(self):
		with self.assertRaises(FacturaImportError):
			parse_text(ARAX_TEXT.replace("1002600041697", "1000000000000"), ARAX_LAYOUT)

	def test_invalid_pdf_fails(self):
		with self.assertRaises(FacturaImportError):
			parse_pdf(b"not a pdf")

	def test_invalid_scan_returns_quality_error(self):
		with self.assertRaisesRegex(
			FacturaImportError,
			re.escape(f"{OCR_ERROR}: the source is not a supported JPEG, PNG or PDF file"),
		):
			parse_image(b"not an image")

	def test_gemini_extraction_reconciles_retail_rows(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1838180",
			"issue_date": "2026-08-29",
			"supplier_name": "I.C.S METRO CASH & CARRY MOLDOVA S.R.L.",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"buyer_vat_id": "0211775",
			"related_document_type": "Bon fiscal",
			"related_document_number": "36",
			"net_total": "104.65",
			"vat_total": "20.93",
			"total": "125.58",
			"items": [
				{
					"description": "BIC RADIERA GALET",
					"supplier_item_code": "4000005319066",
					"source_uom": "IM",
					"source_qty": "3",
					"source_rate": "20.75",
					"net_amount": "62.25",
					"vat_rate": "20",
					"vat_amount": "12.45",
					"amount": "74.70",
				},
				{
					"description": "BIBLIORAFT CLASSIC 50MM VERDE",
					"supplier_item_code": "6931597514035",
					"source_uom": "ST",
					"source_qty": "1",
					"source_rate": "29.92",
					"net_amount": "29.92",
					"vat_rate": "20",
					"vat_amount": "5.98",
					"amount": "35.90",
				},
				{
					"description": "CAIET 24 FILE LINIE COLOR",
					"supplier_item_code": "4840842033998",
					"source_uom": "BU",
					"source_qty": "4",
					"source_rate": "3.12",
					"net_amount": "12.48",
					"vat_rate": "20",
					"vat_amount": "2.50",
					"amount": "14.98",
				},
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual((data["series"], data["number"], data["total"]), ("AAQ", "1838180", "125.58"))
		self.assertEqual(data["provider"], "Gemini")
		self.assertEqual(data["related_document_number"], "36")
		self.assertEqual(len(data["items"]), 3)
		self.assertEqual(data["items"][0]["supplier_item_code"], "4000005319066")

	def test_gemini_extraction_accepts_printed_unit_rate_rounding(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAU",
			"number": "02225776",
			"issue_date": "2024-09-12",
			"supplier_name": "S.A. VICON",
			"supplier_idno": "1003600050470",
			"buyer_name": "HOTEL LIFE S.R.L.",
			"buyer_idno": "1024600026571",
			"net_total": "620.83",
			"vat_total": "124.17",
			"total": "745.00",
			"items": [
				{
					"description": "Подставка информационная пластиковая A4 AXENT",
					"supplier_item_code": "30201000",
					"source_uom": "buc.",
					"source_qty": "5.00",
					"source_rate": "124.17",
					"net_amount": "620.83",
					"vat_rate": "20",
					"vat_amount": "124.17",
					"amount": "745.00",
				}
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual(data["items"][0]["net_amount"], "620.83")
		self.assertEqual(data["items"][0]["source_qty"], "5.00")

	def test_gemini_extraction_splits_glued_idno_and_vat(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1830389",
			"issue_date": "2025-03-24",
			"supplier_name": "I.C.S METRO CASH & CARRY MOLDOVA S.R.L.",
			"supplier_idno": "10046010027387800030",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "10246000265710211775",
			"net_total": "658.33",
			"vat_total": "131.67",
			"total": "790.00",
			"items": [
				{
					"description": "CUTIA SET 5 UNEASE VELVET NEG",
					"supplier_item_code": "5904134079640",
					"source_uom": "BU",
					"source_qty": "10",
					"source_rate": "65.83",
					"net_amount": "658.33",
					"vat_rate": "20",
					"vat_amount": "131.67",
					"amount": "790.00",
				}
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual(data["supplier_idno"], "1004601002738")
		self.assertEqual(data["supplier_vat_id"], "7800030")
		self.assertEqual(data["buyer_idno"], "1024600026571")
		self.assertEqual(data["buyer_vat_id"], "0211775")

	def test_gemini_extraction_rejects_copied_supplier_idno_as_buyer(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1830389",
			"issue_date": "2025-03-24",
			"supplier_name": "I.C.S METRO CASH & CARRY MOLDOVA S.R.L.",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1004601002738",
			"net_total": "658.33",
			"vat_total": "131.67",
			"total": "790.00",
			"items": [
				{
					"description": "CUTIA SET 5 UNEASE VELVET NEG",
					"supplier_item_code": "5904134079640",
					"source_uom": "BU",
					"source_qty": "10",
					"source_rate": "65.83",
					"net_amount": "658.33",
					"vat_rate": "20",
					"vat_amount": "131.67",
					"amount": "790.00",
				}
			],
		}
		with self.assertRaisesRegex(FacturaImportError, "customer identity"):
			document_from_extraction(payload, b"\xff\xd8\xff")

	def test_gemini_accepts_metro_line_reducere_and_footer_discounts(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1774375",
			"issue_date": "2025-12-09",
			"supplier_name": "I.C.S METRO CASH & CARRY MOLDOVA S.R.L.",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "560.28",
			"vat_total": "112.06",
			"total": "672.34",
			"items": [
				{
					"description": "1KG CAF BOABE RIOBA PERFETTO",
					"supplier_item_code": "4337182253991",
					"source_uom": "BU",
					"source_qty": "2",
					"source_rate": "307.50",
					"net_amount": "615.00",
					"vat_rate": "20",
					"vat_amount": "123.00",
					"amount": "678.00",
				},
				{
					"description": "250075348",
					"supplier_item_code": "250075348",
					"source_qty": "",
					"source_rate": "4.72",
					"net_amount": "-4.72",
					"vat_rate": "20",
					"vat_amount": "-0.94",
					"amount": "-5.66",
				},
				{
					"description": "250075862 BC",
					"supplier_item_code": "250075862",
					"source_qty": "1",
					"source_rate": "",
					"net_amount": "-50.00",
					"vat_rate": "",
					"vat_amount": "",
					"amount": "-60.00",
				},
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual(len(data["items"]), 1)
		self.assertEqual(data["items"][0]["description"], "1KG CAF BOABE RIOBA PERFETTO")
		self.assertEqual(data["items"][0]["source_qty"], "2")
		self.assertEqual(data["items"][0]["source_rate"], "307.50")
		self.assertEqual(data["items"][0]["net_amount"], "560.28")
		self.assertEqual(data["items"][0]["amount"], "672.34")
		self.assertEqual(data["total"], "672.34")

	def test_gemini_accepts_accounting_minus_and_drops_vat_bucket_rows(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1774375",
			"issue_date": "2025-12-09",
			"supplier_name": "I.C.S METRO CASH & CARRY MOLDOVA S.R.L.",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "560.28",
			"vat_total": "112.06",
			"total": "672.34",
			"items": [
				{
					"description": "1KG CAF BOABE RIOBA PERFETTO",
					"source_qty": "2",
					"source_rate": "307.50",
					"net_amount": "615.00",
					"vat_rate": "20",
					"vat_amount": "123.00",
					"amount": "738.00",
				},
				{
					"description": "250075348",
					"source_qty": "",
					"source_rate": "4,72",
					"net_amount": "4,72-",
					"vat_rate": "20",
					"vat_amount": "0,94-",
					"amount": "5,66-",
				},
				{
					"description": "250075862 BC",
					"source_qty": "1",
					"source_rate": "50,00",
					"net_amount": "50,00-",
					"vat_rate": "",
					"vat_amount": "",
					"amount": "60,00-",
				},
				{
					"description": "Total fara TVA 8%",
					"source_qty": "1",
					"source_rate": "233.67",
					"net_amount": "233.67",
					"vat_rate": "8",
					"vat_amount": "18.69",
					"amount": "252.36",
				},
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual(len(data["items"]), 1)
		self.assertEqual(data["items"][0]["description"], "1KG CAF BOABE RIOBA PERFETTO")
		self.assertEqual(data["items"][0]["net_amount"], "560.28")
		self.assertEqual(data["items"][0]["amount"], "672.34")
		self.assertEqual(data["total"], "672.34")

	def test_gemini_extraction_rejects_unreconciled_rows(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1838180",
			"issue_date": "2026-08-29",
			"supplier_name": "METRO",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "1251.02",
			"vat_total": "250.20",
			"total": "1501.22",
			"items": [
				{
					"description": "BIC RADIERA GALET",
					"source_qty": "3",
					"source_rate": "20.75",
					"net_amount": "62.25",
					"vat_rate": "20",
					"vat_amount": "12.45",
					"amount": "74.70",
				}
			],
		}
		with self.assertRaises(FacturaImportError) as ctx:
			document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertIn("item totals do not match", str(ctx.exception))

	def test_gemini_drops_duplicated_line_when_printed_totals_match(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		pivot = {
			"description": "Pivot",
			"source_uom": "buc",
			"source_qty": "1",
			"source_rate": "583.33",
			"net_amount": "583.33",
			"vat_rate": "20",
			"vat_amount": "116.67",
			"amount": "700.00",
		}
		payload = {
			"series": "AAY",
			"number": "7128754",
			"issue_date": "2026-08-21",
			"supplier_name": "SRL Nifestcom",
			"supplier_idno": "1002600041697",
			"buyer_name": "Hotel Life SRL",
			"buyer_idno": "1024600026571",
			"net_total": "11049.99",
			"vat_total": "2210.01",
			"total": "13260.00",
			"items": [
				{
					"description": "Schimb ulei in motor",
					"source_uom": "buc",
					"source_qty": "1",
					"source_rate": "10466.67",
					"net_amount": "10466.67",
					"vat_rate": "20",
					"vat_amount": "2093.33",
					"amount": "12560.00",
				},
				pivot,
				pivot,
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual((data["total"], len(data["items"])), ("13260.00", 2))
		self.assertEqual(data["items"][1]["description"], "Pivot")

	def test_gemini_drops_overage_row_matching_printed_surplus(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		bieleta = {
			"description": "Bieleta antiruliu",
			"source_uom": "buc",
			"source_qty": "1",
			"source_rate": "333.33",
			"net_amount": "333.33",
			"vat_rate": "20",
			"vat_amount": "66.67",
			"amount": "400.00",
		}
		extra = dict(bieleta, net_amount="333.34", vat_amount="66.66")
		payload = {
			"series": "AAY",
			"number": "7128754",
			"issue_date": "2026-08-21",
			"supplier_name": "SRL Nifestcom",
			"supplier_idno": "1002600041697",
			"buyer_name": "Hotel Life SRL",
			"buyer_idno": "1024600026571",
			"net_total": "11049.99",
			"vat_total": "2210.01",
			"total": "13260.00",
			"items": [
				{
					"description": "Schimb ulei in motor",
					"source_uom": "buc",
					"source_qty": "1",
					"source_rate": "10466.67",
					"net_amount": "10466.67",
					"vat_rate": "20",
					"vat_amount": "2093.33",
					"amount": "12560.00",
				},
				{
					"description": "Pivot",
					"source_uom": "buc",
					"source_qty": "1",
					"source_rate": "249.99",
					"net_amount": "249.99",
					"vat_rate": "20",
					"vat_amount": "50.01",
					"amount": "300.00",
				},
				bieleta,
				extra,
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual((data["total"], len(data["items"])), ("13260.00", 3))
		self.assertEqual(sum(1 for row in data["items"] if "Bieleta" in row["description"]), 1)

	def test_gemini_applies_metro_reducere_to_charged_row(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1785209",
			"issue_date": "2024-10-19",
			"supplier_name": "ICS METRO CASH & CARRY MOLDOVA SRL",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "1162.01",
			"vat_total": "232.40",
			"total": "1394.41",
			"items": [
				{
					"description": "COS IMPLETIT MATERIAL",
					"source_qty": "1",
					"source_rate": "1186.67",
					"net_amount": "1186.67",
					"vat_rate": "20",
					"vat_amount": "237.33",
					"amount": "1424.00",
				}
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual((data["net_total"], data["total"], len(data["items"])), ("1162.01", "1394.41", 1))
		self.assertEqual(data["items"][0]["description"], "COS IMPLETIT MATERIAL")
		self.assertEqual(data["items"][0]["net_amount"], "1162.01")
		self.assertEqual(data["items"][0]["source_rate"], "1186.67")

	def test_gemini_applies_metro_reducere_column_on_mixed_vat_rows(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1774375",
			"issue_date": "2025-12-09",
			"supplier_name": "ICS METRO CASH & CARRY MOLDOVA SRL",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "214.12",
			"vat_total": "28.51",
			"total": "242.63",
			"items": [
				{
					"description": "400G BISCUITI DE OVAZ FRANZEL",
					"source_qty": "4",
					"source_rate": "24.92",
					"net_amount": "99.68",
					"vat_rate": "20",
					"vat_amount": "19.92",
					"amount": "113.94",
				},
				{
					"description": "10X28G SNACK AL LATTE BALCONI",
					"source_qty": "3",
					"source_rate": "42.50",
					"net_amount": "127.50",
					"vat_rate": "8",
					"vat_amount": "10.20",
					"amount": "128.69",
				},
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual(len(data["items"]), 2)
		self.assertEqual(data["items"][0]["net_amount"], "94.96")
		self.assertEqual(data["items"][0]["amount"], "113.94")
		self.assertEqual(data["items"][1]["amount"], "128.69")
		self.assertEqual(data["items"][0]["source_qty"], "4")
		self.assertFalse(any("discount" in row["description"].casefold() for row in data["items"]))

	def test_gemini_drops_reducere_cantitativa_row(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1785209",
			"issue_date": "2024-10-19",
			"supplier_name": "ICS METRO CASH & CARRY MOLDOVA SRL",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "1162.01",
			"vat_total": "232.40",
			"total": "1394.41",
			"items": [
				{
					"description": "COS IMPLETIT MATERIAL",
					"source_qty": "1",
					"source_rate": "1186.67",
					"net_amount": "1186.67",
					"vat_rate": "20",
					"vat_amount": "237.33",
					"amount": "1424.00",
				},
				{
					"description": "REDUCERE CANTITATIVA",
					"supplier_item_code": "2400236093",
					"source_qty": "1",
					"source_rate": "24.66",
					"net_amount": "-24.66",
					"vat_rate": "20",
					"vat_amount": "-4.93",
					"amount": "-29.59",
				},
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual((data["total"], len(data["items"])), ("1394.41", 1))
		self.assertEqual(data["items"][0]["description"], "COS IMPLETIT MATERIAL")
		self.assertEqual(data["items"][0]["net_amount"], "1162.01")

	def test_gemini_accepts_printed_vat_rounding_on_till_rows(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1838180",
			"issue_date": "2026-08-29",
			"supplier_name": "METRO CASH & CARRY MOLDOVA SRL",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "97.80",
			"vat_total": "19.50",
			"total": "117.30",
			"items": [
				{
					"description": "SET 3 PIXURI",
					"supplier_item_code": "6938944300693",
					"source_uom": "ST",
					"source_qty": "5",
					"source_rate": "17.42",
					"net_amount": "87.10",
					"vat_rate": "20",
					"vat_amount": "17.40",
					"amount": "104.50",
				},
				{
					"description": "CAIET 12 FILE LINIE",
					"supplier_item_code": "4840842000877",
					"source_uom": "BU",
					"source_qty": "10",
					"source_rate": "1.07",
					"net_amount": "10.70",
					"vat_rate": "20",
					"vat_amount": "2.10",
					"amount": "12.80",
				},
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual((data["total"], len(data["items"])), ("117.30", 2))
		self.assertEqual(data["items"][0]["vat_amount"], "17.40")

	def test_gemini_repairs_metro_unit_qty_from_printed_net(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1838180",
			"issue_date": "2026-08-29",
			"supplier_name": "METRO CASH & CARRY MOLDOVA SRL",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "62.25",
			"vat_total": "12.45",
			"total": "74.70",
			"items": [
				{
					"description": "BIC RADIERA GALET",
					"supplier_item_code": "4000005319066",
					"source_uom": "IM",
					"source_qty": "1",
					"source_rate": "20.75",
					"net_amount": "62.25",
					"vat_rate": "20",
					"vat_amount": "12.45",
					"amount": "74.70",
				}
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual(data["items"][0]["source_qty"], "3")
		self.assertEqual(data["total"], "74.70")

	def test_gemini_drops_plpa_note_rows(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1838180",
			"issue_date": "2026-08-29",
			"supplier_name": "METRO CASH & CARRY MOLDOVA SRL",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "62.25",
			"vat_total": "12.45",
			"total": "74.70",
			"items": [
				{
					"description": "BIC RADIERA GALET",
					"source_qty": "3",
					"source_rate": "20.75",
					"net_amount": "62.25",
					"vat_rate": "20",
					"vat_amount": "12.45",
					"amount": "74.70",
				},
				{
					"description": "PL/PA",
					"source_qty": "2.34",
					"source_rate": "35.04",
					"net_amount": "12.64",
					"vat_rate": "20",
					"vat_amount": "2.52",
					"amount": "15.16",
				},
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual(len(data["items"]), 1)
		self.assertEqual(data["items"][0]["description"], "BIC RADIERA GALET")

	def test_gemini_accepts_footer_net_vat_rounding_when_gross_matches(self):
		from erpnext_moldova_efactura.utils.factura_ai import document_from_extraction

		payload = {
			"series": "AAQ",
			"number": "1838180",
			"issue_date": "2026-08-29",
			"supplier_name": "METRO CASH & CARRY MOLDOVA SRL",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "1251.02",
			"vat_total": "250.11",
			"total": "1501.13",
			"items": [
				{
					"description": "SET 3 PIXURI",
					"source_uom": "ST",
					"source_qty": "1",
					"source_rate": "1251.00",
					"net_amount": "1251.00",
					"vat_rate": "20",
					"vat_amount": "250.22",
					"amount": "1501.22",
				}
			],
		}
		data = document_from_extraction(payload, b"\xff\xd8\xff")
		self.assertEqual((data["net_total"], data["vat_total"], data["total"]), ("1251.02", "250.20", "1501.22"))

	@patch("erpnext_moldova_efactura.utils.factura_ai._credentials", return_value=("key", "gemini-3.6-flash"))
	@patch("erpnext_moldova_efactura.utils.factura_ai._generate")
	def test_parse_image_uses_gemini(self, generate, _credentials):
		generate.return_value = {
			"series": "AAZ",
			"number": "1606962",
			"issue_date": "29.08.2026",
			"supplier_name": "MAGAS TRANS S.R.L.",
			"supplier_idno": "1004600061235",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "4800.00",
			"vat_total": "0.00",
			"total": "4800.00",
			"items": [
				{
					"description": "Servicii de transport",
					"source_uom": "serv",
					"source_qty": "1",
					"source_rate": "4800.00",
					"net_amount": "4800.00",
					"vat_rate": "0",
					"vat_amount": "0.00",
					"amount": "4800.00",
				}
			],
		}
		data = parse_image(b"\xff\xd8\xffdummy")
		self.assertEqual(data["provider"], "Gemini")
		self.assertEqual((data["series"], data["number"], data["total"]), ("AAZ", "1606962", "4800.00"))
		generate.assert_called_once()
		self.assertEqual(generate.call_args[0][1], "image/jpeg")

	@patch("erpnext_moldova_efactura.utils.factura_ai._credentials", return_value=("key", "gemini-3.6-flash"))
	@patch("erpnext_moldova_efactura.utils.factura_ai._generate")
	def test_parse_image_accepts_pdf(self, generate, _credentials):
		generate.return_value = {
			"series": "AAZ",
			"number": "1606962",
			"issue_date": "29.08.2026",
			"supplier_name": "MAGAS TRANS S.R.L.",
			"supplier_idno": "1004600061235",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "4800.00",
			"vat_total": "0.00",
			"total": "4800.00",
			"items": [
				{
					"description": "Servicii de transport",
					"source_uom": "serv",
					"source_qty": "1",
					"source_rate": "4800.00",
					"net_amount": "4800.00",
					"vat_rate": "0",
					"vat_amount": "0.00",
					"amount": "4800.00",
				}
			],
		}
		data = parse_image(b"%PDF-1.4 dummy")
		self.assertEqual(data["provider"], "Gemini")
		generate.assert_called_once()
		self.assertEqual(generate.call_args[0][1], "application/pdf")

	@patch("erpnext_moldova_efactura.utils.factura_ai._credentials", return_value=("key", "gemini-3.6-flash"))
	@patch("erpnext_moldova_efactura.utils.factura_ai._generate")
	def test_parse_image_retries_when_item_totals_mismatch(self, generate, _credentials):
		ok = {
			"series": "AAQ",
			"number": "1838180",
			"issue_date": "2026-08-29",
			"supplier_name": "METRO CASH & CARRY MOLDOVA SRL",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "104.65",
			"vat_total": "20.93",
			"total": "125.58",
			"items": [
				{
					"description": "BIC RADIERA GALET",
					"source_qty": "3",
					"source_rate": "20.75",
					"net_amount": "62.25",
					"vat_rate": "20",
					"vat_amount": "12.45",
					"amount": "74.70",
				},
				{
					"description": "ARO HARTIE COPIATOR 80G A4",
					"source_qty": "1",
					"source_rate": "42.40",
					"net_amount": "42.40",
					"vat_rate": "20",
					"vat_amount": "8.48",
					"amount": "50.88",
				},
			],
		}
		missing = dict(ok, items=[ok["items"][0]], net_total="104.65", vat_total="20.93", total="125.58")
		generate.side_effect = [missing, ok]
		data = parse_image(b"\xff\xd8\xffdummy")
		self.assertEqual(generate.call_count, 2)
		self.assertEqual((data["total"], len(data["items"])), ("125.58", 2))
		self.assertIn("item totals do not match", generate.call_args.kwargs["prompt"])

	@patch("erpnext_moldova_efactura.utils.factura_ai._credentials", return_value=("key", "gemini-3.6-flash"))
	@patch("erpnext_moldova_efactura.utils.factura_ai._generate")
	def test_parse_image_drops_note_rows_without_retry(self, generate, _credentials):
		ok = {
			"series": "AAQ",
			"number": "1838180",
			"issue_date": "2026-08-29",
			"supplier_name": "METRO CASH & CARRY MOLDOVA SRL",
			"supplier_idno": "1004601002738",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"net_total": "62.25",
			"vat_total": "12.45",
			"total": "74.70",
			"items": [
				{
					"description": "BIC RADIERA GALET",
					"source_qty": "3",
					"source_rate": "20.75",
					"net_amount": "62.25",
					"vat_rate": "20",
					"vat_amount": "12.45",
					"amount": "74.70",
				}
			],
		}
		broken = dict(
			ok,
			items=ok["items"]
			+ [
				{
					"description": "PL/PA",
					"source_qty": "2.34",
					"source_rate": "35.04",
					"net_amount": "12.64",
					"vat_rate": "20",
					"vat_amount": "2.52",
					"amount": "15.16",
				}
			],
		)
		generate.side_effect = [broken, ok]
		data = parse_image(b"\xff\xd8\xffdummy")
		self.assertEqual(generate.call_count, 1)
		self.assertEqual((data["total"], len(data["items"])), ("74.70", 1))

	@patch("erpnext_moldova_efactura.utils.factura_ai.time.sleep")
	def test_gemini_timeout_fails_without_retry(self, sleep):
		from erpnext_moldova_efactura.utils.factura_ai import _post_gemini

		def urlopen(request, timeout=180):
			raise TimeoutError("timed out")

		with patch("erpnext_moldova_efactura.utils.factura_ai.urllib.request.urlopen", side_effect=urlopen):
			with self.assertRaises(FacturaImportError) as ctx:
				_post_gemini(b"{}", "key", "gemini-3.6-flash")
		self.assertIn("timed out", str(ctx.exception))
		sleep.assert_not_called()

	@patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
	@patch("frappe.db.get_single_value", return_value=None)
	def test_paper_ai_disabled_without_gemini_key(self, _single):
		from erpnext_moldova_efactura.boot import extend_bootinfo
		from erpnext_moldova_efactura.utils.factura_ai import paper_ai_enabled

		self.assertFalse(paper_ai_enabled())
		bootinfo = {}
		extend_bootinfo(bootinfo)
		self.assertEqual(bootinfo["moldova_efactura_paper_ai"], 0)

	@patch("frappe.db.get_single_value", return_value="encrypted")
	def test_paper_ai_enabled_when_settings_key_present(self, _single):
		from erpnext_moldova_efactura.utils.factura_ai import paper_ai_enabled

		self.assertTrue(paper_ai_enabled())

	def test_retired_gemini_model_is_remapped(self):
		from erpnext_moldova_efactura.utils.factura_ai import RETIRED_MODELS, _suggested_model

		self.assertEqual(RETIRED_MODELS["gemini-2.5-flash"], "gemini-3.6-flash")
		self.assertEqual(
			_suggested_model(
				"This model models/gemini-2.5-flash is no longer available. "
				"Please update your code to use models/gemini-3.6-flash"
			),
			"gemini-3.6-flash",
		)

	@patch("erpnext_moldova_efactura.utils.factura_ai.time.sleep")
	def test_gemini_404_retries_replacement_model(self, _sleep):
		from io import BytesIO
		from unittest.mock import MagicMock

		from erpnext_moldova_efactura.utils.factura_ai import _post_gemini

		not_found = urllib.error.HTTPError(
			"https://example/models/gemini-2.5-flash",
			404,
			"Not Found",
			hdrs={},
			fp=BytesIO(
				b'{"error":{"message":"This model models/gemini-2.5-flash is no longer available '
				b'to new users. Please use models/gemini-3.6-flash"}}'
			),
		)
		ok = MagicMock()
		ok.read.return_value = b'{"candidates":[{"content":{"parts":[{"text":"{}"}]}}]}'
		ok.__enter__.return_value = ok
		ok.__exit__.return_value = False

		def urlopen(request, timeout=120):
			if "gemini-2.5-flash" in request.full_url:
				raise not_found
			return ok

		with patch("erpnext_moldova_efactura.utils.factura_ai.urllib.request.urlopen", side_effect=urlopen):
			payload = _post_gemini(b"{}", "key", "gemini-2.5-flash")
		self.assertIn("candidates", payload)

	def test_imported_idno_formatting_does_not_count_as_original_change(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura import _changed

		current = frappe._dict(f_supplier_idno="100 260 004 1697")
		previous = frappe._dict(f_supplier_idno="1002600041697")
		current.meta = previous.meta = frappe.get_meta("Purchase Factura")
		self.assertFalse(_changed(current, previous, "f_supplier_idno"))

	@unittest.skipUnless(os.environ.get("GEMINI_API_KEY"), "GEMINI_API_KEY is not set")
	def test_actual_photographed_facturas(self):
		for filename, series, number, total, item_count in (
			("IMG_20260906_115807.jpg", "AAZ", "1606962", "4800.00", 1),
			("IMG_20260906_120600.jpg", "AAY", "7128757", "579.00", 6),
			("IMG_20260906_120644.jpg", "AAY", "7128754", "13260.00", 22),
			("IMG_20260907_210310.jpg", "AAQ", "1838180", "1501.22", 36),
		):
			with self.subTest(filename=filename):
				data = parse_image((EXAMPLES / filename).read_bytes())
				self.assertEqual((data["series"], data["number"], data["total"]), (series, number, total))
				self.assertEqual(len(data["items"]), item_count)
				self.assertEqual(data["buyer_idno"], "1024600026571")
				self.assertEqual(data["original_format"], "Paper")

	def test_actual_provider_pdfs(self):
		for filename, series, number, total, uom in (
			("AAY9977940.signed.pdf", "AAY", "9977940", "270.00", "GB"),
			("74085315_1_FiscalInvoice.pdf", "AAX", "8280597", "220.00", ""),
		):
			path = EXAMPLES / filename
			with self.subTest(filename=filename):
				data = parse_pdf(path.read_bytes())
				self.assertEqual((data["series"], data["number"], data["total"]), (series, number, total))
				self.assertEqual(data["items"][0]["source_uom"], uom)
				self.assertEqual(data["signature_status"], "Not Checked")
				self.assertIn(data["original_format"], ("Digitally Signed PDF", "Other Electronic"))
				self.assertTrue(data["signature_format"])
				self.assertTrue(data["signature_field"])
				self.assertTrue(data["signature_coverage"])
				self.assertTrue(data["supplier_address"])
				self.assertTrue(data["buyer_address"])
				self.assertTrue(data["supplier_bank_account"])
				self.assertTrue(data["buyer_bank_account"])
				if filename.startswith("AAY"):
					self.assertEqual(data["supplier_address"], "str.M. Dosoftei 118, mun.Chisi")
					self.assertEqual(data["supplier_bank_account"], "MD77FT222420100000415498")
					self.assertEqual(data["supplier_bank_code"], "FTMDMD2X735")
					self.assertEqual(data["buyer_name"], '"HOTEL LIFE" SRL')
					self.assertEqual(data["buyer_bank_account"], "MD46AG000000022516020091")


class TestPurchaseFactura(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		frappe.db.set_value("Currency", "MDL", "enabled", 1)
		frappe.db.set_single_value("eFactura Settings", "currency", "MDL")
		frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)
		for doctype, key, name in (
			("UOM", "uom_name", "Nos"),
			("Supplier Group", "supplier_group_name", "All Supplier Groups"),
			("Item Group", "item_group_name", "All Item Groups"),
			("Warehouse Type", "name", "Transit"),
		):
			if not frappe.db.exists(doctype, name):
				frappe.get_doc({"doctype": doctype, key: name, "is_group": 1}).insert()
		cls.company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "_Test PF Company",
				"abbr": "PFT",
				"default_currency": "MDL",
				"country": "Moldova, Republic of",
				"chart_of_accounts": "Standard",
				"tax_id": "1024600026571",
				"enable_perpetual_inventory": 0,
			}
		).insert()
		frappe.db.set_single_value("eFactura Settings", "company_idno_field", "tax_id")
		frappe.db.set_single_value("eFactura Settings", "supplier_idno_field", "tax_id")
		cls.supplier = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "_Test PF Supplier",
				"supplier_group": "All Supplier Groups",
				"supplier_type": "Company",
				"tax_id": "1002600041697",
			}
		).insert()
		cls.item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "_Test PF Service",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		parent = frappe.db.get_value(
			"Account", {"company": cls.company.name, "is_group": 1, "root_type": "Liability"}, "name"
		)
		cls.vat = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "_Test PF VAT",
				"company": cls.company.name,
				"parent_account": parent,
				"account_type": "Tax",
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "eFactura Company Setting",
				"parent": "eFactura Settings",
				"parenttype": "eFactura Settings",
				"parentfield": "company_settings",
				"company": cls.company.name,
				"buying_vat_account": cls.vat.name,
			}
		).db_insert()

	def setUp(self):
		frappe.db.savepoint("pf_test")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="pf_test")

	def factura(self, **values):
		doc = frappe.get_doc(
			{
				"doctype": "Purchase Factura",
				"company": self.company.name,
				"supplier_party_type": "Supplier",
				"supplier_party": self.supplier.name,
				"f_series": "PFT",
				"f_number": frappe.generate_hash(length=9),
				"issue_date": nowdate(),
				"currency": "MDL",
				"f_supplier_idno": "1002600041697",
				"f_customer_idno": "1024600026571",
				"items": [
					{
						"supplier_item_name": "Internet service",
						"f_qty": 1,
						"f_rate": 225,
						"f_vat_rate": 20,
						"item_code": self.item.name,
						"uom": "Nos",
						"qty": 1,
					}
				],
				**values,
			}
		)
		return doc.insert()

	def test_paper_row_accepts_printed_unit_rate_rounding(self):
		pf = self.factura(
			original_format="Paper",
			items=[
				{
					"supplier_item_name": "Подставка информационная пластиковая A4 AXENT",
					"f_qty": 5,
					"f_rate": 124.17,
					"f_net_amount": 620.83,
					"f_vat_rate": 20,
					"f_vat_amount": 124.17,
					"item_code": self.item.name,
					"uom": "Nos",
					"qty": 5,
				}
			],
		)
		self.assertEqual(flt(pf.items[0].f_net_amount), 620.83)
		self.assertEqual(flt(pf.items[0].f_vat_amount), 124.17)
		self.assertEqual(flt(pf.items[0].f_amount), 745)

	def test_paper_row_accepts_metro_line_reducere(self):
		pf = self.factura(
			original_format="Paper",
			items=[
				{
					"supplier_item_name": "10X28G SNACK AL LATTE BALCONI",
					"f_qty": 3,
					"f_rate": 42.50,
					"f_net_amount": 119.16,
					"f_vat_rate": 8,
					"f_vat_amount": 9.53,
					"item_code": self.item.name,
					"uom": "Nos",
					"qty": 3,
				}
			],
		)
		self.assertEqual(flt(pf.items[0].f_qty), 3)
		self.assertEqual(flt(pf.items[0].f_rate), 42.5)
		self.assertEqual(flt(pf.items[0].f_net_amount), 119.16)
		self.assertEqual(flt(pf.items[0].f_amount), 128.69)

	def test_paper_row_accepts_mixed_vat_after_reducere(self):
		pf = self.factura(
			original_format="Paper",
			items=[
				{
					"supplier_item_name": "20% goods",
					"f_qty": 1,
					"f_rate": 2415.81,
					"f_net_amount": 2415.81,
					"f_vat_rate": 20,
					"f_vat_amount": 483.16,
					"item_code": self.item.name,
					"uom": "Nos",
					"qty": 1,
				},
				{
					"supplier_item_name": "8% goods",
					"f_qty": 1,
					"f_rate": 233.73,
					"f_net_amount": 233.73,
					"f_vat_rate": 8,
					"f_vat_amount": 18.70,
					"item_code": self.item.name,
					"uom": "Nos",
					"qty": 1,
				},
			],
		)
		self.assertEqual(flt(pf.f_net_total), 2649.54)
		self.assertEqual(flt(pf.f_total), 3151.4)

	def test_accounting_and_fiscal_lifecycle(self):
		pf = self.factura()
		self.assertEqual(flt(pf.total), 270)
		self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": pf.name}))
		pi = make_purchase_invoice(pf.name)
		pi.insert()
		pf.reload()
		self.assertEqual(pf.purchase_invoice, pi.name)
		self.assertEqual(pf.items[0].purchase_invoice, pi.name)
		self.assertEqual(pf.items[0].pi_detail, pi.items[0].name)
		pi.submit()
		self.assertTrue(frappe.db.exists("GL Entry", {"voucher_no": pi.name, "is_cancelled": 0}))
		self.assertEqual(pi.reload().fiscal_status, "Pending (Draft)")
		pf.reload().submit()
		self.assertEqual(pi.reload().fiscal_status, "Completed")
		self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": pf.name}))
		with self.assertRaises(frappe.ValidationError):
			pi.cancel()
		pf.cancel()
		self.assertEqual(pi.reload().docstatus, 1)
		self.assertFalse(pi.purchase_factura)
		self.assertFalse(pf.reload().purchase_invoice)
		self.assertFalse(pf.items[0].purchase_invoice)
		self.assertFalse(pf.items[0].pi_detail)
		self.assertEqual(pi.fiscal_status, "Pending")
		replacement = self.factura(f_series=pf.f_series, f_number=pf.f_number)
		self.assertNotEqual(replacement.name, pf.name)
		self.assertEqual(replacement.docstatus, 0)
		pi.cancel()

	def test_mapped_pi_keeps_factura_posting_date(self):
		prev = frappe.db.get_single_value("eFactura Settings", "copy_date_from_factura")
		try:
			frappe.db.set_single_value("eFactura Settings", "copy_date_from_factura", 1)
			pf = self.factura(issue_date="2026-01-15")
			pi = make_purchase_invoice(pf.name)
			self.assertEqual(int(pi.set_posting_time or 0), 1)
			self.assertEqual(getdate(pi.posting_date), getdate("2026-01-15"))
			pi.insert()
			self.assertEqual(int(pi.reload().set_posting_time or 0), 1)
			self.assertEqual(getdate(pi.posting_date), getdate("2026-01-15"))
		finally:
			frappe.db.set_single_value("eFactura Settings", "copy_date_from_factura", prev)

	def test_amend_copies_original_file_and_allows_delete(self):
		content = b"%PDF-1.4 factura-original-bytes"
		source = frappe.get_doc(
			{"doctype": "File", "file_name": "pf-original.bin", "is_private": 1, "content": content}
		).insert()
		pf = self.factura(original_format="Paper", original_file=source.file_url)
		self.assertNotEqual(pf.original_file, source.file_url)
		pi = make_purchase_invoice(pf.name)
		pi.insert()
		pi.submit()
		pf.reload().submit()
		pf.cancel()
		amended = frappe.copy_doc(pf)
		amended.amended_from = pf.name
		amended.docstatus = 0
		amended.insert()
		self.assertNotEqual(amended.original_file, pf.reload().original_file)
		copied = frappe.get_doc("File", {"file_url": amended.original_file})
		copied_bytes = copied.get_content()
		if isinstance(copied_bytes, str):
			copied_bytes = copied_bytes.encode()
		self.assertEqual(copied_bytes, content)
		self.assertEqual(copied.attached_to_name, amended.name)
		amended_name = amended.name
		amended_url = amended.original_file
		original_url = pf.original_file
		amended.delete()
		self.assertFalse(frappe.db.exists("Purchase Factura", amended_name))
		self.assertFalse(frappe.db.exists("File", {"file_url": amended_url}))
		kept = frappe.get_doc("File", {"file_url": original_url}).get_content()
		if isinstance(kept, str):
			kept = kept.encode()
		self.assertEqual(kept, content)
		pf.delete()
		self.assertFalse(frappe.db.exists("Purchase Factura", pf.name))
		self.assertFalse(frappe.db.exists("File", {"file_url": original_url}))
		self.assertFalse(frappe.db.exists("File", source.name))

	def test_duplicate_and_cross_route_allocation_blocked(self):
		pf = self.factura(f_number="000001")
		with self.assertRaises(frappe.ValidationError):
			self.factura(f_number="000001")
		pi = make_purchase_invoice(pf.name).insert()
		with self.assertRaises(frappe.ValidationError):
			assert_no_pf_for_pi(pi.name)
		with self.assertRaises(frappe.ValidationError):
			assert_no_pf_for_pef(
				frappe._dict(
					company=pf.company,
					ef_supplier_idno=pf.f_supplier_idno,
					ef_series=pf.f_series,
					ef_number=pf.f_number,
				)
			)
		self.assertEqual(make_purchase_invoice(pf.name).name, pi.name)

	def test_pi_changes_cannot_break_allocations(self):
		pf = self.factura()
		pi = make_purchase_invoice(pf.name).insert()
		pi.items[0].qty = 2
		with self.assertRaises(frappe.ValidationError):
			pi.save()
		pi.reload()
		pi.purchase_factura = None
		with self.assertRaises(frappe.ValidationError):
			pi.save()

	def test_link_existing_and_unlink(self):
		from erpnext_moldova_efactura.utils.pf_invoice import linkable_purchase_invoices

		pf = self.factura()
		pi = make_purchase_invoice(pf.name)
		pi.purchase_factura = None
		pi.bill_no = None
		pi.bill_date = None
		pi.insert()
		pi.submit()
		rows = linkable_purchase_invoices(
			"Purchase Invoice",
			pi.name,
			"name",
			0,
			20,
			{"company": pf.company, "supplier": pf.supplier_party},
		)
		self.assertIn(pi.name, [row[0] for row in rows])
		from frappe.utils import formatdate

		row = next(item for item in rows if item[0] == pi.name)
		self.assertEqual(row[1], formatdate(pi.posting_date))
		link_purchase_invoice(pf.name, pi.name)
		self.assertEqual(pf.reload().purchase_invoice, pi.name)
		names = [
			row[0]
			for row in linkable_purchase_invoices(
				"Purchase Invoice",
				pi.name,
				"name",
				0,
				20,
				{"company": pf.company, "supplier": pf.supplier_party},
			)
		]
		self.assertNotIn(pi.name, names)
		unlink_purchase_invoice(pf.name)
		self.assertFalse(pf.reload().purchase_invoice)
		self.assertFalse(pf.items[0].purchase_invoice)
		self.assertEqual(pi.reload().docstatus, 1)
		self.assertFalse(pi.purchase_factura)

	def test_link_paid_submitted_invoice(self):
		pf = self.factura()
		pi = make_purchase_invoice(pf.name)
		pi.purchase_factura = None
		pi.bill_no = None
		pi.bill_date = None
		pi.insert()
		pi.submit()
		frappe.db.set_value("Purchase Invoice", pi.name, "outstanding_amount", 0)
		link_purchase_invoice(pf.name, pi.name)
		self.assertEqual(pf.reload().purchase_invoice, pi.name)
		self.assertEqual(pi.reload().purchase_factura, pf.name)
		self.assertEqual(flt(pi.outstanding_amount), 0)

	def test_company_checks(self):
		self.factura()
		with self.assertRaises(frappe.ValidationError):
			self.factura(f_customer_idno="9999999999999")

	def test_import_is_repeat_safe_and_preserves_original(self):
		path = EXAMPLES / "AAY9977940.signed.pdf"
		if not path.exists():
			self.skipTest("Private example PDF is not available")
		# Keep the IDNO match unambiguous even on a development site with a real ARAX fixture.
		for supplier in frappe.get_all("Supplier", filters={"tax_id": "1002600041697"}, pluck="name"):
			if supplier != self.supplier.name:
				frappe.db.set_value("Supplier", supplier, "tax_id", None)
		frappe.get_doc(
			{
				"doctype": "eFactura Supplier Item Map",
				"supplier": self.supplier.name,
				"supplier_item_name": "Access internet prin linie dedicata",
				"item_code": self.item.name,
				"uom": "Nos",
			}
		).insert()
		content = path.read_bytes()
		file = frappe.get_doc(
			{"doctype": "File", "file_name": path.name, "is_private": 1, "content": content}
		).insert()
		upload_name = file.name
		upload_url = file.file_url
		name = import_pdf(upload_url, self.company.name)
		self.assertFalse(frappe.db.exists("File", upload_name))
		repeat = frappe.get_doc(
			{"doctype": "File", "file_name": f"repeat-{path.name}", "is_private": 1, "content": content}
		).insert()
		repeat_name = repeat.name
		self.assertEqual(import_pdf(repeat.file_url, self.company.name), name)
		self.assertFalse(frappe.db.exists("File", repeat_name))
		pf = frappe.get_doc("Purchase Factura", name)
		self.assertNotEqual(pf.original_file, upload_url)
		owned_name = frappe.db.get_value(
			"File",
			{
				"attached_to_doctype": "Purchase Factura",
				"attached_to_name": pf.name,
				"attached_to_field": "original_file",
			},
			"name",
		)
		self.assertTrue(owned_name)
		self.assertEqual(frappe.db.get_value("File", owned_name, "file_url"), pf.original_file)
		self.assertEqual(pf.signature_status, "Indeterminate")
		self.assertEqual(pf.signature_integrity, "Passed")
		self.assertEqual(pf.signature_format, "adbe.pkcs7.detached")
		verified = verify_pdf_signature(pf.name)
		self.assertEqual(verified["signature_integrity"], "Passed")
		self.assertEqual(verified["signature_status"], "Indeterminate")
		self.assertNotEqual(verified["signature_status"], "Valid")
		pf.reload()
		self.assertEqual(pf.signature_status, "Indeterminate")
		self.assertEqual(pf.supplier_party, self.supplier.name)
		self.assertEqual(pf.f_supplier_address, "str.M. Dosoftei 118, mun.Chisi")
		self.assertEqual(pf.f_supplier_bank_account, "MD77FT222420100000415498")
		self.assertEqual(pf.f_customer_name, '"HOTEL LIFE" SRL')
		self.assertEqual(flt(pf.total), 270)
		self.assertEqual(pf.items[0].supplier_uom, "GB")
		self.assertEqual((pf.items[0].item_code, pf.items[0].uom), (self.item.name, "Nos"))
		pf.currency = "EUR"
		pf.f_conversion_rate = 20
		pf.save()
		self.assertEqual((pf.total, pf.f_total, pf.f_currency), (13.5, 270, "MDL"))
		self.assertEqual(pf.signature_status, "Indeterminate")
		self.assertEqual(pf.items[0].f_rate, 225)
		pf.items[0].f_rate = 226
		with self.assertRaises(frappe.ValidationError):
			pf.save()

	def test_sales_user_cannot_create_pf(self):
		roles = {p.role for p in frappe.get_meta("Purchase Factura").permissions if p.create}
		self.assertNotIn("Sales User", roles)
		self.assertIn("Purchase User", roles)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			import_pdf("/private/files/unknown.pdf", self.company.name)

	@patch("erpnext_moldova_efactura.utils.factura_ai._generate")
	def test_parser_import_does_not_call_ai(self, generate):
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "not-a-pdf.bin",
				"is_private": 1,
				"content": b"not a pdf",
			}
		).insert()
		upload_name, upload_url = file.name, file.file_url
		upload_path = file.get_full_path()
		with self.assertRaisesRegex(frappe.ValidationError, "accepts only PDF files"):
			import_pdf(file.file_url, self.company.name)
		generate.assert_not_called()
		self.assertFalse(frappe.db.exists("File", upload_name))
		self.assertFalse(frappe.db.exists("File", {"file_url": upload_url}))
		self.assertFalse(Path(upload_path).exists())

	def _ai_extraction(self):
		return {
			"provider": "Gemini",
			"series": "AAZ",
			"number": "1606962",
			"issue_date": "2026-08-29",
			"delivery_date": "2026-08-29",
			"supplier_name": "ARAX-IMPEX SRL",
			"supplier_idno": "1002600041697",
			"buyer_name": "HOTEL LIFE SRL",
			"buyer_idno": "1024600026571",
			"currency": "MDL",
			"net_total": "225.00",
			"vat_total": "45.00",
			"total": "270.00",
			"original_format": "Paper",
			"signature_status": "Not Applicable",
			"file_hash": "0" * 64,
			"items": [
				{
					"description": "Access internet",
					"source_uom": "GB",
					"source_qty": "1",
					"source_rate": "225.00",
					"net_amount": "225.00",
					"vat_rate": "20",
					"vat_amount": "45.00",
					"amount": "270.00",
				}
			],
		}

	@patch("erpnext_moldova_efactura.utils.factura_ai.parse_image")
	def test_ai_text_pdf_import_verifies_signatures(self, parse_image):
		path = EXAMPLES / "AAY9977940.signed.pdf"
		if not path.exists():
			self.skipTest("Private example PDF is not available")
		parsed = self._ai_extraction()
		parsed["file_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
		parse_image.return_value = parsed
		file = frappe.get_doc(
			{"doctype": "File", "file_name": "ai-signed.pdf", "is_private": 1, "content": path.read_bytes()}
		).insert()
		name = import_pdf(file.file_url, self.company.name, use_ai=1)
		pf = frappe.get_doc("Purchase Factura", name)
		self.assertEqual(pf.original_format, "Digitally Signed PDF")
		self.assertEqual(pf.signature_integrity, "Passed")
		self.assertEqual(pf.signature_status, "Indeterminate")
		self.assertEqual(pf.signature_format, "adbe.pkcs7.detached")

	@patch("erpnext_moldova_efactura.utils.factura_ai.parse_image")
	def test_ai_scan_pdf_import_skips_signature_check(self, parse_image):
		from pypdf import PdfWriter

		buffer = io.BytesIO()
		writer = PdfWriter()
		writer.add_blank_page(width=100, height=100)
		writer.write(buffer)
		content = buffer.getvalue()
		parsed = self._ai_extraction()
		parsed["file_hash"] = hashlib.sha256(content).hexdigest()
		parse_image.return_value = parsed
		file = frappe.get_doc(
			{"doctype": "File", "file_name": "ai-scan.pdf", "is_private": 1, "content": content}
		).insert()
		name = import_pdf(file.file_url, self.company.name, use_ai=1)
		pf = frappe.get_doc("Purchase Factura", name)
		self.assertEqual(pf.original_format, "Paper")
		self.assertEqual(pf.signature_status, "Not Applicable")

	@patch("erpnext_moldova_efactura.utils.factura_ai.parse_image")
	def test_ai_import_swaps_inverted_metro_parties(self, parse_image):
		content = self._jpeg_with_exif()
		file = frappe.get_doc(
			{"doctype": "File", "file_name": "metro-till.jpg", "is_private": 1, "content": content}
		).insert()
		parsed = self._ai_extraction()
		parsed["number"] = "1830389"
		parsed["supplier_name"] = "HOTEL LIFE SRL"
		parsed["supplier_idno"] = "1024600026571"
		parsed["buyer_name"] = "I.C.S METRO CASH & CARRY MOLDOVA S.R.L."
		parsed["buyer_idno"] = "1004601002738"
		parsed["file_hash"] = hashlib.sha256(Path(file.get_full_path()).read_bytes()).hexdigest()
		parse_image.return_value = parsed
		name = import_pdf(file.file_url, self.company.name, use_ai=1)
		pf = frappe.get_doc("Purchase Factura", name)
		self.assertEqual(pf.f_customer_idno, "1024600026571")
		self.assertEqual(pf.f_supplier_idno, "1004601002738")
		self.assertEqual(pf.f_supplier_name, "I.C.S METRO CASH & CARRY MOLDOVA S.R.L.")

	@patch("erpnext_moldova_efactura.utils.factura_ai.parse_image")
	def test_ai_import_matches_glued_company_idno_and_vat(self, parse_image):
		content = self._jpeg_with_exif()
		file = frappe.get_doc(
			{"doctype": "File", "file_name": "metro-glued-idno.jpg", "is_private": 1, "content": content}
		).insert()
		parsed = self._ai_extraction()
		parsed["number"] = "1830389"
		parsed["supplier_name"] = "I.C.S METRO CASH & CARRY MOLDOVA S.R.L."
		parsed["supplier_idno"] = "1004601002738"
		parsed["buyer_idno"] = "1024600026571"
		parsed["file_hash"] = hashlib.sha256(Path(file.get_full_path()).read_bytes()).hexdigest()
		parse_image.return_value = parsed
		prev = frappe.db.get_value("Company", self.company.name, "tax_id")
		try:
			frappe.db.set_value("Company", self.company.name, "tax_id", "10246000265710211775")
			name = import_pdf(file.file_url, self.company.name, use_ai=1)
			pf = frappe.get_doc("Purchase Factura", name)
			self.assertEqual(pf.f_customer_idno, "1024600026571")
			self.assertEqual(pf.f_supplier_idno, "1004601002738")
		finally:
			frappe.db.set_value("Company", self.company.name, "tax_id", prev)

	def _jpeg_with_exif(self):
		from PIL import Image

		image = Image.new("RGB", (48, 32), (10, 20, 30))
		exif = image.getexif()
		exif[271] = "Test"
		buffer = io.BytesIO()
		image.save(buffer, format="JPEG", quality=95, exif=exif)
		return buffer.getvalue()

	def test_save_uploaded_original_keeps_jpeg_bytes(self):
		from erpnext_moldova_efactura.utils.pf_original import save_uploaded_original

		prev = frappe.db.get_single_value("System Settings", "strip_exif_metadata_from_uploaded_images")
		try:
			frappe.db.set_single_value("System Settings", "strip_exif_metadata_from_uploaded_images", 1)
			content = self._jpeg_with_exif()
			frappe.local.uploaded_file = content
			frappe.local.uploaded_filename = "paper-scan.jpg"
			frappe.form_dict.is_private = 1
			file = save_uploaded_original()
			self.assertEqual(Path(file.get_full_path()).read_bytes(), content)
			self.assertEqual(cint(file.file_size), len(content))
		finally:
			frappe.db.set_single_value("System Settings", "strip_exif_metadata_from_uploaded_images", prev)

	@patch("erpnext_moldova_efactura.utils.factura_ai.parse_image")
	def test_ai_jpeg_import_allows_mapping_save(self, parse_image):
		prev = frappe.db.get_single_value("System Settings", "strip_exif_metadata_from_uploaded_images")
		try:
			frappe.db.set_single_value("System Settings", "strip_exif_metadata_from_uploaded_images", 1)
			content = self._jpeg_with_exif()
			file = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "paper-factura.jpg",
					"is_private": 1,
					"content": content,
				}
			).insert()
			uploaded = Path(file.get_full_path()).read_bytes()
			parsed = self._ai_extraction()
			parsed["number"] = "1606999"
			parsed["file_hash"] = hashlib.sha256(uploaded).hexdigest()
			parse_image.return_value = parsed
			name = import_pdf(file.file_url, self.company.name, use_ai=1)
			pf = frappe.get_doc("Purchase Factura", name)
			self.assertNotEqual(pf.original_file, file.file_url)
			pf.supplier_party = self.supplier.name
			pf.items[0].item_code = self.item.name
			pf.save()
		finally:
			frappe.db.set_single_value("System Settings", "strip_exif_metadata_from_uploaded_images", prev)

	def test_pf_schema_uses_original_prefix_and_invoice_link(self):
		for doctype in ("Purchase Factura", "Purchase Factura Item"):
			meta = frappe.get_meta(doctype)
			self.assertFalse(any(f.fieldname.startswith("ef_") for f in meta.fields))
		pf_meta = frappe.get_meta("Purchase Factura")
		for old_field in (
			"supplier",
			"series",
			"number",
			"supplier_idno",
			"supplier_name",
			"supplier_vat_id",
			"buyer_idno",
			"buyer_vat_id",
		):
			self.assertFalse(pf_meta.has_field(old_field), old_field)
		for fieldname in (
			"supplier_party_type",
			"supplier_party",
			"f_series",
			"f_number",
			"f_supplier_idno",
			"f_supplier_name",
			"f_supplier_vat_id",
			"f_supplier_taxpayer_type",
			"f_supplier_address",
			"f_supplier_bank_account",
			"f_supplier_bank_name",
			"f_supplier_bank_code",
			"f_customer_idno",
			"f_customer_name",
			"f_customer_vat_id",
			"f_customer_taxpayer_type",
			"f_customer_address",
			"f_customer_bank_account",
			"f_customer_bank_name",
			"f_customer_bank_code",
		):
			self.assertTrue(pf_meta.has_field(fieldname), fieldname)
		self.assertEqual(pf_meta.get_field("supplier_party").options, "supplier_party_type")
		meta = frappe.get_meta("Purchase Factura Item")
		self.assertFalse(meta.has_field("expense_account"))
		self.assertFalse(meta.has_field("cost_center"))
		self.assertEqual(meta.get_field("purchase_invoice").options, "Purchase Invoice")

	def test_foreign_currency_invoice_and_vat_modes(self):
		parent = frappe.db.get_value(
			"Account", {"company": self.company.name, "is_group": 1, "root_type": "Liability"}, "name"
		)
		account = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "_Test PF EUR Payable",
				"company": self.company.name,
				"parent_account": parent,
				"account_type": "Payable",
				"account_currency": "EUR",
			}
		).insert()
		supplier = frappe.get_doc("Supplier", self.supplier.name)
		supplier.default_currency = "EUR"
		supplier.append("accounts", {"company": self.company.name, "account": account.name})
		supplier.save()
		for inclusive in (0, 1):
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", inclusive)
			pf = self.factura(currency="EUR", f_currency="MDL", f_conversion_rate=20)
			self.assertEqual((pf.f_net_total, pf.f_vat_total, pf.f_total), (225, 45, 270))
			self.assertEqual((pf.net_total, pf.vat_total, pf.total), (11.25, 2.25, 13.5))
			self.assertEqual(pf.items[0].rate, 13.5 if inclusive else 11.25)
			pi = make_purchase_invoice(pf.name).insert()
			self.assertEqual(pi.currency, "EUR")
			self.assertEqual(pi.conversion_rate, 20)
			self.assertEqual(flt(pi.base_grand_total, 2), 270)
			pi.submit()
			pf.reload().submit()
			self.assertEqual(pi.reload().fiscal_status, "Completed")

	def test_uom_conversion_is_frozen_and_price_uses_purchase_qty(self):
		for name in ("Box", "Pack"):
			if not frappe.db.exists("UOM", name):
				frappe.get_doc({"doctype": "UOM", "uom_name": name}).insert()
		item = frappe.get_doc("Item", self.item.name)
		item.append("uoms", {"uom": "Box", "conversion_factor": 12})
		item.append("uoms", {"uom": "Pack", "conversion_factor": 6})
		item.save()
		pf = self.factura(
			items=[
				{
					"supplier_item_name": "Service bundles",
					"item_code": item.name,
					"supplier_uom": "Box",
					"f_uom": "Box",
					"f_qty": 2,
					"uom": "Pack",
					"f_rate": 225,
					"f_vat_rate": 20,
				}
			]
		)
		row = pf.items[0]
		self.assertEqual(
			(row.f_conversion_factor, row.conversion_factor, row.stock_qty, row.qty), (12, 6, 24, 4)
		)
		pi = make_purchase_invoice(pf.name).insert()
		self.assertEqual((pi.items[0].qty, pi.items[0].rate, pi.items[0].stock_qty), (4, 112.5, 24))
		unlink_purchase_invoice(pf.name)
		for conversion in item.uoms:
			if conversion.uom == "Box":
				conversion.conversion_factor = 24
		item.save()
		pf.reload().save()
		self.assertEqual((pf.items[0].f_conversion_factor, pf.items[0].qty), (12, 4))
		pf.items[0].uom = "Nos"
		pf.save()
		self.assertEqual(pf.items[0].qty, 48)
	def test_rate_refresh_missing_rate(self):
		pf = self.factura(currency="EUR", f_currency="MDL", f_conversion_rate=20)
		pf.f_conversion_rate = 25
		pf.save()
		self.assertEqual(pf.total, 10.8)
		pf.currency = "USD"
		with patch("erpnext.setup.utils.get_exchange_rate", return_value=18):
			pf.save()
		self.assertEqual(pf.f_conversion_rate, 18)
		pf.currency = "EUR"
		with patch("erpnext.setup.utils.get_exchange_rate", return_value=0):
			with self.assertRaises(frappe.ValidationError):
				pf.save()

	def test_reverse_currency(self):
		pf = self.factura(currency="MDL", f_currency="EUR", f_conversion_rate=0.05)
		self.assertEqual(pf.total, 5400)
		pi = make_purchase_invoice(pf.name).insert()
		self.assertEqual(pi.conversion_rate, 1)
		self.assertEqual(flt(pi.grand_total, 2), 5400)

	def test_zero_amount_edit_clears_converted_values(self):
		pf = self.factura()
		pf.items[0].f_rate = pf.items[0].f_net_amount = pf.items[0].f_vat_amount = 0
		pf.save()
		self.assertEqual((pf.total, pf.f_total, pf.items[0].amount, pf.items[0].rate), (0, 0, 0, 0))

	def test_preview_and_supplier_currency_defaults(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura import (
			party_defaults,
			preview_amounts,
		)

		frappe.db.set_value("Supplier", self.supplier.name, "default_currency", "EUR")
		self.assertEqual(party_defaults(self.company.name, self.supplier.name)["currency"], "EUR")
		pf = self.factura()
		pf.currency = "EUR"
		pf.f_conversion_rate = 20
		result = preview_amounts(pf.as_json())
		self.assertEqual(result["header"]["total"], 13.5)
		self.assertEqual(pf.reload().currency, "MDL")
		pf.currency = "EUR"
		pf.f_conversion_rate = 20
		pf.save()
		# Desk JSON dates must not make an unchanged saved exchange rate look stale.
		with patch("erpnext.setup.utils.get_exchange_rate", side_effect=AssertionError("Unexpected lookup")):
			result = preview_amounts(pf.as_json())
		self.assertEqual(result["header"]["f_conversion_rate"], 20)


class TestPurchaseFacturaInvoiceMatch(TestCase):
	def test_line_net_may_differ_by_rounding(self):
		from types import SimpleNamespace

		from erpnext_moldova_efactura.utils.pf_invoice import _line_amounts_ok, _line_identity

		pf = SimpleNamespace(item_code="ITEM", uom="Kg", qty=0.453, net_amount=12.47, amount=14.96)
		pi = SimpleNamespace(item_code="ITEM", uom="KG", qty=0.453, net_amount=12.50, amount=15.00)
		self.assertTrue(_line_identity(pf, pi))
		self.assertTrue(_line_amounts_ok(pf, pi))
		pi.net_amount, pi.amount = 13.00, 15.60
		self.assertFalse(_line_amounts_ok(pf, pi))

	def test_split_pi_rows_cover_one_factura_line(self):
		from types import SimpleNamespace

		from erpnext_moldova_efactura.utils.pf_invoice import _cover_pf_pi_items

		row = SimpleNamespace(item_code="EMB", uom="Nos", qty=80, net_amount=800, amount=944)
		pi_rows = [
			SimpleNamespace(item_code="EMB", uom="Nos", qty=40, net_amount=400, amount=472),
			SimpleNamespace(item_code="EMB", uom="Nos", qty=40, net_amount=400, amount=472),
		]
		chosen = _cover_pf_pi_items(row, pi_rows)
		self.assertEqual(len(chosen), 2)
