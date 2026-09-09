import json
import unittest
from pathlib import Path

import io

from erpnext_moldova_efactura.utils.factura_pdf import is_pdf_image_scan, parse_pdf
from erpnext_moldova_efactura.utils.factura_pdf_signature import (
	inspect_pdf_signatures,
	verify_pdf_signatures,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class TestFacturaPDFSignature(unittest.TestCase):
	def test_arax_and_orange_cms_integrity_is_not_full_validity(self):
		cases = (
			(
				"AAY9977940.signed.pdf",
				"sig",
				"adbe.pkcs7.detached",
				0,
				False,
			),
			(
				"74085315_1_FiscalInvoice.pdf",
				"Argint Dana",
				"ETSI.CAdES.detached",
				421695,
				True,
			),
		)
		for filename, field, subfilter, trailing, zeros in cases:
			path = EXAMPLES / filename
			with self.subTest(filename=filename):
				if not path.exists():
					self.skipTest("Private example PDF is not available")
				content = path.read_bytes()
				inspected = inspect_pdf_signatures(content)
				self.assertEqual(inspected["signature_status"], "Not Checked")
				self.assertEqual(inspected["signature_integrity"], "Not Checked")
				self.assertEqual(inspected["signature_field"], field)
				self.assertEqual(inspected["signature_format"], subfilter)
				self.assertEqual(inspected["signature_certificate_trust"], "Not Checked")
				parsed = parse_pdf(content)
				self.assertEqual(parsed["signature_status"], "Not Checked")
				self.assertEqual(parsed["signature_format"], subfilter)
				verified = verify_pdf_signatures(content)
				self.assertEqual(verified["signature_integrity"], "Passed")
				self.assertEqual(verified["signature_status"], "Indeterminate")
				self.assertNotEqual(verified["signature_status"], "Valid")
				self.assertEqual(verified["signature_certificate_trust"], "Not Checked")
				self.assertEqual(verified["signature_revocation"], "Not Checked")
				evidence = json.loads(verified["signature_evidence"])
				self.assertEqual(evidence["signatures"][0]["trailing_bytes"], trailing)
				self.assertEqual(evidence["signatures"][0]["trailing_all_zero"], zeros)
				self.assertIn("CMS Verification successful", evidence["signatures"][0]["openssl_message"])
				if filename.startswith("AAY"):
					self.assertEqual(evidence["signatures"][0]["signer_name"], "Moraru Veronica")
					self.assertEqual(evidence["signatures"][0]["serial_number"], "0981103423263")
					self.assertIn("ARAX-IMPEX", evidence["signatures"][0]["organization"])
					self.assertEqual(evidence["signatures"][0]["reason"], "MoldSign Signature")
					self.assertEqual(evidence["signatures"][0]["location"], "Moldova")
					self.assertIn("12:30:56", evidence["signatures"][0]["time_display"])
					self.assertEqual(evidence["signatures"][0]["timestamp"], "Passed")
					self.assertEqual(verified["signature_timestamp_check"], "Passed")
				else:
					self.assertEqual(evidence["signatures"][0]["signer_name"], "Argint Dana")
					self.assertEqual(evidence["signatures"][0]["serial_number"], "0992210219627")
					self.assertIn("Orange Moldova", evidence["signatures"][0]["organization"])

	def test_blank_pdf_is_an_image_scan_and_signed_examples_are_not(self):
		from pypdf import PdfWriter

		buffer = io.BytesIO()
		writer = PdfWriter()
		writer.add_blank_page(width=100, height=100)
		writer.write(buffer)
		self.assertTrue(is_pdf_image_scan(buffer.getvalue()))
		self.assertFalse(is_pdf_image_scan(b"\xff\xd8\xffjpeg"))
		path = EXAMPLES / "AAY9977940.signed.pdf"
		if not path.exists():
			self.skipTest("Private example PDF is not available")
		self.assertFalse(is_pdf_image_scan(path.read_bytes()))

	def test_tampered_signed_bytes_are_invalid(self):
		path = EXAMPLES / "AAY9977940.signed.pdf"
		if not path.exists():
			self.skipTest("Private example PDF is not available")
		content = bytearray(path.read_bytes())
		content[50] ^= 0x01
		verified = verify_pdf_signatures(bytes(content))
		self.assertEqual(verified["signature_integrity"], "Failed")
		self.assertEqual(verified["signature_status"], "Invalid")
