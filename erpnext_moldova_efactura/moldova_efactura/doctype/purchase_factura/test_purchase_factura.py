import os
import re
import unittest
import urllib.error
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura import import_pdf
from erpnext_moldova_efactura.utils.factura_ocr import (
	OCR_ERROR,
	_bank_details,
	_is_total_row,
	_items,
	_missing_image_dependencies,
	_party_address,
	_party_name,
	_totals,
	parse_image,
)
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

	def test_ocr_rows_and_totals_are_accepted_only_when_they_reconcile(self):
		text = """Schimb bec auto stop  buc  1  66-67  66-67  20-00  13-33  80-00
12. Total (pe factura fiscala) 66-67 x 13-33 80-00"""
		rows = _items(text)
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0]["description"], rows[0]["amount"]), ("Schimb bec auto stop", "80.00"))
		self.assertEqual(_totals(text), [(decimal("66.67"), decimal("13.33"), decimal("80.00"))])
		self.assertFalse(_items(text.replace("80-00", "81-00", 1)))

	def test_ocr_rows_allow_table_separators(self):
		text = "Servicii de transport auto international | Serv. | 1 | 4,800.00 | 4,800.00 | 0 | 0.00 | 4,800.00 |"
		rows = _items(text)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["amount"], "4800.00")

	def test_ocr_rows_recover_numbers_separated_by_noise(self):
		text = "Schimb bec auto stop buc 1 66-67 ruido 66-67 ruido 20 ruido 13-33 ruido 80-00"
		rows = _items(text)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["amount"], "80.00")

	def test_ocr_totals_accept_item_totals_when_detected_net_has_ocr_error(self):
		from erpnext_moldova_efactura.utils.factura_ocr import _totals_reconcile

		self.assertTrue(
			_totals_reconcile(
				tuple(map(decimal, ("482.51", "96.49", "579.00"))),
				tuple(map(decimal, ("482.61", "96.49", "579.00"))),
			)
		)
		self.assertFalse(
			_totals_reconcile(
				tuple(map(decimal, ("482.51", "96.49", "579.00"))),
				tuple(map(decimal, ("482.61", "96.49", "573.00"))),
			)
		)

	def test_ocr_item_name_strips_table_artifacts(self):
		text = "= Servicii de transport auto international | Serv. | 1 | 4,800.00 | 4,800.00 | 0 | 0.00 | 4,800.00 |"
		self.assertEqual(_items(text)[0]["description"], "Servicii de transport auto international")

	def test_grid_total_rows_are_not_counted_as_items_when_label_is_partial(self):
		self.assertTrue(_is_total_row("11. TOTAL (pe pagină)"))
		self.assertTrue(_is_total_row("12. TOTAL"))
		self.assertFalse(_is_total_row("Servicii de transport auto international"))

	def test_invalid_scan_returns_quality_error(self):
		with self.assertRaisesRegex(
			FacturaImportError, re.escape(f"{OCR_ERROR}: the source is not a supported JPEG or PNG image")
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

	@patch("erpnext_moldova_efactura.utils.factura_ocr.import_module")
	def test_missing_image_dependencies_are_reported(self, import_module):
		import_module.side_effect = [ImportError, ImportError, ImportError]
		self.assertEqual(
			_missing_image_dependencies(), ["opencv-python-headless", "numpy", "pytesseract"]
		)

	def test_ocr_party_name_stops_before_requisites(self):
		block = (
			"MAGAS TRANS S.R.L., R.M., MD-2009, mun. Chisinau, str. Ion Ganea 1/A, "
			"c/d ef/nr.TVA 1004600061235/0207163 A 1. Поставщик"
		)
		self.assertEqual(_party_name(block), "MAGAS TRANS S.R.L.")
		self.assertEqual(_party_name("i HOTEL LIFE SRL, R.M., MD-2011, mun. Chisinau"), "HOTEL LIFE SRL")
		self.assertEqual(_party_address(block), "R.M., MD-2009, mun. Chisinau, str. Ion Ganea 1/A")
		self.assertEqual(_party_address("i HOTEL LIFE SRL, R.M., MD-2011, mun. Chisinau, c/d MD46AG"), "R.M., MD-2011, mun. Chisinau")
		self.assertEqual(
			_party_name("‘Hotel Life’ S.R.L. mun.Chisinau or.Codru str.Grenoble 128/2 ap.31"),
			"‘Hotel Life’ S.R.L.",
		)
		self.assertEqual(
			_party_name("SRL 'Nifestcom’ m.Chisinau, str.Padurii 6/1 IBAN MD52MO2224ASV12246927100"),
			"SRL 'Nifestcom’",
		)
		self.assertEqual(
			_party_address(
				"‘Hotel Life’ S.R.L. mun.Chisinau or.Codru str.Grenoble 128/2 ap.31 "
				"IBAN MD46AG000000022516020091 in BC Moldova"
			),
			"mun.Chisinau or.Codru str.Grenoble 128/2 ap.31",
		)
		self.assertEqual(
			_party_address("BIC c"),
			"",
		)

	def test_ocr_extracts_source_bank_names_and_codes(self):
		self.assertEqual(
			_bank_details("IBAN MD52MO2224ASV12246927100 In BC Moblasbanca-OTP Group SA PRCBMD22"),
			('BC "MOBLASBANCA-OTP GROUP" SA', "PRCBMD22"),
		)
		self.assertEqual(
			_bank_details("IBAN MD46AG000000022516020091 in BC Moldova Agroindbank SA AGRNMD2X805"),
			("BC MOLDOVA AGROINDBANK SA", "AGRNMD2X805"),
		)
		self.assertEqual(
			_bank_details(
				"IBAN MD52MO2224ASV12246927100 In BC Moblasbanca-OTP Group ” SA PRCBMD22"
			),
			('BC "MOBLASBANCA-OTP GROUP" SA', "PRCBMD22"),
		)
		self.assertEqual(
			_bank_details(
				"IBAN MD46AG000000022516020091 in BC Moldova Agroindbank — |O.F./NR.TVA "
				"MOKYNARENV/NONYUAREN» SA CHISINAU AGRNMD2X805"
			),
			("BC MOLDOVA AGROINDBANK SA", "AGRNMD2X805"),
		)

	def test_retail_till_factura_ocr(self):
		from erpnext_moldova_efactura.utils.factura_ocr import parse_retail_text

		text = """
FACTURA FISCALA
I.C.S METRO CASH & CARRY MOLDOVA S.R.L.
IDNO 1004601002738
Str. Chisinau 5, MD-4839
IBAN MD29VI000002251921103MDL in BC Victoriabank SA VIEXMD2X
HOTEL LIFE SRL
Strada Grenoble 128/2 Ap.31
1024600026571 / 0211775
IBAN MD46AG000000022516020091 in BC Moldova Agroindbank SA AGRNMD2X
SERIA AAQ NR. 1838180 Bon fiscal nr. 36
Client 002 802279 SC
29-08-2026 13:32
Cod articol Denumire articol Unit vanz Mod amb Cant Pret unitar Pret colet Valoare fara TVA % TVA Valoare TVA Reducere Valoare incl. TVA
4000005319066 BIC RADIERA GALET 1 IM 3 20.75 62.25 62.25 20 12.45 0.00 74.70
PL/PA: 2.3400 / 35.04%
6931597514035 BIBLIORAFT CLASSIC 50MM VERDE 1 ST 1 29.92 29.92 29.92 20 5.98 0.00 35.90
4840842033998 CAIET 24 FILE LINIE COLOR 1 BU 4 3.12 12.48 12.48 20 2.50 0.00 14.98
Total cantitate 8
Val. tot. fara TVA 104.65
"""
		data = parse_retail_text(text)
		self.assertEqual((data["series"], data["number"], data["total"]), ("AAQ", "1838180", "125.58"))
		self.assertEqual(data["supplier_idno"], "1004601002738")
		self.assertEqual(data["buyer_idno"], "1024600026571")
		self.assertEqual(data["related_document_number"], "36")
		self.assertEqual(len(data["items"]), 3)
		self.assertEqual(data["items"][0]["supplier_item_code"], "4000005319066")
		self.assertEqual(data["items"][0]["source_uom"], "IM")
		self.assertIn("Chisinau", data["supplier_address"])
		self.assertIn("Grenoble", data["buyer_address"])

	def test_retail_ocr_uses_footer_totals_not_column_headers(self):
		from erpnext_moldova_efactura.utils.factura_ocr import parse_retail_text

		text = """
I.C.S METRO CASH & CARRY MOLDOVA S.R.L. 1004601002738
HOTEL LIFE SRL 1024600026571 / 0211775
SERIA AAQ NR. 1838180 Bon fiscal nr. 36
29-08-2026
Cod articol Valoare fara TVA % TVA Valoare TVA Valoare incl. TVA
4000005319066 BIC RADIERA GALET IM 3 20.75 62.25 20 12.45 0.00 74.70
6931597514035 BIBLIORAFT CLASSIC 50MM VERDE 1 29.92 20 35.90
4000005319000 MARKER 2
12.50 20 15.00
Total cantitate 6
Val. tot. fara TVA 104.67
"""
		data = parse_retail_text(text)
		self.assertEqual(data["total"], "125.60")
		self.assertEqual(len(data["items"]), 3)
		self.assertEqual(data["items"][1]["description"], "BIBLIORAFT CLASSIC 50MM VERDE")
		self.assertEqual(data["items"][2]["supplier_item_code"], "4000005319000")

	def test_retail_ocr_does_not_double_count_two_ocr_passes(self):
		from erpnext_moldova_efactura.utils.factura_ocr import parse_retail_text

		once = """
I.C.S METRO CASH & CARRY MOLDOVA S.R.L. 1004601002738
HOTEL LIFE SRL 1024600026571 / 0211775
SERIA AAQ NR. 1838180 Bon fiscal nr. 36
29-08-2026
4000005319066 BIC RADIERA GALET IM 3 20.75 62.25 20 12.45 0.00 74.70
6931597514035 BIBLIORAFT CLASSIC 50MM VERDE ST 1 29.92 20 5.98 0.00 35.90
4840842033998 CAIET 24 FILE LINIE COLOR BU 4 3.12 12.48 20 2.50 0.00 14.98
Total cantitate 8
Val. tot. fara TVA 104.65
"""
		noisy = once.replace("3.12 12.48 20 2.50 0.00 14.98", "3.10 12.40 20 2.48 0.00 14.88")
		data = parse_retail_text(once, noisy)
		self.assertEqual((data["total"], len(data["items"])), ("125.58", 3))
		self.assertEqual(sum(float(row["source_qty"]) for row in data["items"]), 8)

	def test_retail_ocr_rejects_incomplete_rows_against_footer(self):
		from erpnext_moldova_efactura.utils.factura_ocr import parse_retail_text

		text = """
I.C.S METRO CASH & CARRY MOLDOVA S.R.L. 1004601002738
HOTEL LIFE SRL 1024600026571 / 0211775
SERIA AAQ NR. 1838180
29-08-2026 Bon fiscal nr. 36
4000005319066 BIC RADIERA GALET IM 3 20.75 62.25 20 12.45 0.00 74.70
Total cantitate 69
Val tot fara TVA 1251,02
"""
		with self.assertRaises(FacturaImportError) as ctx:
			parse_retail_text(text)
		self.assertIn("printed net 1251.02", str(ctx.exception))

	def test_retail_layout_is_not_used_for_numbered_forms(self):
		from erpnext_moldova_efactura.utils.factura_ocr import _is_retail

		self.assertFalse(
			_is_retail("1. Furnizor MAGAS TRANS S.R.L.\n12. TOTAL (pe factura fiscala) 4800.00 X 0.00 4800.00")
		)
		self.assertTrue(_is_retail("METRO CASH & CARRY SERIA AAQ NR. 1838180 Bon fiscal nr. 36"))

	def test_ocr_party_labels_allow_photo_variants(self):
		from erpnext_moldova_efactura.utils.factura_ocr import _party

		text = (
			"1.Furnizor: MAGAS TRANS S.R.L.\n"
			"2 Cumpărător/i. HOTEL LIFE SRL\n"
			"3. Delegaţie"
		)
		supplier = _party(text, r"1[.:\s]+Fur(?:n|m)izor", r"2[.:\s]+Cump(?:arator|ărător)(?:i)?")
		customer = _party(text, r"2[.:\s]+Cump(?:arator|ărător)(?:i)?(?:\s*/\s*beneficiar)?", r"3[.:\s]+Deleg")
		self.assertEqual(supplier, "MAGAS TRANS S.R.L.")
		self.assertEqual(customer, "HOTEL LIFE SRL")

	def test_imported_idno_formatting_does_not_count_as_original_change(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura import _changed

		current = frappe._dict(f_supplier_idno="100 260 004 1697")
		previous = frappe._dict(f_supplier_idno="1002600041697")
		current.meta = previous.meta = frappe.get_meta("Purchase Factura")
		self.assertFalse(_changed(current, previous, "f_supplier_idno"))
		self.assertEqual(
			_party_address("HOTEL LIFE SRL, R.M., MD-2011, mun. Chișinău, or. Codru, Grenoble, 128/2, ap.(of.) 31, ‘"),
			"R.M., MD-2011, mun. Chișinău, or. Codru, Grenoble, 128/2, ap.(of.) 31",
		)

	@unittest.skipUnless(os.environ.get("GEMINI_API_KEY"), "GEMINI_API_KEY is not set")
	def test_actual_photographed_facturas(self):
		for filename, series, number, total, item_count in (
			("IMG_20260906_115807.jpg", "AAZ", "1606962", "4800.00", 1),
			("IMG_20260906_120600.jpg", "AAY", "7128757", "579.00", 6),
			("IMG_20260906_120644.jpg", "AAY", "7128754", "13260.00", 22),
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
				self.assertEqual(len(data["signature_details"]), 1)
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
		self.assertEqual(pi.fiscal_status, "Pending")
		with self.assertRaises(frappe.ValidationError):
			self.factura(f_series=pf.f_series, f_number=pf.f_number)
		pi.cancel()
		amended = frappe.copy_doc(pf)
		amended.docstatus = 0
		amended.amended_from = pf.name
		amended.purchase_invoice = None
		amended.insert()
		amended.save()
		replacement_pi = make_purchase_invoice(amended.name).insert()
		replacement_pi.submit()
		amended.reload().submit()
		amended.cancel()
		replacement_pi.cancel()
		second_amendment = frappe.copy_doc(amended)
		second_amendment.docstatus = 0
		second_amendment.amended_from = amended.name
		second_amendment.purchase_invoice = None
		second_amendment.insert()

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
		pf = self.factura()
		pi = make_purchase_invoice(pf.name)
		pi.purchase_factura = None
		pi.insert()
		pi.submit()
		link_purchase_invoice(pf.name, pi.name)
		self.assertEqual(pf.reload().purchase_invoice, pi.name)
		unlink_purchase_invoice(pf.name)
		self.assertFalse(pf.reload().purchase_invoice)
		self.assertFalse(pf.items[0].purchase_invoice)
		self.assertEqual(pi.reload().docstatus, 1)
		self.assertFalse(pi.purchase_factura)

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
		file = frappe.get_doc(
			{"doctype": "File", "file_name": path.name, "is_private": 1, "content": path.read_bytes()}
		).insert()
		name = import_pdf(file.file_url, self.company.name)
		self.assertEqual(import_pdf(file.file_url, self.company.name), name)
		pf = frappe.get_doc("Purchase Factura", name)
		self.assertEqual(pf.signature_status, "Not Checked")
		self.assertEqual(pf.supplier_party, self.supplier.name)
		self.assertEqual(pf.f_supplier_address, "str.M. Dosoftei 118, mun.Chisi")
		self.assertEqual(pf.f_supplier_bank_account, "MD77FT222420100000415498")
		self.assertEqual(pf.f_customer_name, '"HOTEL LIFE" SRL')
		self.assertEqual(flt(pf.total), 270)
		self.assertEqual(pf.items[0].supplier_uom, "GB")
		self.assertEqual((pf.items[0].item_code, pf.items[0].uom), (self.item.name, "Nos"))
		with self.assertRaises(frappe.ValidationError):
			file.delete()
		pf.currency = "EUR"
		pf.f_conversion_rate = 20
		pf.save()
		self.assertEqual((pf.total, pf.f_total, pf.f_currency), (13.5, 270, "MDL"))
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

	def test_prefix_migration_preserves_currency_values_and_links(self):
		from erpnext_moldova_efactura.patches.v3_0.rename_pf_original_prefix import execute

		if not frappe.db.has_column("Purchase Factura", "ef_currency"):
			self.skipTest("Intermediate schema columns are absent")
		pf = self.factura()
		pi = make_purchase_invoice(pf.name).insert()
		frappe.db.sql(
			"""update `tabPurchase Factura`
			set f_currency=NULL, ef_currency='MDL', ef_conversion_rate=20,
			ef_net_total=225, ef_vat_total=45, ef_total=270 where name=%s""",
			pf.name,
		)
		frappe.db.sql(
			"""update `tabPurchase Factura Item`
			set ef_qty=1, ef_rate=225, ef_net_amount=225, ef_vat_amount=45,
			ef_amount=270, purchase_invoice=NULL where parent=%s""",
			pf.name,
		)
		execute()
		pf.reload()
		self.assertEqual((pf.f_currency, pf.f_conversion_rate, pf.f_total), ("MDL", 20, 270))
		self.assertEqual((pf.items[0].f_rate, pf.items[0].f_amount), (225, 270))
		self.assertEqual(pf.items[0].purchase_invoice, pi.name)
		frappe.db.set_value("Purchase Factura", pf.name, "f_conversion_rate", 25)
		execute()
		self.assertEqual(pf.reload().f_conversion_rate, 25)

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

	def test_legacy_migration_preserves_original_and_quantities(self):
		from erpnext_moldova_efactura.patches.v3_0.align_pf_currency_uom import execute

		if not frappe.db.has_column("Purchase Factura Item", "source_qty"):
			self.skipTest("Legacy columns are absent on a fresh installation")
		pf = self.factura()
		frappe.db.sql(
			"""update `tabPurchase Factura Item`
			set description='Legacy service', source_qty=2, source_rate=112.5, source_uom='unit', vat_rate=20,
			amount=270, qty=4, conversion_factor=3 where name=%s""",
			pf.items[0].name,
		)
		frappe.db.set_value("Purchase Factura", pf.name, "f_currency", None)
		execute()
		pf.reload()
		self.assertEqual((pf.currency, pf.f_currency, pf.f_conversion_rate), ("MDL", "MDL", 1))
		self.assertEqual((pf.f_total, pf.items[0].supplier_item_name), (270, "Legacy service"))
		self.assertEqual(
			(pf.items[0].f_qty, pf.items[0].qty, pf.items[0].stock_qty, pf.items[0].f_conversion_factor),
			(2, 4, 12, 6),
		)
		execute()
		pf.reload().save()
		self.assertEqual((pf.items[0].qty, pf.total), (4, 270))

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
