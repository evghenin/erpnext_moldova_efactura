from pathlib import Path
from unittest import TestCase

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura import import_pdf
from erpnext_moldova_efactura.utils.factura_pdf import FacturaImportError, parse_pdf, parse_text
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
		self.assertNotIn("service_period_start", data)

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

	def test_actual_provider_pdfs(self):
		for filename, series, number, total, uom in (
			("AAY9977940.signed.pdf", "AAY", "9977940", "270.00", "GB"),
			("74085315_1_FiscalInvoice.pdf", "AAX", "8280597", "220.00", ""),
		):
			path = EXAMPLES / filename
			if not path.exists():
				self.skipTest("Private example PDFs are not available")
			with self.subTest(filename=filename):
				data = parse_pdf(path.read_bytes())
				self.assertEqual((data["series"], data["number"], data["total"]), (series, number, total))
				self.assertEqual(data["items"][0]["source_uom"], uom)
				self.assertEqual(data["signature_status"], "Not Checked")
				self.assertEqual(len(data["signature_details"]), 1)


class TestPurchaseFactura(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		frappe.db.set_value("Currency", "MDL", "enabled", 1)
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
				"supplier": self.supplier.name,
				"series": "PFT",
				"number": frappe.generate_hash(length=9),
				"issue_date": nowdate(),
				"currency": "MDL",
				"supplier_idno": "1002600041697",
				"buyer_idno": "1024600026571",
				"reviewed": 1,
				"items": [
					{
						"description": "Internet service",
						"source_qty": 1,
						"source_rate": 225,
						"vat_rate": 20,
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
			self.factura(series=pf.series, number=pf.number)
		pi.cancel()
		amended = frappe.copy_doc(pf)
		amended.docstatus = 0
		amended.amended_from = pf.name
		amended.purchase_invoice = None
		amended.insert()
		self.assertFalse(amended.reviewed)
		amended.reviewed = 1
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
		self.assertFalse(second_amendment.reviewed)

	def test_duplicate_and_cross_route_allocation_blocked(self):
		pf = self.factura(number="000001")
		with self.assertRaises(frappe.ValidationError):
			self.factura(number="000001")
		pi = make_purchase_invoice(pf.name).insert()
		with self.assertRaises(frappe.ValidationError):
			assert_no_pf_for_pi(pi.name)
		with self.assertRaises(frappe.ValidationError):
			assert_no_pf_for_pef(
				frappe._dict(
					company=pf.company,
					ef_supplier_idno=pf.supplier_idno,
					ef_series=pf.series,
					ef_number=pf.number,
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
		self.assertEqual(pi.reload().docstatus, 1)
		self.assertFalse(pi.purchase_factura)

	def test_review_and_company_checks(self):
		pf = self.factura(reviewed=0)
		with self.assertRaises(frappe.ValidationError):
			make_purchase_invoice(pf.name)
		with self.assertRaises(frappe.ValidationError):
			self.factura(buyer_idno="9999999999999")

	def test_import_is_repeat_safe_and_preserves_original(self):
		path = EXAMPLES / "AAY9977940.signed.pdf"
		if not path.exists():
			self.skipTest("Private example PDF is not available")
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
		self.assertEqual(pf.supplier, self.supplier.name)
		self.assertEqual(flt(pf.total), 270)
		self.assertEqual(pf.items[0].source_uom, "GB")
		self.assertEqual((pf.items[0].item_code, pf.items[0].uom), (self.item.name, "Nos"))
		with self.assertRaises(frappe.ValidationError):
			file.delete()
		pf.items[0].source_rate = 226
		with self.assertRaises(frappe.ValidationError):
			pf.save()

	def test_sales_user_cannot_create_pf(self):
		roles = {p.role for p in frappe.get_meta("Purchase Factura").permissions if p.create}
		self.assertNotIn("Sales User", roles)
		self.assertIn("Purchase User", roles)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			import_pdf("/private/files/unknown.pdf", self.company.name)
