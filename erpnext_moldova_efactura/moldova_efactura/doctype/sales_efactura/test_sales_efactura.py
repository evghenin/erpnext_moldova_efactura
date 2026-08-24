# Copyright (c) 2025, Evgheni Nemerenco and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_moldova_efactura.utils.party import new_customer_defaults


class TestSaleseFactura(FrappeTestCase):
	def test_apply_vat_zero_rate_includes_line_in_totals(self):
		doc = frappe.get_doc(
			{
				"doctype": "Sales eFactura",
				"ef_conversion_rate": 1,
				"items": [
					{
						"item_code": "SKU00508",
						"item_name": "Set dentar ECO",
						"qty": 396,
						"rate": 6.8,
						"uom": "Nos",
						"ef_uom": "Nos",
					}
				],
			}
		)
		doc.apply_vat()
		row = doc.items[0]
		self.assertEqual(flt(row.amount, 2), 2692.8)
		self.assertEqual(flt(row.net_amount, 2), 2692.8)
		self.assertEqual(flt(row.vat_amount, 2), 0)
		self.assertEqual(flt(doc.net_total, 2), 2692.8)
		self.assertEqual(flt(doc.vat_total, 2), 0)
		self.assertEqual(flt(doc.total, 2), 2692.8)
		self.assertEqual(flt(doc.ef_net_total, 2), 2692.8)
		self.assertEqual(flt(doc.ef_vat_total, 2), 0)
		self.assertEqual(flt(doc.ef_total, 2), 2692.8)

	def test_sync_sales_invoice_links_copies_header_to_rows(self):
		from erpnext_moldova_efactura.utils.si_link import sync_sales_invoice_links

		doc = frappe.get_doc(
			{
				"doctype": "Sales eFactura",
				"sales_invoice": "SINV-0001",
				"items": [{"item_name": "A", "qty": 1, "rate": 1}],
			}
		)
		sync_sales_invoice_links(doc)
		self.assertEqual(doc.items[0].sales_invoice, "SINV-0001")

	def test_sync_sales_invoice_links_rejects_mixed_invoices(self):
		from erpnext_moldova_efactura.utils.si_link import sync_sales_invoice_links

		doc = frappe.get_doc(
			{
				"doctype": "Sales eFactura",
				"items": [
					{"item_name": "A", "qty": 1, "rate": 1, "sales_invoice": "SINV-1"},
					{"item_name": "B", "qty": 1, "rate": 1, "sales_invoice": "SINV-2"},
				],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			sync_sales_invoice_links(doc)

	def test_fill_from_xml_uses_creation_motiv_and_line_vat(self):
		xml = """<?xml version="1.0" encoding="UTF-8"?>
<Document>
  <SupplierInfo>
    <Seria>AA</Seria>
    <Number>0001</Number>
    <IssuedDate>2026-08-20T10:00:00</IssuedDate>
    <DeliveryDate>2026-08-20T10:00:00</DeliveryDate>
    <Supplier IDNO="1000000000001" Title="Seller" Address="Chisinau" CodTVA="123" TaxpayerType="1"/>
    <Buyer IDNO="1000000000002" Title="Buyer" Address="Balti" CodTVA="456" TaxpayerType="1"/>
    <Total>118.00</Total>
    <TotalTVA>18.00</TotalTVA>
    <Merchandises>
      <Row Code="X1" Name="Widget" UnitOfMeasure="buc" Quantity="1"
           UnitPriceWithoutTVA="100" TotalPriceWithoutTVA="100" TVA="18" TotalTVA="18" TotalPrice="118"/>
    </Merchandises>
    <CreationMotiv>5</CreationMotiv>
  </SupplierInfo>
</Document>
"""
		prev = frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate")
		try:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)
			doc = frappe.get_doc({"doctype": "Sales eFactura", "ef_conversion_rate": 1})
			doc.fill_from_xml(xml)
			self.assertEqual(doc.type, "Non-Transfer")
			self.assertEqual(doc.ef_series, "AA")
			self.assertEqual(doc.ef_number, "0001")
			self.assertEqual(len(doc.items), 1)
			self.assertEqual(doc.items[0].ef_item_code, "X1")
			self.assertEqual(flt(doc.items[0].ef_vat_rate), 18)
			self.assertEqual(flt(doc.items[0].vat_amount, 2), 18)
			self.assertEqual(flt(doc.items[0].rate, 2), 100)
			self.assertEqual(flt(doc.items[0].amount, 2), 100)
			self.assertEqual(flt(doc.vat_total, 2), 18)
			self.assertEqual(flt(doc.ef_vat_total, 2), 18)
			self.assertEqual(flt(doc.ef_total, 2), 118)

			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 1)
			doc.apply_vat()
			self.assertEqual(flt(doc.items[0].rate, 2), 118)
			self.assertEqual(flt(doc.items[0].amount, 2), 118)
			self.assertEqual(flt(doc.items[0].vat_amount, 2), 18)
		finally:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", prev)

	def test_fill_from_xml_keeps_vat_when_tva_attr_missing(self):
		xml = """<?xml version="1.0" encoding="UTF-8"?>
<Document>
  <SupplierInfo>
    <Seria>AA</Seria>
    <Number>0002</Number>
    <IssuedDate>2026-08-20T10:00:00</IssuedDate>
    <DeliveryDate>2026-08-20T10:00:00</DeliveryDate>
    <Supplier IDNO="1000000000001" Title="Seller" Address="Chisinau" CodTVA="123" TaxpayerType="1"/>
    <Buyer IDNO="1000000000002" Title="Buyer" Address="Balti" CodTVA="456" TaxpayerType="1"/>
    <Total>118.00</Total>
    <TotalTVA>18.00</TotalTVA>
    <Merchandises>
      <Row Code="X1" Name="Widget" UnitOfMeasure="buc" Quantity="1"
           UnitPriceWithoutTVA="100" TotalPriceWithoutTVA="100" TotalTVA="18" TotalPrice="118"/>
    </Merchandises>
  </SupplierInfo>
</Document>
"""
		doc = frappe.get_doc({"doctype": "Sales eFactura", "ef_conversion_rate": 1, "ef_status": 8})
		doc.fill_from_xml(xml)
		self.assertEqual(flt(doc.items[0].ef_vat_rate), 18)
		self.assertEqual(flt(doc.items[0].vat_amount, 2), 18)
		self.assertEqual(flt(doc.items[0].ef_vat_amount, 2), 18)
		self.assertEqual(flt(doc.vat_total, 2), 18)
		self.assertEqual(flt(doc.total, 2), 118)
		doc.apply_vat()
		self.assertEqual(flt(doc.items[0].vat_amount, 2), 18)
		self.assertEqual(flt(doc.vat_total, 2), 18)

	def test_fill_from_xml_assigns_item_tax_template_by_vat_rate(self):
		from erpnext_moldova_efactura.utils.item_tax_template import item_tax_template_for_vat_rate

		company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		account = frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 0, "account_type": "Tax"},
			"name",
		) if company else None
		if not company or not account:
			self.skipTest("Need Company and Tax Account")

		tpl_name = frappe.db.get_value(
			"Item Tax Template Detail",
			{"tax_rate": 18, "parenttype": "Item Tax Template"},
			"parent",
		)
		created = None
		if not tpl_name:
			created = frappe.get_doc(
				{
					"doctype": "Item Tax Template",
					"title": "eFactura Test VAT 18",
					"company": company,
					"taxes": [{"tax_type": account, "tax_rate": 18}],
				}
			)
			created.insert(ignore_permissions=True)
			tpl_name = created.name

		settings = frappe.get_single("eFactura Settings")
		prev_rows = [r.as_dict() for r in (settings.outgoing_item_tax_templates or [])]
		try:
			settings.set("outgoing_item_tax_templates", [])
			settings.append("outgoing_item_tax_templates", {"item_tax_template": tpl_name})
			settings.flags.ignore_permissions = True
			settings.save()

			self.assertEqual(item_tax_template_for_vat_rate(18, company), tpl_name)
			self.assertFalse(item_tax_template_for_vat_rate(20, company))

			xml = """<?xml version="1.0" encoding="UTF-8"?>
<Document>
  <SupplierInfo>
    <Seria>AA</Seria>
    <Number>0003</Number>
    <IssuedDate>2026-08-20T10:00:00</IssuedDate>
    <DeliveryDate>2026-08-20T10:00:00</DeliveryDate>
    <Supplier IDNO="1000000000001" Title="Seller" Address="Chisinau" CodTVA="123" TaxpayerType="1"/>
    <Buyer IDNO="1000000000002" Title="Buyer" Address="Balti" CodTVA="456" TaxpayerType="1"/>
    <Total>118.00</Total>
    <TotalTVA>18.00</TotalTVA>
    <Merchandises>
      <Row Code="X1" Name="Widget" UnitOfMeasure="buc" Quantity="1"
           UnitPriceWithoutTVA="100" TotalPriceWithoutTVA="100" TVA="18" TotalTVA="18" TotalPrice="118"/>
    </Merchandises>
  </SupplierInfo>
</Document>
"""
			doc = frappe.get_doc(
				{"doctype": "Sales eFactura", "company": company, "ef_conversion_rate": 1, "ef_status": 8}
			)
			doc.fill_from_xml(xml)
			self.assertEqual(doc.items[0].item_tax_template, tpl_name)
		finally:
			settings = frappe.get_single("eFactura Settings")
			settings.set("outgoing_item_tax_templates", [])
			for row in prev_rows:
				settings.append("outgoing_item_tax_templates", row)
			settings.flags.ignore_permissions = True
			settings.save()
			if created:
				frappe.delete_doc("Item Tax Template", created.name, force=1)

	def test_sales_invoice_requires_customer(self):
		doc = frappe.get_doc({"doctype": "Sales eFactura", "sales_invoice": "SINV-0001"})
		with self.assertRaises(frappe.ValidationError):
			doc._validate_sales_invoice_customer()

	def test_submit_blocked_until_customer_and_items_mapped(self):
		doc = frappe.get_doc(
			{
				"doctype": "Sales eFactura",
				"issue_date": "2026-08-20",
				"delivery_date": "2026-08-20",
				"company_bank_account": "X",
				"items": [
					{
						"item_name": "Widget",
						"qty": 1,
						"uom": "Nos",
						"stock_uom": "Nos",
						"ef_uom": "Nos",
						"rate": 1,
					}
				],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_ready_to_submit()
		doc.customer = "Cust"
		with self.assertRaises(frappe.ValidationError):
			doc._validate_ready_to_submit()

	def test_require_mapped_needs_customer_and_item_code(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
			_require_mapped,
		)

		doc = frappe.get_doc(
			{
				"doctype": "Sales eFactura",
				"items": [
					{
						"item_name": "Widget",
						"qty": 1,
						"uom": "Nos",
						"rate": 1,
					}
				],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			_require_mapped(doc, "Sales Invoice")
		doc.customer = "Cust"
		with self.assertRaises(frappe.ValidationError):
			_require_mapped(doc, "Sales Invoice")

	def test_make_sales_invoice_follows_vat_included_setting(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
			make_sales_invoice,
			make_sales_order,
		)

		company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		customer = frappe.db.get_value("Customer", {"disabled": 0}, "name")
		bank = frappe.db.get_value("Bank Account", {"company": company, "is_company_account": 1}, "name")
		if not company or not item or not customer or not bank:
			self.skipTest("Need Company, Item, Customer, and Company Bank Account")

		name = frappe.db.exists(
			"Sales eFactura",
			{"company": company, "ef_series": "AA", "ef_number": "99001"},
		)
		if name:
			doc = frappe.get_doc("Sales eFactura", name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Sales eFactura", name, force=1)

		xml = """<?xml version="1.0" encoding="UTF-8"?>
<Document>
  <SupplierInfo>
    <Seria>AA</Seria>
    <Number>99001</Number>
    <IssuedDate>2026-08-20T10:00:00</IssuedDate>
    <DeliveryDate>2026-08-20T10:00:00</DeliveryDate>
    <Supplier IDNO="1000000000001" Title="Seller" Address="Chisinau" CodTVA="123" TaxpayerType="1"/>
    <Buyer IDNO="1000000000002" Title="Buyer" Address="Balti" CodTVA="456" TaxpayerType="1"/>
    <Total>118.00</Total>
    <TotalTVA>18.00</TotalTVA>
    <Merchandises>
      <Row Code="X1" Name="Widget" UnitOfMeasure="buc" Quantity="1"
           UnitPriceWithoutTVA="100" TotalPriceWithoutTVA="100" TVA="18" TotalTVA="18" TotalPrice="118"/>
    </Merchandises>
  </SupplierInfo>
</Document>
"""
		prev = frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate")
		try:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)
			doc = frappe.get_doc(
				{
					"doctype": "Sales eFactura",
					"company": company,
					"customer": customer,
					"company_bank_account": bank,
					"ef_conversion_rate": 1,
					"ef_status": 8,
				}
			)
			doc.fill_from_xml(xml)
			doc.ef_customer_idno = None
			doc.customer = customer
			doc.items[0].item_code = item.name
			doc.items[0].uom = item.stock_uom
			doc.items[0].ef_uom = item.stock_uom
			doc.items[0].stock_uom = item.stock_uom
			doc.insert()

			si = make_sales_invoice(doc.name)
			self.assertEqual(si.customer, customer)
			self.assertAlmostEqual(flt(si.items[0].rate), 100.0, places=2)
			self.assertTrue(si.get_onload("load_after_mapping"))
			if si.meta.has_field("sales_efactura"):
				self.assertEqual(si.sales_efactura, doc.name)

			so = make_sales_order(doc.name)
			self.assertEqual(so.customer, customer)
			self.assertAlmostEqual(flt(so.items[0].rate), 100.0, places=2)

			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 1)
			si_inc = make_sales_invoice(doc.name)
			self.assertAlmostEqual(flt(si_inc.items[0].rate), 118.0, places=2)

			doc.sales_invoice = "SINV-DUMMY"
			doc.db_set("sales_invoice", "SINV-DUMMY", update_modified=False)
			doc.reload()
			with self.assertRaises(frappe.ValidationError):
				make_sales_invoice(doc.name)
		finally:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", prev)

	def test_new_customer_defaults_title_and_idno(self):
		field = frappe.db.get_single_value("eFactura Settings", "customer_idno_field")
		if not field or not frappe.get_meta("Customer").has_field(field):
			self.skipTest("Customer IDNO field is not configured")
		defaults = new_customer_defaults('S.R.L. "HOTEL LIFE"', "1024600026571", "Company")
		self.assertEqual(defaults.get("customer_name"), "HOTEL LIFE SRL")
		self.assertEqual(defaults.get(field), "1024600026571")
		self.assertEqual(defaults.get("customer_type"), "Company")

	def test_signable_skip_reason(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
			_signable_skip_reason,
		)

		self.assertIsNone(_signable_skip_reason(frappe._dict(docstatus=1, ef_status=-1)))
		self.assertEqual(_signable_skip_reason(None), frappe._("Not found"))
		self.assertEqual(
			_signable_skip_reason(frappe._dict(docstatus=0, ef_status=-1)),
			frappe._("Not submitted"),
		)
		self.assertEqual(
			_signable_skip_reason(frappe._dict(docstatus=1, ef_status=0)),
			frappe._("Not in Pending Registration"),
		)

	def test_filter_signable_missing_and_empty(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
			filter_signable,
		)

		empty = filter_signable([])
		self.assertEqual(empty["signable"], [])
		self.assertEqual(empty["skipped"], [])

		missing = filter_signable(["SEF-DOES-NOT-EXIST", "SEF-DOES-NOT-EXIST"])
		self.assertEqual(missing["signable"], [])
		self.assertEqual(len(missing["skipped"]), 1)
		self.assertEqual(missing["skipped"][0]["name"], "SEF-DOES-NOT-EXIST")
		self.assertEqual(missing["skipped"][0]["reason"], frappe._("Not found"))

		pending = frappe.get_all(
			"Sales eFactura",
			filters={"docstatus": 1, "ef_status": -1},
			fields=["name", "status"],
			limit=1,
		)
		if pending:
			mixed = filter_signable([pending[0].name, "SEF-DOES-NOT-EXIST"])
			self.assertEqual([row["name"] for row in mixed["signable"]], [pending[0].name])
			self.assertIn("status", mixed["signable"][0])
			self.assertEqual(mixed["skipped"][0]["name"], "SEF-DOES-NOT-EXIST")

	def test_assert_can_register_signed(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
			_assert_can_register_signed,
		)

		_assert_can_register_signed(frappe._dict(docstatus=1, ef_status=-1))
		with self.assertRaises(frappe.ValidationError):
			_assert_can_register_signed(frappe._dict(docstatus=0, ef_status=-1))
		with self.assertRaises(frappe.ValidationError):
			_assert_can_register_signed(frappe._dict(docstatus=1, ef_status=1))

	def test_supplier_bank_comes_from_company_bank_account(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
			_ensure_supplier_bank_details,
			_party_bank_link_field,
		)

		self.assertEqual(_party_bank_link_field("supplier"), "company_bank_account")
		self.assertEqual(_party_bank_link_field("customer"), "customer_bank_account")

		company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		bank = frappe.db.get_value("Bank Account", {"company": company, "is_company_account": 1}, "name")
		if not company or not bank:
			self.skipTest("Need Company and Company Bank Account")
		iban = frappe.db.get_value("Bank Account", bank, "iban")
		account_no = frappe.db.get_value("Bank Account", bank, "bank_account_no")
		account = (iban or account_no or "").strip()
		if not account:
			self.skipTest("Company Bank Account has no IBAN")

		doc = frappe.get_doc(
			{
				"doctype": "Sales eFactura",
				"company": company,
				"company_bank_account": bank,
			}
		)
		_ensure_supplier_bank_details(doc)
		self.assertEqual(doc.ef_supplier_bank_account, account)

		doc.ef_supplier_bank_account = "KEEP-ME"
		_ensure_supplier_bank_details(doc)
		self.assertEqual(doc.ef_supplier_bank_account, "KEEP-ME")
