from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

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
		self.assertEqual(data["supplier_name"], "ARAX-IMPEX SRL")
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
				"reviewed": 1,
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

	def test_review_and_company_checks(self):
		pf = self.factura(reviewed=0)
		with self.assertRaises(frappe.ValidationError):
			make_purchase_invoice(pf.name)
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
			"f_supplier_details",
			"f_supplier_idno",
			"f_supplier_name",
			"f_supplier_vat_id",
			"f_supplier_taxpayer_type",
			"f_supplier_address",
			"f_supplier_bank_account",
			"f_supplier_bank_name",
			"f_supplier_bank_code",
			"f_customer_details",
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
		self.assertFalse(pf.reviewed)

	def test_rate_refresh_missing_rate_and_review_reset(self):
		pf = self.factura(currency="EUR", f_currency="MDL", f_conversion_rate=20)
		pf.f_conversion_rate = 25
		pf.save()
		self.assertEqual(pf.total, 10.8)
		self.assertFalse(pf.reviewed)
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
