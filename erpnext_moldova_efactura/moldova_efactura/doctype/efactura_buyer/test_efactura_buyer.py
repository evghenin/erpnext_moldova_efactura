# Copyright (c) 2026, Evgheni Nemerenco and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_moldova_efactura.utils.api_response import extract_invoices, invoice_xml
from erpnext_moldova_efactura.utils.buyer_status import compose_buyer_status, status_label
from erpnext_moldova_efactura.utils.invoice_xml import parse_invoice_xml
from erpnext_moldova_efactura.utils.item_map import resolve_item_code
from erpnext_moldova_efactura.utils.uom_map import (
	clear_uom_alias_cache,
	compute_buyer_item_qtys,
	resolve_uom,
)
SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Document>
  <SupplierInfo>
    <Seria>EBJ</Seria>
    <Number>000066606</Number>
    <IssuedDate>2026-06-09T10:38:41</IssuedDate>
    <DeliveryDate>2026-06-09T10:38:01</DeliveryDate>
    <Supplier IDNO="1015608001255" Title="PRIM-LOGIST SRL" Address="Chisinau" CodTVA="0405427" TaxpayerType="1">
      <BankAccount BranchTitle="Bank" BranchCode="FTMD" Account="MD16FT2224"/>
    </Supplier>
    <Buyer IDNO="1024600026571" Title="HOTEL LIFE SRL" Address="Codru" CodTVA="0211775" TaxpayerType="1">
      <BankAccount BranchTitle="" BranchCode="" Account=""/>
    </Buyer>
    <Total>118.00</Total>
    <TotalTVA>18.00</TotalTVA>
    <Merchandises>
      <Row Code="A1" Name="Item A" UnitOfMeasure="buc" Quantity="2"
           UnitPriceWithoutTVA="50" TotalPriceWithoutTVA="100" TVA="18" TotalTVA="18" TotalPrice="118"/>
    </Merchandises>
  </SupplierInfo>
</Document>
"""


class TestEFacturaBuyerUtils(FrappeTestCase):
	def test_status_label(self):
		self.assertEqual(status_label(7), "Sent to Buyer")
		self.assertEqual(status_label(1), "Signed by Supplier")
		self.assertEqual(status_label(8), "Signed by Buyer")
		self.assertEqual(status_label(2), "Rejected")
		self.assertEqual(status_label(0), "")
		self.assertEqual(compose_buyer_status(8), "Signed by Buyer")
		self.assertEqual(compose_buyer_status(8, "PINV-001"), "Signed by Buyer · Linked to PI")
		self.assertEqual(compose_buyer_status(7, "PINV-001"), "Sent to Buyer · Linked to PI")

	def test_extract_xml_invoice(self):
		resp = {
			"Results": {
				"XmlInvoice": {
					"Seria": "EBJ",
					"Number": "000066606",
					"InvoiceStatus": 8,
					"Xml": SAMPLE_XML,
				}
			}
		}
		invs = extract_invoices(resp)
		self.assertEqual(len(invs), 1)
		self.assertTrue(invoice_xml(invs[0]).startswith("<?xml"))

	def test_parse_invoice_xml(self):
		parsed = parse_invoice_xml(SAMPLE_XML)
		self.assertEqual(parsed["ef_series"], "EBJ")
		self.assertEqual(parsed["ef_number"], "000066606")
		self.assertEqual(parsed["supplier"]["idno"], "1015608001255")
		self.assertEqual(len(parsed["items"]), 1)
		self.assertEqual(parsed["items"][0]["supplier_item_code"], "A1")
		self.assertEqual(parsed["items"][0]["supplier_uom"], "buc")
		self.assertEqual(parsed["items"][0]["ef_qty"], 2)
		self.assertEqual(parsed["vat_total"], 18)


class TestEFacturaBuyerUOM(FrappeTestCase):
	def tearDown(self):
		clear_uom_alias_cache()

	def test_resolve_uom_by_print_name_and_translation(self):
		uom = frappe.db.get_value("UOM", {"name": "Nos"}, "name") or frappe.db.get_value("UOM", {}, "name")
		if not uom:
			self.skipTest("No UOM")

		frappe.db.set_value("UOM", uom, "print_name", "buc-test-alias")
		clear_uom_alias_cache()
		self.assertEqual(resolve_uom("buc-test-alias"), uom)

		if not frappe.db.exists("Translation", {"language": "ro", "source_text": uom}):
			frappe.get_doc(
				{
					"doctype": "Translation",
					"language": "ro",
					"source_text": uom,
					"translated_text": "bucată-test",
				}
			).insert(ignore_permissions=True)
		else:
			frappe.db.set_value(
				"Translation",
				{"language": "ro", "source_text": uom},
				"translated_text",
				"bucată-test",
			)
		clear_uom_alias_cache()
		self.assertEqual(resolve_uom("bucată-test"), uom)

	def test_resolve_item_direct_match(self):
		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "item_name"], as_dict=True)
		if not item:
			self.skipTest("No Item")
		# code only (no name to verify) — accepted
		self.assertEqual(resolve_item_code(None, item.name), item.name)
		# code + matching name — accepted
		if item.item_name:
			self.assertEqual(resolve_item_code(None, item.name, item.item_name), item.name)
			# code hit with wrong name — discarded, then name search may still find it
			self.assertEqual(
				resolve_item_code(None, item.name, "___other product title___"),
				None,
			)
		self.assertIsNone(resolve_item_code(None, "___no_such_item_code___"))

	def test_resolve_item_by_name_when_code_misses(self):
		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "item_name"], as_dict=True)
		if not item or not item.item_name:
			self.skipTest("No Item with item_name")
		# random supplier code should not match; stable name should
		self.assertEqual(
			resolve_item_code(None, "RND-CODE-999", item.item_name),
			item.name,
		)

	def test_compute_stock_qty_from_item_uom_conversion(self):
		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		if not item:
			self.skipTest("No Item")

		# same UOM → 1:1
		same = compute_buyer_item_qtys(item.name, item.stock_uom, 2, item.stock_uom)
		self.assertEqual(same["stock_uom"], item.stock_uom)
		self.assertEqual(same["stock_qty"], 2)
		self.assertEqual(same["qty"], 2)

		# optional non-stock UOM on item
		extra = frappe.db.get_value(
			"UOM Conversion Detail",
			{"parent": item.name, "uom": ["!=", item.stock_uom]},
			["uom", "conversion_factor"],
			as_dict=True,
		)
		if not extra:
			self.skipTest("No extra UOM conversion on item")

		out = compute_buyer_item_qtys(item.name, extra.uom, 2, item.stock_uom)
		self.assertEqual(out["stock_qty"], 2 * float(extra.conversion_factor))
		self.assertEqual(out["qty"], out["stock_qty"])  # PI uom = stock


class TestEFacturaBuyerDoc(FrappeTestCase):
	def setUp(self):
		self.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		if not self.company:
			self.skipTest("No company")

	def test_make_purchase_invoice_uses_vat_inclusive_rate(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer import (
			make_purchase_invoice,
		)

		name = frappe.db.exists(
			"eFactura Buyer",
			{"company": self.company, "ef_series": "EBJ", "ef_number": "000066606"},
		)
		if name:
			doc = frappe.get_doc("eFactura Buyer", name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("eFactura Buyer", name, force=1)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		prev = frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate")
		try:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 1)
			doc = frappe.get_doc(
				{
					"doctype": "eFactura Buyer",
					"naming_series": "EFB-.YYYY.-",
					"company": self.company,
					"ef_series": "EBJ",
					"ef_number": "000066606",
					"ef_status": 8,
					"supplier": sup,
				}
			)
			doc.fill_from_xml(SAMPLE_XML)
			doc.items[0].item_code = item.name
			doc.items[0].ef_uom = item.stock_uom
			doc.items[0].uom = item.stock_uom
			doc.insert()
			doc.submit()

			pi = make_purchase_invoice(doc.name)
			# SAMPLE_XML: rate=50, rate_with_vat=59 (118/2), qty=2
			self.assertAlmostEqual(flt(pi.items[0].rate), 59.0, places=2)

			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)
			pi_net = make_purchase_invoice(doc.name)
			self.assertAlmostEqual(flt(pi_net.items[0].rate), 50.0, places=2)
		finally:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", prev)

	def test_fill_from_xml_and_unique(self):
		name = frappe.db.exists(
			"eFactura Buyer",
			{"company": self.company, "ef_series": "EBJ", "ef_number": "000066606"},
		)
		if name:
			frappe.delete_doc("eFactura Buyer", name, force=1)

		doc = frappe.get_doc(
			{
				"doctype": "eFactura Buyer",
				"naming_series": "EFB-.YYYY.-",
				"company": self.company,
				"ef_series": "EBJ",
				"ef_number": "000066606",
				"ef_status": 8,
			}
		)
		doc.fill_from_xml(SAMPLE_XML)
		doc.insert()
		self.assertEqual(doc.status, "Signed by Buyer")
		self.assertEqual(len(doc.items), 1)
		self.assertEqual(doc.ef_supplier_idno, "1015608001255")
		self.assertEqual(doc.items[0].supplier_uom, "buc")
		# unknown "buc": ef_uom and uom stay empty (no stock_uom fallback)
		self.assertFalse(doc.items[0].ef_uom)
		self.assertFalse(doc.items[0].uom)

		dup = frappe.get_doc(
			{
				"doctype": "eFactura Buyer",
				"naming_series": "EFB-.YYYY.-",
				"company": self.company,
				"ef_series": "EBJ",
				"ef_number": "000066606",
				"ef_status": 8,
			}
		)
		self.assertRaises(frappe.ValidationError, dup.insert)

		# cannot submit until items mapped
		self.assertRaises(frappe.ValidationError, doc.submit)

		item = frappe.db.get_value("Item", {"disabled": 0}, "name")
		if not item:
			self.skipTest("No Item")
		stock_uom = frappe.db.get_value("Item", item, "stock_uom")
		doc.reload()
		doc.items[0].item_code = item
		doc.items[0].ef_uom = stock_uom
		doc.items[0].uom = stock_uom
		if not doc.supplier:
			sup = frappe.db.get_value("Supplier", {}, "name")
			if not sup:
				self.skipTest("No Supplier")
			doc.supplier = sup
		doc.save()
		doc.reload()
		self.assertEqual(doc.items[0].supplier_uom, "buc")
		self.assertEqual(doc.items[0].ef_uom, stock_uom)
		self.assertEqual(doc.items[0].stock_uom, stock_uom)
		self.assertEqual(doc.items[0].stock_qty, doc.items[0].ef_qty)
		self.assertEqual(doc.items[0].uom, stock_uom)
		doc.submit()
		self.assertEqual(doc.docstatus, 1)
