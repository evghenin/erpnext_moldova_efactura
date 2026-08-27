# Copyright (c) 2025, Evgheni Nemerenco and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, flt

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

	def test_submit_requires_sales_invoice_for_transfer(self):
		doc = frappe.get_doc(
			{
				"doctype": "Sales eFactura",
				"type": "Transfer",
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
		doc.customer = "Cust"
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc._validate_ready_to_submit()
		self.assertIn("Sales Invoice is required before submit", str(ctx.exception))

	def test_submit_allows_non_transfer_without_sales_invoice(self):
		doc = frappe.get_doc(
			{
				"doctype": "Sales eFactura",
				"type": "Non-Transfer",
				"customer_party_type": "Customer",
				"customer_party": "Cust",
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
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc._validate_ready_to_submit()
		self.assertNotIn("Sales Invoice is required before submit", str(ctx.exception))
		self.assertNotIn("Purchase Receipt Return", str(ctx.exception))

	def test_expected_party_type_non_transfer_return(self):
		from types import SimpleNamespace

		from erpnext_moldova_efactura.utils.sef_mode import expected_party_type

		self.assertEqual(expected_party_type(SimpleNamespace(type="Transfer", is_return=0)), "Customer")
		self.assertEqual(expected_party_type(SimpleNamespace(type="Non-Transfer", is_return=0)), "Customer")
		self.assertEqual(expected_party_type(SimpleNamespace(type="Non-Transfer", is_return=1)), "Supplier")

	def test_submit_return_non_transfer_requires_pr(self):
		doc = frappe.get_doc(
			{
				"doctype": "Sales eFactura",
				"type": "Non-Transfer",
				"is_return": 1,
				"customer_party_type": "Supplier",
				"customer_party": "Supp",
				"issue_date": "2026-08-20",
				"delivery_date": "2026-08-20",
				"company_bank_account": "X",
				"items": [
					{
						"item_code": "SKU",
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
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc._validate_ready_to_submit()
		self.assertIn("Purchase Receipt Return", str(ctx.exception))

	def test_mark_as_return_sets_supplier_and_unmark_restores_customer(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
			mark_as_return,
			unmark_as_return,
		)
		from erpnext_moldova_efactura.utils.party import get_customer_idno_field, get_supplier_idno_field

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		cust = frappe.db.get_value("Customer", {"disabled": 0}, "name")
		sup = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
		company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		bank = frappe.db.get_value("Bank Account", {"company": company, "is_company_account": 1}, "name")
		if not item or not cust or not sup or not company or not bank:
			self.skipTest("Need Item, Customer, Supplier, Company Bank Account")
		idno = "1999999999999"
		cfield = get_customer_idno_field()
		sfield = get_supplier_idno_field()
		prev_c = frappe.db.get_value("Customer", cust, cfield) if cfield else None
		prev_s = frappe.db.get_value("Supplier", sup, sfield) if sfield else None
		name = frappe.db.exists("Sales eFactura", {"ef_series": "ZZ", "ef_number": "88001"})
		if name:
			frappe.delete_doc("Sales eFactura", name, force=1, ignore_permissions=True)
		try:
			if cfield:
				frappe.db.set_value("Customer", cust, cfield, idno, update_modified=False)
			if sfield:
				frappe.db.set_value("Supplier", sup, sfield, idno, update_modified=False)
			doc = frappe.get_doc(
				{
					"doctype": "Sales eFactura",
					"company": company,
					"company_bank_account": bank,
					"type": "Non-Transfer",
					"customer_party_type": "Customer",
					"customer_party": cust,
					"ef_series": "ZZ",
					"ef_number": "88001",
					"ef_customer_idno": idno,
					"ef_conversion_rate": 1,
					"items": [
						{
							"item_code": item.name,
							"item_name": "Widget",
							"qty": 1,
							"uom": item.stock_uom,
							"stock_uom": item.stock_uom,
							"ef_uom": item.stock_uom,
							"rate": 1,
						}
					],
				}
			)
			doc.flags.ignore_validate = True
			doc.insert(ignore_permissions=True, ignore_mandatory=True)
			mark_as_return(doc.name)
			doc.reload()
			self.assertEqual(cint(doc.is_return), 1)
			self.assertEqual(doc.customer_party_type, "Supplier")
			if sfield:
				self.assertEqual(doc.customer_party, sup)
			unmark_as_return(doc.name)
			doc.reload()
			self.assertEqual(cint(doc.is_return), 0)
			self.assertEqual(doc.customer_party_type, "Customer")
			if cfield:
				self.assertEqual(doc.customer_party, cust)
		finally:
			if cfield:
				frappe.db.set_value("Customer", cust, cfield, prev_c, update_modified=False)
			if sfield:
				frappe.db.set_value("Supplier", sup, sfield, prev_s, update_modified=False)
			name = frappe.db.exists("Sales eFactura", {"ef_series": "ZZ", "ef_number": "88001"})
			if name:
				frappe.delete_doc("Sales eFactura", name, force=1, ignore_permissions=True)

	def test_mark_as_return_blocked_when_pr_linked(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
			mark_as_return,
		)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		if not item or not company:
			self.skipTest("Need Item and Company")
		name = frappe.db.exists("Sales eFactura", {"ef_series": "ZZ", "ef_number": "88002"})
		if name:
			frappe.delete_doc("Sales eFactura", name, force=1, ignore_permissions=True)
		doc = frappe.get_doc(
			{
				"doctype": "Sales eFactura",
				"company": company,
				"type": "Non-Transfer",
				"ef_series": "ZZ",
				"ef_number": "88002",
				"items": [
					{
						"item_code": item.name,
						"item_name": "Widget",
						"qty": 1,
						"uom": item.stock_uom,
						"stock_uom": item.stock_uom,
						"ef_uom": item.stock_uom,
						"rate": 1,
						"purchase_receipt": "PR-DUMMY",
						"pr_detail": "PR-DUMMY-1",
					}
				],
			}
		)
		doc.flags.ignore_validate = True
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True, ignore_mandatory=True)
		try:
			with self.assertRaises(frappe.ValidationError):
				mark_as_return(doc.name)
		finally:
			frappe.delete_doc("Sales eFactura", doc.name, force=1, ignore_permissions=True)

	def test_regular_pr_fiscal_mirrors_sales_invoice_status(self):
		from types import SimpleNamespace
		from unittest.mock import patch

		from erpnext_moldova_efactura.utils.fiscal_status import determine_pr_fiscal_status

		pr = SimpleNamespace(name="PR-1", docstatus=1, is_return=0)
		with patch(
			"erpnext_moldova_efactura.utils.fiscal_status.sales_invoices_for_purchase_receipt",
			return_value=[],
		):
			self.assertEqual(determine_pr_fiscal_status(pr), "Pending")

		with (
			patch(
				"erpnext_moldova_efactura.utils.fiscal_status.sales_invoices_for_purchase_receipt",
				return_value=["SI-1"],
			),
			patch("frappe.db.get_value", return_value="Completed"),
		):
			self.assertEqual(determine_pr_fiscal_status(pr), "Completed")

		def mixed_status(dt, name, field=None, **kwargs):
			return {"SI-1": "Completed", "SI-2": "Failed"}.get(name, "")

		with (
			patch(
				"erpnext_moldova_efactura.utils.fiscal_status.sales_invoices_for_purchase_receipt",
				return_value=["SI-1", "SI-2"],
			),
			patch("frappe.db.get_value", side_effect=mixed_status),
		):
			self.assertEqual(determine_pr_fiscal_status(pr), "Failed")

	def test_make_sales_invoice_blocked_for_non_transfer(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
			_require_transfer_for_selling,
		)

		doc = frappe.get_doc({"doctype": "Sales eFactura", "type": "Non-Transfer"})
		with self.assertRaises(frappe.ValidationError) as ctx:
			_require_transfer_for_selling(doc)
		self.assertIn("not used for Non-Transfer", str(ctx.exception))

	def test_sales_invoice_cannot_change_after_submit(self):
		from unittest.mock import patch

		doc = frappe.get_doc({"doctype": "Sales eFactura", "sales_invoice": "SINV-NEW"})
		doc.docstatus = 1
		with (
			patch.object(doc, "is_new", return_value=False),
			patch.object(doc, "has_value_changed", return_value=True),
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc._validate_sales_invoice_locked_after_submit()
		self.assertIn("cannot be changed after submit", str(ctx.exception))

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
		territory = frappe.db.get_single_value("eFactura Settings", "fiscal_territory")
		if territory and frappe.db.exists("Territory", territory):
			self.assertEqual(defaults.get("territory"), territory)

	def test_is_sef_cancelable_status(self):
		from erpnext_moldova_efactura.utils.fiscal_status import is_sef_cancelable_status

		for code in (1, 2, 3, 7, 8, 9, 10):
			self.assertTrue(is_sef_cancelable_status(code), code)
		for label in (
			"Signed by Supplier",
			"Sent to Customer",
			"Accepted by Customer",
			"Signed by Customer",
			"Rejected by Customer",
			"Transportation",
		):
			self.assertTrue(is_sef_cancelable_status(label), label)
		for code in (-1, 0, 5, 6, 11, None, ""):
			self.assertFalse(is_sef_cancelable_status(code), code)
		for label in (
			"Pending Registration",
			"Registered as Draft",
			"Canceled by Supplier",
			"Archived",
			"Cancellation Requested",
		):
			self.assertFalse(is_sef_cancelable_status(label), label)

	def test_assert_can_cancel(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
			_assert_can_cancel,
		)

		ok = frappe._dict(
			docstatus=1,
			ef_series="AA",
			ef_number="1",
			ef_status="Sent to Customer",
		)
		_assert_can_cancel(ok)
		_assert_can_cancel(
			frappe._dict(docstatus=1, ef_series="AA", ef_number="1", ef_status="Rejected by Customer")
		)
		with self.assertRaises(frappe.ValidationError):
			_assert_can_cancel(frappe._dict(docstatus=0, ef_series="AA", ef_number="1", ef_status=1))
		with self.assertRaises(frappe.ValidationError):
			_assert_can_cancel(
				frappe._dict(docstatus=1, ef_series="", ef_number="", ef_status="Sent to Customer")
			)
		with self.assertRaises(frappe.ValidationError):
			_assert_can_cancel(
				frappe._dict(docstatus=1, ef_series="AA", ef_number="1", ef_status="Pending Registration")
			)
		with self.assertRaises(frappe.ValidationError):
			_assert_can_cancel(
				frappe._dict(docstatus=1, ef_series="AA", ef_number="1", ef_status="Registered as Draft")
			)

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
			filters={"docstatus": 1, "ef_status": "Pending Registration"},
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

	def test_process_signed_xml_uses_loaded_doc_company(self):
		from unittest.mock import MagicMock, patch

		from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura import sales_efactura as sef

		import base64

		content = base64.b64encode(b"<SupplierInfo/>").decode()
		signature = base64.b64encode(b"<ds:Signature/>").decode()
		doc = frappe._dict(name="ACC-SEF-1", company="Hotel Life")
		doc.db_set = MagicMock()
		doc.set_status = MagicMock()
		client = MagicMock()
		client.post_invoices.return_value = {"TotalInvoices": 1, "TotalInvoicesPosted": 1}

		with (
			patch.object(sef, "_get_sales_efactura", return_value=doc),
			patch.object(sef, "_assert_can_register_signed"),
			patch.object(sef, "EFacturaAPIClient") as api,
			patch.object(sef, "log_event"),
		):
			api.from_settings.return_value = client
			sef.process_signed_xml("ACC-SEF-1", signature, content)
			api.from_settings.assert_called_once_with(company="Hotel Life")

	def test_sef_workflow_status_maps_return_to_sfs_label(self):
		from types import SimpleNamespace

		from erpnext_moldova_efactura.utils.fiscal_status import sef_workflow_status

		self.assertEqual(
			sef_workflow_status(SimpleNamespace(ef_status="Signed by Customer", status="Return")),
			"Signed by Customer",
		)
		self.assertEqual(
			sef_workflow_status(SimpleNamespace(ef_status=8, status="Return")),
			"Signed by Customer",
		)
		self.assertEqual(
			sef_workflow_status(SimpleNamespace(status="Signed by Customer")),
			"Signed by Customer",
		)
		self.assertEqual(
			sef_workflow_status(SimpleNamespace(status="Draft", ef_status=-1)),
			"Pending Registration",
		)

	def test_migrate_from_v1_does_not_drop_live_customer_party(self):
		from types import SimpleNamespace
		from unittest.mock import patch

		from erpnext_moldova_efactura.patches.v2_0 import migrate_from_v1 as m

		dropped = []
		with (
			patch.object(m.frappe.db, "table_exists", return_value=True),
			patch.object(m.frappe.db, "exists", return_value=True),
			patch.object(m.frappe, "get_meta") as get_meta,
			patch.object(m.frappe.db, "has_column", return_value=True),
			patch.object(m.frappe.db, "sql_ddl", side_effect=lambda sql: dropped.append(sql)),
		):
			get_meta.return_value.fields = [
				SimpleNamespace(fieldname="customer_party"),
				SimpleNamespace(fieldname="customer_party_type"),
				SimpleNamespace(fieldname="sales_invoice"),
			]
			m._drop_legacy_sef_columns()

		self.assertFalse(any("customer_party`" in sql for sql in dropped))
		self.assertFalse(any("customer_party_type`" in sql for sql in dropped))
		self.assertTrue(any("supplier_party`" in sql for sql in dropped))

	def test_restore_sef_customer_party_from_linked_si(self):
		from erpnext_moldova_efactura.patches.v2_1.restore_sef_customer_party_from_si import execute

		si = frappe.db.get_value(
			"Sales Invoice", {"docstatus": ["<", 2]}, ["name", "customer"], as_dict=True
		)
		if not si or not si.customer:
			self.skipTest("Need a Sales Invoice with a customer")

		names = []
		try:
			def insert_sef(**kwargs):
				doc = frappe.get_doc(
					{
						"doctype": "Sales eFactura",
						"items": [{"item_name": "X", "qty": 1, "rate": 1}],
						**kwargs,
					}
				)
				doc.flags.ignore_validate = True
				doc.insert(ignore_permissions=True, ignore_mandatory=True)
				names.append(doc.name)
				frappe.db.set_value(
					"Sales eFactura", doc.name, "customer_party", "", update_modified=False
				)
				return doc.name

			transfer = insert_sef(
				type="Transfer",
				sales_invoice=si.name,
				customer_party_type="Customer",
				items=[{"item_name": "X", "qty": 1, "rate": 1, "sales_invoice": si.name}],
			)
			non_transfer = insert_sef(
				type="Non-Transfer",
				sales_invoice=si.name,
				customer_party_type="Supplier",
			)
			item_only = insert_sef(
				type="Transfer",
				customer_party_type="Customer",
				items=[{"item_name": "X", "qty": 1, "rate": 1, "sales_invoice": si.name}],
			)

			execute()
			self.assertEqual(
				frappe.db.get_value("Sales eFactura", transfer, "customer_party"), si.customer
			)
			self.assertFalse(frappe.db.get_value("Sales eFactura", non_transfer, "customer_party"))
			self.assertEqual(
				frappe.db.get_value("Sales eFactura", item_only, "customer_party"), si.customer
			)
		finally:
			for name in names:
				frappe.delete_doc("Sales eFactura", name, force=1, ignore_permissions=True)

