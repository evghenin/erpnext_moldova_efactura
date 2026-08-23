# Copyright (c) 2026, Evgheni Nemerenco and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, flt, get_time, getdate, now_datetime

from erpnext_moldova_efactura.utils.api_response import extract_invoices, invoice_xml
from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import _sfs_action_error
from erpnext_moldova_efactura.utils.buyer_status import (
	compose_buyer_status,
	should_create_incoming,
	status_label,
)
from erpnext_moldova_efactura.utils.invoice_xml import parse_invoice_xml, unescape_sfs_text
from erpnext_moldova_efactura.utils.item_map import resolve_item_code
from erpnext_moldova_efactura.utils.party import (
	normalize_idno,
	normalize_supplier_title,
	new_supplier_defaults,
	throw_if_supplier_idno_mismatch,
)
from erpnext_moldova_efactura.utils.taxpayer_type import taxpayer_type_from_sfs, taxpayer_type_to_sfs
from erpnext_moldova_efactura.utils.pef_currency import (
	apply_document_amounts_from_ef,
	default_document_currency,
	remap_xml_item_money,
)
from erpnext_moldova_efactura.utils.uom_map import (
	apply_qty_defaults,
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
		self.assertEqual(status_label(4), "Signing")
		self.assertEqual(status_label(6), "Archived")
		self.assertEqual(status_label(0), "")
		self.assertEqual(compose_buyer_status(8), "Signed by Buyer")
		self.assertEqual(compose_buyer_status(8, "PINV-001"), "Signed by Buyer")
		self.assertEqual(compose_buyer_status(7, "PINV-001"), "Sent to Buyer")

	def test_should_create_incoming_respects_cancelled_setting(self):
		prev = frappe.db.get_single_value("eFactura Settings", "do_not_create_cancelled_invoices")
		try:
			frappe.db.set_single_value("eFactura Settings", "do_not_create_cancelled_invoices", 1)
			self.assertFalse(should_create_incoming(5))
			self.assertTrue(should_create_incoming(8))
			self.assertTrue(should_create_incoming(7))
			self.assertTrue(should_create_incoming(11))
			frappe.db.set_single_value("eFactura Settings", "do_not_create_cancelled_invoices", 0)
			self.assertTrue(should_create_incoming(5))
		finally:
			frappe.db.set_single_value("eFactura Settings", "do_not_create_cancelled_invoices", prev)

	def test_search_statuses_skip_archived_by_default(self):
		from erpnext_moldova_efactura.tasks.supplier_sync import supplier_search_statuses
		from erpnext_moldova_efactura.utils.buyer_status import buyer_search_statuses

		prev_in = frappe.db.get_single_value("eFactura Settings", "load_archived_purchase_efactura")
		prev_out = frappe.db.get_single_value("eFactura Settings", "load_archived_sales_efactura")
		try:
			frappe.db.set_single_value("eFactura Settings", "load_archived_purchase_efactura", 0)
			frappe.db.set_single_value("eFactura Settings", "load_archived_sales_efactura", 0)
			self.assertNotIn(6, buyer_search_statuses())
			self.assertNotIn(6, supplier_search_statuses())
			frappe.db.set_single_value("eFactura Settings", "load_archived_purchase_efactura", 1)
			frappe.db.set_single_value("eFactura Settings", "load_archived_sales_efactura", 1)
			self.assertIn(6, buyer_search_statuses())
			self.assertIn(6, supplier_search_statuses())
			self.assertNotIn(6, buyer_search_statuses()[:-1])
		finally:
			frappe.db.set_single_value("eFactura Settings", "load_archived_purchase_efactura", prev_in or 0)
			frappe.db.set_single_value("eFactura Settings", "load_archived_sales_efactura", prev_out or 0)

	def test_pef_bulk_eligibility(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			_pef_acceptable_skip_reason,
			_pef_signable_skip_reason,
			filter_acceptable,
			filter_signable,
		)

		self.assertIsNone(_pef_signable_skip_reason(frappe._dict(docstatus=1, ef_status=7)))
		self.assertIsNone(_pef_signable_skip_reason(frappe._dict(docstatus=1, ef_status=3)))
		self.assertEqual(
			_pef_signable_skip_reason(frappe._dict(docstatus=0, ef_status=7)),
			frappe._("Not submitted"),
		)
		self.assertEqual(
			_pef_signable_skip_reason(frappe._dict(docstatus=1, ef_status=8)),
			frappe._("Not eligible for signing"),
		)
		self.assertIsNone(_pef_acceptable_skip_reason(frappe._dict(docstatus=1, ef_status=1)))
		self.assertEqual(
			_pef_acceptable_skip_reason(frappe._dict(docstatus=1, ef_status=3)),
			frappe._("Not eligible for accepting"),
		)

		missing = filter_signable(["PEF-DOES-NOT-EXIST", "PEF-DOES-NOT-EXIST"])
		self.assertEqual(missing["signable"], [])
		self.assertEqual(len(missing["skipped"]), 1)
		self.assertEqual(missing["skipped"][0]["reason"], frappe._("Not found"))
		self.assertEqual(filter_acceptable([])["acceptable"], [])

	def test_normalize_idno(self):
		self.assertEqual(normalize_idno("1015 608 001 255"), "1015608001255")
		self.assertEqual(normalize_idno("1015608001255"), "1015608001255")
		self.assertEqual(normalize_idno(None), "")
		self.assertEqual(normalize_idno("IDNO-1015608001255"), "1015608001255")

	def test_throw_if_supplier_idno_mismatch_noop_without_supplier(self):
		throw_if_supplier_idno_mismatch(None, "1015608001255")
		throw_if_supplier_idno_mismatch("X", None)
		throw_if_supplier_idno_mismatch("X", "")

	def test_throw_unallocated_items_lists_rows(self):
		from erpnext_moldova_efactura.utils.pi_alloc import throw_unallocated_items

		items = [
			frappe._dict(
				idx=1,
				supplier_item_name="Item A",
				item_code="A",
				item_name="Item A",
				supplier_item_code="A1",
				ef_qty=2,
				qty=2,
				rate=50,
				purchase_invoice=None,
			),
			frappe._dict(
				idx=2,
				supplier_item_name="Item B",
				item_code="B",
				item_name="Item B",
				supplier_item_code="B1",
				ef_qty=1,
				qty=1,
				rate=10,
				purchase_invoice="PINV-1",
				pi_detail="PINV-1-1",
			),
			frappe._dict(
				idx=3,
				supplier_item_name="Item C",
				item_code="C",
				item_name="Item C",
				supplier_item_code="C1",
				ef_qty=4,
				qty=4,
				rate=20,
				purchase_invoice="",
			),
		]
		with self.assertRaises(frappe.ValidationError) as ctx:
			throw_unallocated_items(items, "Allocate all rows to a Purchase Invoice before submit", "MDL")
		msg = str(ctx.exception)
		self.assertIn("Row 1", msg)
		self.assertIn("Item A", msg)
		self.assertIn("Row 3", msg)
		self.assertIn("Item C", msg)
		self.assertNotIn("Item B", msg)
		self.assertIn("<li>", msg)

	def test_sfs_reject_response_errors(self):
		self.assertTrue(_sfs_action_error(None))
		self.assertEqual(_sfs_action_error({"ErrorMessage": "nope"}), "nope")
		self.assertTrue(_sfs_action_error({"Status": 3}))
		self.assertIsNone(_sfs_action_error({"Status": 2}))
		self.assertEqual(
			_sfs_action_error({"Results": {"InvoiceResult": {"Status": 3, "Message": "cannot reject"}}}),
			"cannot reject",
		)

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

	def test_buying_rate_follows_xml_line_total(self):
		from erpnext_moldova_efactura.utils.buying_rate import buying_rate_for_row

		row = frappe._dict(
			qty=186.763,
			ef_qty=186.763,
			rate=26.08,
			rate_with_vat=31.296,
			net_amount=4870.23,
			amount=5844.28,
		)
		rate = buying_rate_for_row(row, vat_included=False)
		self.assertAlmostEqual(flt(rate * row.qty, 2), 4870.23, places=2)
		self.assertNotAlmostEqual(flt(row.rate * row.qty, 2), 4870.23, places=2)
		self.assertAlmostEqual(flt(row.rate * row.qty, 2), 4870.78, places=2)

	def test_parse_invoice_xml(self):
		parsed = parse_invoice_xml(SAMPLE_XML)
		self.assertEqual(parsed["ef_series"], "EBJ")
		self.assertEqual(parsed["ef_number"], "000066606")
		self.assertEqual(parsed["supplier"]["idno"], "1015608001255")
		self.assertEqual(parsed["supplier"]["taxpayer_type"], "Company")
		self.assertEqual(parsed["buyer"]["taxpayer_type"], "Company")
		self.assertEqual(len(parsed["items"]), 1)
		self.assertEqual(parsed["items"][0]["supplier_item_code"], "A1")
		self.assertEqual(parsed["items"][0]["supplier_uom"], "buc")
		self.assertEqual(parsed["items"][0]["ef_qty"], 2)
		self.assertEqual(parsed["vat_total"], 18)
		self.assertEqual(str(parsed["issue_date"]), "2026-06-09")
		self.assertEqual(get_time(parsed["issue_time"]), get_time("10:38:41"))

	def test_parse_issued_date_with_fractional_seconds(self):
		xml = SAMPLE_XML.replace(
			"<IssuedDate>2026-06-09T10:38:41</IssuedDate>",
			"<IssuedDate>2026-06-26T16:12:59.65</IssuedDate>",
		).replace(
			"<DeliveryDate>2026-06-09T10:38:01</DeliveryDate>",
			"<DeliveryDate>2026-06-26T16:06:15.001Z</DeliveryDate>",
		)
		parsed = parse_invoice_xml(xml)
		self.assertEqual(str(parsed["issue_date"]), "2026-06-26")
		self.assertEqual(get_time(parsed["issue_time"]), get_time("16:12:59"))
		self.assertEqual(str(parsed["delivery_date"]), "2026-06-26")

	def test_parse_unescapes_html_entities_in_bank_name(self):
		xml = SAMPLE_XML.replace(
			'BranchTitle="Bank"',
			'BranchTitle="BC&amp;apos;Moldindconbank&amp;apos;S.A. suc Durlesti"',
		)
		parsed = parse_invoice_xml(xml)
		self.assertEqual(
			parsed["supplier"]["bank_name"],
			"BC'Moldindconbank'S.A. suc Durlesti",
		)

	def test_taxpayer_type_mapping(self):
		self.assertEqual(taxpayer_type_from_sfs("1"), "Company")
		self.assertEqual(taxpayer_type_from_sfs(2), "Individual")
		self.assertEqual(taxpayer_type_from_sfs("3"), "Non-Resident")
		self.assertEqual(taxpayer_type_from_sfs("Company"), "Company")
		self.assertEqual(taxpayer_type_from_sfs(""), "")
		self.assertEqual(taxpayer_type_from_sfs(None), "")
		self.assertEqual(taxpayer_type_to_sfs("Company"), "1")
		self.assertEqual(taxpayer_type_to_sfs("Individual"), "2")
		self.assertEqual(taxpayer_type_to_sfs("Non-Resident"), "3")
		self.assertEqual(taxpayer_type_to_sfs("1"), "1")
		self.assertEqual(taxpayer_type_to_sfs(""), "")

	def test_pef_currency_helpers(self):
		payload = remap_xml_item_money({"rate": 10, "amount": 100, "supplier_item_code": "A"})
		self.assertEqual(payload["ef_rate"], 10)
		self.assertEqual(payload["ef_amount"], 100)
		self.assertNotIn("rate", payload)
		self.assertEqual(payload["supplier_item_code"], "A")

		class _Row:
			pass

		class _Doc:
			pass

		doc = _Doc()
		doc.currency = "USD"
		doc.ef_currency = "MDL"
		doc.ef_conversion_rate = 2
		doc.ef_total = 118
		doc.ef_vat_total = 18
		doc.ef_net_total = 100
		row = _Row()
		row.ef_rate = 50
		row.ef_rate_with_vat = 59
		row.ef_amount = 118
		row.ef_net_amount = 100
		row.ef_vat_amount = 18
		doc.items = [row]
		apply_document_amounts_from_ef(doc)
		self.assertEqual(flt(doc.total), 59)
		self.assertEqual(flt(row.amount), 59)
		self.assertEqual(flt(row.net_amount), 50)
		self.assertTrue(default_document_currency())

	def test_unescape_sfs_text_decodes_stored_entities(self):
		self.assertEqual(
			unescape_sfs_text("BC&apos;Moldindconbank&apos;S.A. suc Durlesti"),
			"BC'Moldindconbank'S.A. suc Durlesti",
		)
		self.assertEqual(
			unescape_sfs_text("BC'Moldindconbank'S.A. suc Durlesti"),
			"BC'Moldindconbank'S.A. suc Durlesti",
		)

	def test_normalize_supplier_title(self):
		self.assertEqual(normalize_supplier_title('S.R.L. "PRIM-LOGIST"'), "PRIM-LOGIST SRL")
		self.assertEqual(normalize_supplier_title("PRIM-LOGIST S.R.L."), "PRIM-LOGIST SRL")
		self.assertEqual(normalize_supplier_title("„Hotel Life” s.r.l"), "HOTEL LIFE SRL")
		self.assertEqual(normalize_supplier_title("  already   srl  "), "ALREADY SRL")
		self.assertEqual(normalize_supplier_title("SRL PRIM-LOGIST"), "PRIM-LOGIST SRL")
		self.assertEqual(normalize_supplier_title('S.A. "MOLDOVA-AGRO"'), "MOLDOVA-AGRO SA")
		self.assertEqual(normalize_supplier_title("MOLDOVA-AGRO S.A."), "MOLDOVA-AGRO SA")
		self.assertEqual(normalize_supplier_title("SA MOLDOVA-AGRO"), "MOLDOVA-AGRO SA")
		self.assertEqual(normalize_supplier_title('S.C. "PRIM-LOGIST" S.R.L.'), "PRIM-LOGIST SRL")
		self.assertEqual(normalize_supplier_title("SC PRIM-LOGIST SRL"), "PRIM-LOGIST SRL")
		self.assertEqual(normalize_supplier_title("SCANIA MOLDOVA"), "SCANIA MOLDOVA")
		self.assertEqual(normalize_supplier_title(""), "")

	def test_new_supplier_defaults_title_and_idno(self):
		field = frappe.db.get_single_value("eFactura Settings", "supplier_idno_field")
		if not field or not frappe.get_meta("Supplier").has_field(field):
			self.skipTest("Supplier IDNO field is not configured")
		defaults = new_supplier_defaults('S.R.L. "PRIM-LOGIST"', "1015608001255")
		self.assertEqual(defaults.get("supplier_name"), "PRIM-LOGIST SRL")
		self.assertEqual(defaults.get(field), "1015608001255")


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

	def test_compute_uses_stored_conversion_factors(self):
		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		if not item:
			self.skipTest("No Item")

		out = compute_buyer_item_qtys(
			item.name,
			item.stock_uom,
			2,
			item.stock_uom,
			conversion_factor=2,
			ef_conversion_factor=4,
		)
		self.assertEqual(out["stock_qty"], 8)
		self.assertEqual(out["qty"], 4)

	def test_apply_qty_defaults_keeps_stored_factors(self):
		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		if not item:
			self.skipTest("No Item")

		row = frappe._dict(
			item_code=item.name,
			supplier_uom=item.stock_uom,
			ef_uom=item.stock_uom,
			uom=item.stock_uom,
			ef_qty=2,
			conversion_factor=2,
			ef_conversion_factor=4,
			stock_uom=None,
			stock_qty=None,
			qty=None,
		)
		apply_qty_defaults(row)
		self.assertEqual(flt(row.conversion_factor), 2)
		self.assertEqual(flt(row.ef_conversion_factor), 4)
		self.assertEqual(flt(row.stock_qty), 8)
		self.assertEqual(flt(row.qty), 4)

	def test_apply_qty_defaults_captures_when_factors_empty(self):
		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		if not item:
			self.skipTest("No Item")

		extra = frappe.db.get_value(
			"UOM Conversion Detail",
			{"parent": item.name, "uom": ["!=", item.stock_uom]},
			["uom", "conversion_factor"],
			as_dict=True,
		)
		if not extra:
			self.skipTest("No extra UOM conversion on item")

		row = frappe._dict(
			item_code=item.name,
			supplier_uom=extra.uom,
			ef_uom=extra.uom,
			uom=item.stock_uom,
			ef_qty=2,
			conversion_factor=0,
			ef_conversion_factor=0,
			stock_uom=None,
			stock_qty=None,
			qty=None,
		)
		apply_qty_defaults(row)
		self.assertEqual(flt(row.ef_conversion_factor), float(extra.conversion_factor))
		self.assertEqual(flt(row.conversion_factor), 1)
		self.assertEqual(flt(row.stock_qty), 2 * float(extra.conversion_factor))
		self.assertEqual(flt(row.qty), flt(row.stock_qty))

	def test_apply_qty_defaults_force_recaptures_from_item(self):
		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		if not item:
			self.skipTest("No Item")

		row = frappe._dict(
			item_code=item.name,
			supplier_uom=item.stock_uom,
			ef_uom=item.stock_uom,
			uom=item.stock_uom,
			ef_qty=2,
			conversion_factor=99,
			ef_conversion_factor=99,
			stock_uom=None,
			stock_qty=None,
			qty=None,
		)
		apply_qty_defaults(row, force=True)
		self.assertEqual(flt(row.conversion_factor), 1)
		self.assertEqual(flt(row.ef_conversion_factor), 1)
		self.assertEqual(flt(row.stock_qty), 2)
		self.assertEqual(flt(row.qty), 2)


class TestEFacturaBuyerDoc(FrappeTestCase):
	SAMPLE_SUPPLIER_IDNO = "1015608001255"

	def setUp(self):
		super().setUp()
		self.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		if not self.company:
			self.skipTest("No company")
		self._align_default_supplier_idno()

	def tearDown(self):
		self._restore_default_supplier_idno()
		super().tearDown()

	def _align_default_supplier_idno(self, idno: str | None = None):
		self._idno_field = frappe.db.get_single_value("eFactura Settings", "supplier_idno_field")
		self._idno_supplier = frappe.db.get_value("Supplier", {}, "name")
		self._idno_prev = None
		if not self._idno_field or not self._idno_supplier:
			return
		if not frappe.get_meta("Supplier").has_field(self._idno_field):
			self._idno_field = None
			return
		self._idno_prev = frappe.db.get_value("Supplier", self._idno_supplier, self._idno_field)
		target = idno or self.SAMPLE_SUPPLIER_IDNO
		if normalize_idno(self._idno_prev) != normalize_idno(target):
			frappe.db.set_value(
				"Supplier", self._idno_supplier, self._idno_field, target, update_modified=False
			)

	def _restore_default_supplier_idno(self):
		field = getattr(self, "_idno_field", None)
		supplier = getattr(self, "_idno_supplier", None)
		if not field or not supplier:
			return
		frappe.db.set_value("Supplier", supplier, field, self._idno_prev, update_modified=False)

	def test_supplier_idno_must_match_factura(self):
		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")
		if not self._idno_field:
			self.skipTest("Supplier IDNO field is not configured")

		self._delete_buyer("EBJ", "000066612")
		frappe.db.set_value("Supplier", sup, self._idno_field, "9999999999999", update_modified=False)
		xml = SAMPLE_XML.replace("000066606", "000066612")
		doc = frappe.get_doc(
			{
				"doctype": "Purchase eFactura",
				"naming_series": "ACC-PEF-.YYYY.-",
				"company": self.company,
				"ef_series": "EBJ",
				"ef_number": "000066612",
				"ef_status": 8,
				"supplier": sup,
			}
		)
		doc.fill_from_xml(xml)
		doc.supplier = sup
		doc.items[0].item_code = item.name
		doc.items[0].ef_uom = item.stock_uom
		doc.items[0].uom = item.stock_uom
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.insert()
		self.assertIn("IDNO", str(ctx.exception))

		frappe.db.set_value(
			"Supplier", sup, self._idno_field, self.SAMPLE_SUPPLIER_IDNO, update_modified=False
		)
		doc.insert()
		self.assertEqual(doc.supplier, sup)

	def test_make_purchase_invoice_uses_vat_inclusive_rate(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			make_purchase_invoice,
		)

		name = frappe.db.exists(
			"Purchase eFactura",
			{"company": self.company, "ef_series": "EBJ", "ef_number": "000066606"},
		)
		if name:
			doc = frappe.get_doc("Purchase eFactura", name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Purchase eFactura", name, force=1)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		prev = frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate")
		try:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 1)
			doc = frappe.get_doc(
				{
					"doctype": "Purchase eFactura",
					"naming_series": "ACC-PEF-.YYYY.-",
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

			pi = make_purchase_invoice(doc.name)
			# SAMPLE_XML: rate=50, rate_with_vat=59 (118/2), qty=2
			self.assertAlmostEqual(flt(pi.items[0].rate), 59.0, places=2)

			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)
			pi_net = make_purchase_invoice(doc.name)
			self.assertAlmostEqual(flt(pi_net.items[0].rate), 50.0, places=2)
		finally:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", prev)

	def test_make_purchase_invoice_uses_xml_line_amount(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			make_purchase_invoice,
		)

		name = frappe.db.exists(
			"Purchase eFactura",
			{"company": self.company, "ef_series": "EBJ", "ef_number": "000066610"},
		)
		if name:
			doc = frappe.get_doc("Purchase eFactura", name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Purchase eFactura", name, force=1)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		from frappe.model.meta import get_field_precision

		rate_prec = cint(get_field_precision(frappe.get_meta("Purchase Invoice Item").get_field("rate")))
		if rate_prec < 5:
			self.skipTest("Purchase Invoice Item.rate precision is below 5; set it in Customize Form")

		prev = frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate")
		try:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)
			xml = (
				SAMPLE_XML.replace("000066606", "000066610")
				.replace('Quantity="2"', 'Quantity="186.763"')
				.replace('UnitPriceWithoutTVA="50"', 'UnitPriceWithoutTVA="26.08"')
				.replace('TotalPriceWithoutTVA="100"', 'TotalPriceWithoutTVA="4870.23"')
				.replace('TotalTVA="18"', 'TotalTVA="974.05"')
				.replace('TVA="18"', 'TVA="20"')
				.replace('TotalPrice="118"', 'TotalPrice="5844.28"')
				.replace("<Total>118.00</Total>", "<Total>5844.28</Total>")
				.replace("<TotalTVA>18.00</TotalTVA>", "<TotalTVA>974.05</TotalTVA>")
			)
			doc = frappe.get_doc(
				{
					"doctype": "Purchase eFactura",
					"naming_series": "ACC-PEF-.YYYY.-",
					"company": self.company,
					"ef_series": "EBJ",
					"ef_number": "000066610",
					"ef_status": 8,
					"supplier": sup,
				}
			)
			doc.fill_from_xml(xml)
			doc.items[0].item_code = item.name
			doc.items[0].ef_uom = item.stock_uom
			doc.items[0].uom = item.stock_uom
			doc.insert()

			self.assertAlmostEqual(flt(doc.items[0].net_amount), 4870.23, places=2)
			self.assertAlmostEqual(flt(doc.items[0].rate), 26.08, places=2)

			pi = make_purchase_invoice(doc.name)
			self.assertTrue(pi.get_onload("load_after_mapping"))
			if pi.meta.has_field("purchase_efactura"):
				self.assertEqual(pi.purchase_efactura, doc.name)
			self.assertTrue(pi.as_dict().get("__onload", {}).get("load_after_mapping"))
			self.assertEqual(pi.price_list_currency, pi.currency)
			self.assertEqual(flt(pi.conversion_rate), 1)
			self.assertEqual(flt(pi.plc_conversion_rate), 1)
			self.assertAlmostEqual(flt(pi.items[0].amount), 4870.23, places=2)
			self.assertAlmostEqual(flt(pi.items[0].qty * pi.items[0].rate, 2), 4870.23, places=2)
			self.assertNotAlmostEqual(flt(pi.items[0].rate), 26.08, places=4)
		finally:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", prev)

	def test_make_purchase_invoice_applies_company_tax_template(self):
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			make_purchase_invoice,
		)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		tmpl = frappe.db.get_value(
			"Purchase Taxes and Charges Template", {"company": self.company}, "name"
		)
		if not item or not sup or not tmpl:
			self.skipTest("Need Item, Supplier, and Purchase Taxes and Charges Template")

		tax_rows = get_taxes_and_charges("Purchase Taxes and Charges Template", tmpl) or []
		vat_account = tax_rows[0].get("account_head") if tax_rows else None

		name = frappe.db.exists(
			"Purchase eFactura",
			{"company": self.company, "ef_series": "EBJ", "ef_number": "000066607"},
		)
		if name:
			doc = frappe.get_doc("Purchase eFactura", name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Purchase eFactura", name, force=1)

		prev_itemwise = frappe.db.get_single_value("Accounts Settings", "add_taxes_from_item_tax_template")
		prev_incl = frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate")
		settings = frappe.get_single("eFactura Settings")
		prev_company_settings = [r.as_dict() for r in (settings.company_settings or [])]
		try:
			settings.set("company_settings", [])
			settings.append(
				"company_settings",
				{
					"company": self.company,
					"taxes_and_charges": tmpl,
					"buying_vat_account": vat_account,
				},
			)
			settings.flags.ignore_permissions = True
			settings.save()
			frappe.db.set_single_value("Accounts Settings", "add_taxes_from_item_tax_template", 0)
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)

			xml = SAMPLE_XML.replace("000066606", "000066607")
			doc = frappe.get_doc(
				{
					"doctype": "Purchase eFactura",
					"naming_series": "ACC-PEF-.YYYY.-",
					"company": self.company,
					"ef_series": "EBJ",
					"ef_number": "000066607",
					"ef_status": 8,
					"supplier": sup,
				}
			)
			doc.fill_from_xml(xml)
			doc.items[0].item_code = item.name
			doc.items[0].ef_uom = item.stock_uom
			doc.items[0].uom = item.stock_uom
			doc.insert()

			pi = make_purchase_invoice(doc.name)
			self.assertEqual(pi.taxes_and_charges, tmpl)
			self.assertTrue(pi.taxes)
			if vat_account:
				vat_rows = [t for t in pi.taxes if t.account_head == vat_account]
				self.assertTrue(vat_rows)
				self.assertEqual(int(vat_rows[0].included_in_print_rate or 0), 0)

			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 1)
			pi_inc = make_purchase_invoice(doc.name)
			if vat_account:
				vat_rows = [t for t in (pi_inc.taxes or []) if t.account_head == vat_account]
				self.assertTrue(vat_rows)
				self.assertEqual(int(vat_rows[0].included_in_print_rate or 0), 1)
		finally:
			settings = frappe.get_single("eFactura Settings")
			settings.set("company_settings", [])
			for row in prev_company_settings:
				settings.append("company_settings", row)
			settings.flags.ignore_permissions = True
			settings.save()
			frappe.db.set_single_value("Accounts Settings", "add_taxes_from_item_tax_template", prev_itemwise)
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", prev_incl)

	def test_make_purchase_invoice_adds_actual_vat_when_no_template(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			make_purchase_invoice,
		)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		account = frappe.db.get_value(
			"Account",
			{"company": self.company, "is_group": 0, "account_type": "Tax"},
			"name",
		) or frappe.db.get_value("Account", {"company": self.company, "is_group": 0}, "name")
		if not item or not sup or not account:
			self.skipTest("Need Item, Supplier, and Account")

		name = frappe.db.exists(
			"Purchase eFactura",
			{"company": self.company, "ef_series": "EBJ", "ef_number": "000066608"},
		)
		if name:
			doc = frappe.get_doc("Purchase eFactura", name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Purchase eFactura", name, force=1)

		prev_itemwise = frappe.db.get_single_value("Accounts Settings", "add_taxes_from_item_tax_template")
		prev_incl = frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate")
		settings = frappe.get_single("eFactura Settings")
		prev_company_settings = [r.as_dict() for r in (settings.company_settings or [])]
		try:
			settings.set("company_settings", [])
			settings.append(
				"company_settings",
				{"company": self.company, "buying_vat_account": account},
			)
			settings.flags.ignore_permissions = True
			settings.save()
			frappe.db.set_single_value("Accounts Settings", "add_taxes_from_item_tax_template", 0)
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)

			xml = SAMPLE_XML.replace("000066606", "000066608")
			doc = frappe.get_doc(
				{
					"doctype": "Purchase eFactura",
					"naming_series": "ACC-PEF-.YYYY.-",
					"company": self.company,
					"ef_series": "EBJ",
					"ef_number": "000066608",
					"ef_status": 8,
					"supplier": sup,
				}
			)
			doc.fill_from_xml(xml)
			doc.items[0].item_code = item.name
			doc.items[0].ef_uom = item.stock_uom
			doc.items[0].uom = item.stock_uom
			doc.insert()

			pi = make_purchase_invoice(doc.name)
			vat_rows = [t for t in (pi.taxes or []) if t.account_head == account]
			self.assertTrue(vat_rows)
			self.assertEqual(vat_rows[0].charge_type, "Actual")
			self.assertAlmostEqual(flt(vat_rows[0].tax_amount), 18.0, places=2)
			self.assertEqual(int(vat_rows[0].included_in_print_rate or 0), 0)

			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 1)
			pi_inc = make_purchase_invoice(doc.name)
			vat_rows = [t for t in (pi_inc.taxes or []) if t.account_head == account]
			self.assertTrue(vat_rows)
			self.assertEqual(int(vat_rows[0].included_in_print_rate or 0), 1)
			self.assertNotEqual(vat_rows[0].charge_type, "Actual")
			self.assertAlmostEqual(flt(pi_inc.items[0].rate), 59.0, places=2)
			self.assertAlmostEqual(flt(vat_rows[0].tax_amount), 18.0, places=2)
		finally:
			settings = frappe.get_single("eFactura Settings")
			settings.set("company_settings", [])
			for row in prev_company_settings:
				settings.append("company_settings", row)
			settings.flags.ignore_permissions = True
			settings.save()
			frappe.db.set_single_value("Accounts Settings", "add_taxes_from_item_tax_template", prev_itemwise)
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", prev_incl)

	def test_make_purchase_invoice_copies_posting_from_factura(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			make_purchase_invoice,
			make_purchase_order,
		)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		name = frappe.db.exists(
			"Purchase eFactura",
			{"company": self.company, "ef_series": "EBJ", "ef_number": "000066609"},
		)
		if name:
			doc = frappe.get_doc("Purchase eFactura", name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Purchase eFactura", name, force=1)

		prev = frappe.db.get_single_value("eFactura Settings", "copy_date_from_factura")
		try:
			frappe.db.set_single_value("eFactura Settings", "copy_date_from_factura", 1)
			xml = SAMPLE_XML.replace("000066606", "000066609")
			doc = frappe.get_doc(
				{
					"doctype": "Purchase eFactura",
					"naming_series": "ACC-PEF-.YYYY.-",
					"company": self.company,
					"ef_series": "EBJ",
					"ef_number": "000066609",
					"ef_status": 8,
					"supplier": sup,
				}
			)
			doc.fill_from_xml(xml)
			doc.items[0].item_code = item.name
			doc.items[0].ef_uom = item.stock_uom
			doc.items[0].uom = item.stock_uom
			doc.insert()

			pi = make_purchase_invoice(doc.name)
			self.assertEqual(int(pi.set_posting_time or 0), 1)
			self.assertEqual(getdate(pi.posting_date), getdate("2026-06-09"))
			self.assertEqual(get_time(pi.posting_time), get_time("10:38:41"))

			po = make_purchase_order(doc.name)
			self.assertEqual(getdate(po.transaction_date), getdate("2026-06-09"))
			if po.meta.has_field("purchase_efactura"):
				self.assertEqual(po.purchase_efactura, doc.name)

			frappe.db.set_single_value("eFactura Settings", "copy_date_from_factura", 0)
			pi_off = make_purchase_invoice(doc.name)
			self.assertNotEqual(getdate(pi_off.posting_date), getdate("2026-06-09"))
			po_off = make_purchase_order(doc.name)
			self.assertNotEqual(getdate(po_off.transaction_date), getdate("2026-06-09"))
		finally:
			frappe.db.set_single_value("eFactura Settings", "copy_date_from_factura", prev)

	def test_pi_from_po_applies_factura_defaults(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			apply_factura_defaults_from_po,
			make_purchase_order,
		)
		from erpnext_moldova_efactura.utils.pi_alloc import find_buyer_by_po, find_source_buyer

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")
		if not frappe.get_meta("Purchase Order").has_field("purchase_efactura"):
			self.skipTest("Purchase Order.purchase_efactura missing")

		name = frappe.db.exists(
			"Purchase eFactura",
			{"company": self.company, "ef_series": "EBJ", "ef_number": "000066611"},
		)
		if name:
			doc = frappe.get_doc("Purchase eFactura", name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Purchase eFactura", name, force=1)

		prev = frappe.db.get_single_value("eFactura Settings", "copy_date_from_factura")
		try:
			frappe.db.set_single_value("eFactura Settings", "copy_date_from_factura", 1)
			xml = SAMPLE_XML.replace("000066606", "000066611")
			doc = frappe.get_doc(
				{
					"doctype": "Purchase eFactura",
					"naming_series": "ACC-PEF-.YYYY.-",
					"company": self.company,
					"ef_series": "EBJ",
					"ef_number": "000066611",
					"ef_status": 8,
					"supplier": sup,
				}
			)
			doc.fill_from_xml(xml)
			doc.items[0].item_code = item.name
			doc.items[0].ef_uom = item.stock_uom
			doc.items[0].uom = item.stock_uom
			doc.insert()

			po = make_purchase_order(doc.name)
			self.assertEqual(po.purchase_efactura, doc.name)

			pi = frappe.new_doc("Purchase Invoice")
			pi.company = doc.company
			pi.supplier = doc.supplier
			pi.purchase_efactura = doc.name
			self.assertTrue(apply_factura_defaults_from_po(pi))
			self.assertEqual(int(pi.set_posting_time or 0), 1)
			self.assertEqual(getdate(pi.posting_date), getdate("2026-06-09"))
			self.assertEqual(get_time(pi.posting_time), get_time("10:38:41"))
			self.assertEqual(getdate(pi.bill_date), getdate("2026-06-09"))
			self.assertEqual(pi.purchase_efactura, doc.name)
			self.assertTrue(pi.get_onload("load_after_mapping"))

			try:
				po.flags.ignore_mandatory = True
				po.insert()
			except Exception as e:
				self.skipTest(f"Cannot insert Purchase Order: {e}")

			pi_po = frappe.new_doc("Purchase Invoice")
			pi_po.company = doc.company
			pi_po.supplier = doc.supplier
			pi_po.append(
				"items",
				{
					"item_code": item.name,
					"qty": 2,
					"uom": item.stock_uom,
					"rate": 50,
					"purchase_order": po.name,
				},
			)
			self.assertEqual(find_buyer_by_po(pi_po), doc.name)
			self.assertEqual(find_source_buyer(pi_po), doc.name)
			self.assertTrue(apply_factura_defaults_from_po(pi_po, po.name))
			self.assertEqual(getdate(pi_po.posting_date), getdate("2026-06-09"))
			self.assertEqual(pi_po.purchase_efactura, doc.name)
		finally:
			frappe.db.set_single_value("eFactura Settings", "copy_date_from_factura", prev)

	def test_fill_from_xml_and_unique(self):
		name = frappe.db.exists(
			"Purchase eFactura",
			{"company": self.company, "ef_series": "EBJ", "ef_number": "000066606"},
		)
		if name:
			frappe.delete_doc("Purchase eFactura", name, force=1)

		doc = frappe.get_doc(
			{
				"doctype": "Purchase eFactura",
				"naming_series": "ACC-PEF-.YYYY.-",
				"company": self.company,
				"ef_series": "EBJ",
				"ef_number": "000066606",
				"ef_status": 8,
			}
		)
		# Alias that cannot match Settings UOM Map / UOM name / translations,
		# so this does not depend on the site mapping "buc" → Unit.
		xml = SAMPLE_XML.replace('UnitOfMeasure="buc"', 'UnitOfMeasure="xyz-no-such-uom"')
		doc.fill_from_xml(xml)
		doc.insert()
		self.assertEqual(doc.status, "Signed by Buyer")
		self.assertEqual(len(doc.items), 1)
		self.assertEqual(doc.ef_supplier_idno, "1015608001255")
		self.assertEqual(flt(doc.ef_total), 118)
		self.assertEqual(flt(doc.total), flt(doc.ef_total) / (flt(doc.ef_conversion_rate) or 1))
		self.assertEqual(flt(doc.items[0].ef_amount), flt(doc.items[0].amount) * (flt(doc.ef_conversion_rate) or 1))
		self.assertEqual(doc.items[0].supplier_uom, "xyz-no-such-uom")
		self.assertFalse(doc.items[0].ef_uom)
		self.assertFalse(doc.items[0].uom)

		dup = frappe.get_doc(
			{
				"doctype": "Purchase eFactura",
				"naming_series": "ACC-PEF-.YYYY.-",
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
		if not doc.supplier:
			sup = frappe.db.get_value("Supplier", {}, "name")
			if not sup:
				self.skipTest("No Supplier")
			doc.supplier = sup
		doc.save()
		doc.reload()
		self.assertEqual(doc.items[0].supplier_uom, "xyz-no-such-uom")
		self.assertEqual(doc.items[0].ef_uom, stock_uom)
		self.assertEqual(doc.items[0].stock_uom, stock_uom)
		self.assertEqual(doc.items[0].uom, stock_uom)

		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.submit()
		msg = str(ctx.exception)
		self.assertIn("Row 1", msg)
		self.assertIn("<li>", msg)

		doc.reload()
		doc.items[0].purchase_invoice = "PINV-DUMMY"
		doc.items[0].pi_detail = "PINV-DUMMY-1"
		doc.flags.ignore_links = True
		doc.save()
		doc.flags.ignore_links = True
		doc.submit()
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.efactura_status, doc.status)

	def _delete_buyer(self, series: str, number: str):
		name = frappe.db.exists(
			"Purchase eFactura",
			{"company": self.company, "ef_series": series, "ef_number": number},
		)
		if not name:
			return
		doc = frappe.get_doc("Purchase eFactura", name)
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("Purchase eFactura", name, force=1)

	def test_item_map_persists_when_supplier_set_after_mapping(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			save_item_mappings,
		)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		self._delete_buyer("EBJ", "000066701")
		frappe.db.delete("eFactura Supplier Item Map", {"supplier": sup, "supplier_item_name": "Item A"})

		doc = frappe.get_doc(
			{
				"doctype": "Purchase eFactura",
				"naming_series": "ACC-PEF-.YYYY.-",
				"company": self.company,
				"ef_series": "EBJ",
				"ef_number": "000066701",
				"ef_status": 8,
			}
		)
		doc.fill_from_xml(SAMPLE_XML)
		doc.supplier = None
		doc.insert()
		save_item_mappings(doc.name, [{"idx": 1, "item_code": item.name}])
		self.assertFalse(
			frappe.db.exists(
				"eFactura Supplier Item Map",
				{"supplier": sup, "supplier_item_name": "Item A"},
			)
		)

		doc.reload()
		doc.supplier = sup
		if not doc.items[0].ef_uom:
			doc.items[0].ef_uom = item.stock_uom
			doc.items[0].uom = item.stock_uom
		doc.save()
		self.assertTrue(
			frappe.db.exists(
				"eFactura Supplier Item Map",
				{"supplier": sup, "supplier_item_name": "Item A", "item_code": item.name},
			)
		)

	def test_item_map_persists_when_mapping_after_supplier(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			save_item_mappings,
		)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		self._delete_buyer("EBJ", "000066702")
		frappe.db.delete("eFactura Supplier Item Map", {"supplier": sup, "supplier_item_name": "Item A"})

		doc = frappe.get_doc(
			{
				"doctype": "Purchase eFactura",
				"naming_series": "ACC-PEF-.YYYY.-",
				"company": self.company,
				"ef_series": "EBJ",
				"ef_number": "000066702",
				"ef_status": 8,
				"supplier": sup,
			}
		)
		doc.fill_from_xml(SAMPLE_XML)
		doc.supplier = sup
		doc.insert()
		save_item_mappings(doc.name, [{"idx": 1, "item_code": item.name}])
		self.assertTrue(
			frappe.db.exists(
				"eFactura Supplier Item Map",
				{"supplier": sup, "supplier_item_name": "Item A", "item_code": item.name},
			)
		)

	def _make_buyer(self, number, supplier, item, qty=2, ef_status=8):
		self._delete_buyer("EBJ", number)
		xml = SAMPLE_XML.replace("000066606", number)
		if qty != 2:
			xml = xml.replace('Quantity="2"', f'Quantity="{qty}"')
			net = 50 * qty
			vat = 9 * qty
			total = net + vat
			xml = xml.replace("<Total>118.00</Total>", f"<Total>{total:.2f}</Total>")
			xml = xml.replace("<TotalTVA>18.00</TotalTVA>", f"<TotalTVA>{vat:.2f}</TotalTVA>")
			xml = xml.replace('TotalPriceWithoutTVA="100"', f'TotalPriceWithoutTVA="{net:.2f}"')
			xml = xml.replace('TotalTVA="18"', f'TotalTVA="{vat:.2f}"')
			xml = xml.replace('TotalPrice="118"', f'TotalPrice="{total:.2f}"')
		doc = frappe.get_doc(
			{
				"doctype": "Purchase eFactura",
				"naming_series": "ACC-PEF-.YYYY.-",
				"company": self.company,
				"ef_series": "EBJ",
				"ef_number": number,
				"ef_status": ef_status,
				"supplier": supplier,
			}
		)
		doc.fill_from_xml(xml)
		doc.items[0].item_code = item.name
		doc.items[0].ef_uom = item.stock_uom
		doc.items[0].uom = item.stock_uom
		doc.insert()
		return doc

	def _make_pi(self, supplier, item_code, qty, uom, rate=50, bill_no=None):
		pi = frappe.new_doc("Purchase Invoice")
		pi.company = self.company
		pi.supplier = supplier
		if bill_no:
			pi.bill_no = bill_no
		pi.append(
			"items",
			{
				"item_code": item_code,
				"qty": qty,
				"uom": uom,
				"rate": rate,
			},
		)
		try:
			pi.insert()
		except Exception as e:
			self.skipTest(f"Cannot insert Purchase Invoice: {e}")
		return pi

	def _align_pi_totals(self, pi, buyer):
		frappe.db.set_value(
			"Purchase Invoice",
			pi.name,
			{
				"grand_total": buyer.total,
				"rounded_total": buyer.total,
				"total_taxes_and_charges": buyer.vat_total,
			},
			update_modified=False,
		)
		pi.reload()

	def test_link_invoice_requires_supplier(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			link_purchase_invoice,
		)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		doc = self._make_buyer("000066801", sup, item, qty=2)
		doc.db_set("supplier", None)
		doc.reload()
		pi = self._make_pi(sup, item.name, 2, item.stock_uom)
		self.assertRaises(frappe.ValidationError, link_purchase_invoice, doc.name, pi.name)

	def test_link_invoice_one_to_one(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			link_purchase_invoice,
		)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		doc = self._make_buyer("000066802", sup, item, qty=2)
		prev_incl = frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate")
		frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)
		try:
			pi = self._make_pi(sup, item.name, 2, item.stock_uom, rate=50)
			self._align_pi_totals(pi, doc)
			link_purchase_invoice(doc.name, pi.name)
			doc.reload()
			self.assertEqual(doc.items[0].purchase_invoice, pi.name)
			self.assertTrue(doc.items[0].pi_detail)
			self.assertNotIn("Linked to PI", doc.status or "")
		finally:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", prev_incl)

	def test_link_invoice_rejects_partial_qty(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			link_purchase_invoice,
		)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		doc = self._make_buyer("000066803", sup, item, qty=10)
		prev_incl = frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate")
		frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)
		try:
			pi1 = self._make_pi(sup, item.name, 6, item.stock_uom, rate=50)
			self.assertRaises(frappe.ValidationError, link_purchase_invoice, doc.name, pi1.name)
			doc.reload()
			self.assertFalse(any(r.purchase_invoice for r in (doc.items or [])))
		finally:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", prev_incl)

	def test_link_invoice_rate_mismatch_writes_nothing(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			link_purchase_invoice,
		)

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		doc = self._make_buyer("000066804", sup, item, qty=2)
		prev_incl = frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate")
		frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)
		try:
			pi = self._make_pi(sup, item.name, 2, item.stock_uom, rate=40)
			self.assertRaises(frappe.ValidationError, link_purchase_invoice, doc.name, pi.name)
			doc.reload()
			self.assertFalse(any(r.purchase_invoice for r in (doc.items or [])))
			self.assertNotIn("Linked to PI", doc.status or "")
		finally:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", prev_incl)

	def test_pi_fiscal_status_from_buyer(self):
		from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura import (
			link_purchase_invoice,
		)
		from erpnext_moldova_efactura.utils.fiscal_status import determine_pi_fiscal_status

		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		doc = self._make_buyer("000066805", sup, item, qty=2, ef_status=7)
		prev_incl = frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate")
		frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", 0)
		try:
			pi = self._make_pi(sup, item.name, 2, item.stock_uom, rate=50)
			self._align_pi_totals(pi, doc)
			link_purchase_invoice(doc.name, pi.name)
			doc.reload()
			doc.submit()
			pi.reload()
			self.assertIsNone(determine_pi_fiscal_status(pi))
			pi.db_set("docstatus", 1)
			pi.reload()
			supplier_type = frappe.db.get_value("Supplier", sup, "supplier_type")
			if supplier_type == "Individual":
				self.assertEqual(determine_pi_fiscal_status(pi), "Not Required")
			else:
				self.assertEqual(determine_pi_fiscal_status(pi), "In Progress")
				doc.db_set("ef_status", 8)
				self.assertEqual(determine_pi_fiscal_status(pi), "Completed")
				doc.db_set("ef_status", 4)
				self.assertEqual(determine_pi_fiscal_status(pi), "Pending")
				doc.db_set("docstatus", 0)
				self.assertEqual(determine_pi_fiscal_status(pi), "Pending (Draft)")
		finally:
			frappe.db.set_single_value("eFactura Settings", "vat_included_in_rate", prev_incl)

	def test_sfs_status_update_after_submit(self):
		item = frappe.db.get_value("Item", {"disabled": 0}, ["name", "stock_uom"], as_dict=True)
		sup = frappe.db.get_value("Supplier", {}, "name")
		if not item or not sup:
			self.skipTest("Need Item and Supplier")

		doc = self._make_buyer("000066806", sup, item, qty=2, ef_status=3)
		doc.items[0].purchase_invoice = "PINV-DUMMY"
		doc.items[0].pi_detail = "PINV-DUMMY-1"
		doc.flags.ignore_links = True
		doc.save()
		doc.flags.ignore_links = True
		doc.submit()
		self.assertEqual(doc.status, "Accepted")
		self.assertEqual(doc.efactura_status, doc.status)
		doc.persist_sfs_status(8)
		doc.reload()
		self.assertEqual(cint(doc.ef_status), 8)
		self.assertEqual(doc.status, "Signed by Buyer")
		self.assertEqual(doc.efactura_status, doc.status)
		self.assertTrue(doc.last_status_check)

		doc.ef_status = 3
		doc.last_status_check = now_datetime()
		doc.set_status(update=False)
		doc.flags.ignore_links = True
		doc.save()
		doc.reload()
		self.assertEqual(doc.status, "Accepted")



class TestEFacturaBuyerPIMatch(FrappeTestCase):
	def _pair(self, **pi_header):
		from types import SimpleNamespace

		buyer = SimpleNamespace(
			name="EFB-TEST",
			supplier="SUP-1",
			company="CO-1",
			currency="MDL",
			total=118,
			vat_total=18,
			net_total=100,
			purchase_invoice=None,
			items=[
				SimpleNamespace(
					idx=1,
					name="BI-1",
					supplier_item_name="Item A",
					supplier_item_code="A1",
					item_code=None,
					item_name=None,
					ef_qty=2,
					qty=2,
					uom="Nos",
					ef_uom="Nos",
					rate=50,
					rate_with_vat=59,
					net_amount=100,
					amount=118,
					vat_amount=18,
					purchase_invoice=None,
					pi_detail=None,
				)
			],
		)
		pi = SimpleNamespace(
			name="PINV-TEST",
			supplier="SUP-1",
			company="CO-1",
			currency="MDL",
			grand_total=118,
			total_taxes_and_charges=18,
			docstatus=1,
			purchase_efactura=None,
			items=[
				SimpleNamespace(
					idx=1,
					item_code="ITEM-A",
					item_name="Item A",
					qty=2,
					uom="Nos",
					rate=50,
					amount=100,
				)
			],
		)
		for key, value in pi_header.items():
			setattr(pi, key, value)
		return buyer, pi

	def test_match_ok(self):
		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		buyer, pi = self._pair()
		errors, pairs = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertEqual(errors, [])
		self.assertEqual(len(pairs), 1)
		self.assertEqual(pairs[0][1].item_code, "ITEM-A")

	def test_grand_total_mismatch(self):
		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		buyer, pi = self._pair(grand_total=200)
		errors, _ = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertTrue(any("Grand Total" in e for e in errors))

	def test_vat_total_mismatch(self):
		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		buyer, pi = self._pair(total_taxes_and_charges=0)
		errors, _ = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertTrue(any("VAT Total" in e for e in errors))

	def test_qty_mismatch(self):
		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		buyer, pi = self._pair()
		pi.items[0].qty = 5
		errors, _ = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertTrue(any("quantity" in e.lower() or "no matching" in e.lower() for e in errors))

	def test_rate_mismatch(self):
		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		buyer, pi = self._pair()
		pi.items[0].rate = 40
		pi.items[0].amount = 80
		errors, _ = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertTrue(any("rate" in e.lower() or "no matching" in e.lower() for e in errors))

	def test_item_count_mismatch(self):
		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		from types import SimpleNamespace

		buyer, pi = self._pair()
		pi.items.append(
			SimpleNamespace(
				idx=2,
				item_code="ITEM-B",
				item_name="Item B",
				qty=1,
				uom="Nos",
				rate=10,
				amount=10,
			)
		)
		errors, _ = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertTrue(any("Item count" in e for e in errors))

	def test_mapped_item_code_mismatch(self):
		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		buyer, pi = self._pair()
		buyer.items[0].item_code = "OTHER-ITEM"
		errors, _ = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertTrue(errors)

	def test_compose_status_keeps_sfs_label(self):
		self.assertEqual(compose_buyer_status(3, "PINV-001"), "Accepted")

	def test_pending_fiscal_without_buyer(self):
		from erpnext_moldova_efactura.utils.fiscal_status import determine_pi_fiscal_status

		class Dummy:
			def __init__(self, **kw):
				self.__dict__.update(kw)

			def get(self, key, default=None):
				return getattr(self, key, default)

		self.assertIsNone(determine_pi_fiscal_status(Dummy(docstatus=0, purchase_efactura=None)))
		self.assertIsNone(determine_pi_fiscal_status(Dummy(docstatus=2, purchase_efactura=None)))
		self.assertEqual(determine_pi_fiscal_status(Dummy(docstatus=1, purchase_efactura=None)), "Pending")

	def test_classify_pi_fiscal_status(self):
		from erpnext_moldova_efactura.utils.fiscal_status import classify_pi_fiscal_status

		self.assertEqual(
			classify_pi_fiscal_status(
				individual=True, has_factura=True, total=10, signed=10, in_progress=0, precision=3
			),
			"Not Required",
		)
		self.assertEqual(
			classify_pi_fiscal_status(
				individual=False, has_factura=False, total=10, signed=0, in_progress=0, precision=3
			),
			"Pending",
		)
		self.assertEqual(
			classify_pi_fiscal_status(
				individual=False, has_factura=True, total=10, signed=10, in_progress=0, precision=3
			),
			"Completed",
		)
		self.assertEqual(
			classify_pi_fiscal_status(
				individual=False, has_factura=True, total=10, signed=6, in_progress=0, precision=3
			),
			"Partial",
		)
		self.assertEqual(
			classify_pi_fiscal_status(
				individual=False, has_factura=True, total=10, signed=6, in_progress=4, precision=3
			),
			"Partial",
		)
		self.assertEqual(
			classify_pi_fiscal_status(
				individual=False, has_factura=True, total=10, signed=0, in_progress=10, precision=3
			),
			"In Progress",
		)
		self.assertEqual(
			classify_pi_fiscal_status(
				individual=False, has_factura=True, total=10, signed=0, in_progress=6, precision=3
			),
			"Pending",
		)
		self.assertEqual(
			classify_pi_fiscal_status(
				individual=False, has_factura=True, total=10, signed=0, in_progress=0, precision=3
			),
			"Pending",
		)

	def test_apply_draft_suffix(self):
		from erpnext_moldova_efactura.utils.fiscal_status import apply_draft_suffix

		self.assertEqual(apply_draft_suffix("Completed", False), "Completed")
		self.assertEqual(apply_draft_suffix("Completed", True), "Completed (Draft)")
		self.assertEqual(apply_draft_suffix("Partial", True), "Partial (Draft)")
		self.assertEqual(apply_draft_suffix("", True), "")

	def test_split_rows_rejected_as_item_count_mismatch(self):
		from types import SimpleNamespace

		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		buyer, pi = self._pair()
		buyer.items[0].item_code = "ITEM-A"
		buyer.items[0].qty = 10
		buyer.items[0].ef_qty = 10
		buyer.items[0].net_amount = 500
		buyer.items[0].amount = 590
		buyer.total = 590
		buyer.vat_total = 90
		pi.grand_total = 590
		pi.total_taxes_and_charges = 90
		pi.items = [
			SimpleNamespace(idx=1, item_code="ITEM-A", item_name="Item A", qty=6, uom="Nos", rate=50, amount=300),
			SimpleNamespace(idx=2, item_code="ITEM-A", item_name="Item A", qty=4, uom="Nos", rate=50, amount=200),
		]
		errors, pairs = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertTrue(any("Item count" in e for e in errors))
		self.assertNotEqual(len(pairs), 2)

	def test_split_amount_rejected_as_item_count_mismatch(self):
		from types import SimpleNamespace

		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		buyer, pi = self._pair()
		buyer.items[0].item_code = "ITEM-A"
		buyer.items[0].qty = 10
		buyer.items[0].ef_qty = 10
		buyer.items[0].net_amount = 500
		buyer.items[0].amount = 590
		buyer.total = 590
		buyer.vat_total = 90
		pi.grand_total = 590
		pi.total_taxes_and_charges = 90
		pi.items = [
			SimpleNamespace(idx=1, item_code="ITEM-A", item_name="Item A", qty=6, uom="Nos", rate=50, amount=300.01),
			SimpleNamespace(idx=2, item_code="ITEM-A", item_name="Item A", qty=4, uom="Nos", rate=50, amount=199.99),
		]
		errors, _ = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertTrue(any("Item count" in e for e in errors))

	def test_split_qty_mismatch(self):
		from types import SimpleNamespace

		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		buyer, pi = self._pair()
		buyer.items[0].item_code = "ITEM-A"
		buyer.items[0].qty = 10
		buyer.items[0].ef_qty = 10
		buyer.items[0].net_amount = 500
		buyer.items[0].amount = 590
		buyer.total = 590
		buyer.vat_total = 90
		pi.grand_total = 590
		pi.total_taxes_and_charges = 90
		pi.items = [
			SimpleNamespace(idx=1, item_code="ITEM-A", item_name="Item A", qty=6, uom="Nos", rate=50, amount=300),
			SimpleNamespace(idx=2, item_code="ITEM-A", item_name="Item A", qty=3, uom="Nos", rate=50, amount=150),
		]
		errors, _ = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertTrue(errors)

	def test_remaining_match_partial_qty_rejected(self):
		from erpnext_moldova_efactura.utils.pi_alloc import match_pi_to_remaining

		buyer, pi = self._pair()
		buyer.items[0].name = "BI-1"
		buyer.items[0].item_code = "ITEM-A"
		buyer.items[0].qty = 10
		buyer.items[0].ef_qty = 10
		pi.items[0].name = "PII-1"
		pi.items[0].item_code = "ITEM-A"
		pi.items[0].qty = 6
		pi.items[0].amount = 300
		allocs, errors = match_pi_to_remaining(buyer, pi)
		self.assertTrue(errors)
		self.assertEqual(allocs, [])
		self.assertTrue(any("quantity" in e.lower() for e in errors))

	def test_remaining_match_full_qty(self):
		from erpnext_moldova_efactura.utils.pi_alloc import match_pi_to_remaining

		buyer, pi = self._pair()
		buyer.items[0].name = "BI-1"
		buyer.items[0].item_code = "ITEM-A"
		pi.items[0].name = "PII-1"
		pi.items[0].item_code = "ITEM-A"
		allocs, errors = match_pi_to_remaining(buyer, pi)
		self.assertEqual(errors, [])
		self.assertEqual(len(allocs), 1)
		self.assertEqual(flt(allocs[0]["qty"]), 2)

	def test_remaining_match_one_of_two_factura_lines(self):
		from copy import deepcopy

		from erpnext_moldova_efactura.utils.pi_alloc import match_pi_to_remaining

		buyer, pi = self._pair()
		second = deepcopy(buyer.items[0])
		second.idx = 2
		second.name = "BI-2"
		second.item_code = "ITEM-B"
		second.supplier_item_name = "Item B"
		buyer.items[0].name = "BI-1"
		buyer.items[0].item_code = "ITEM-A"
		buyer.items.append(second)
		pi.items[0].name = "PII-1"
		pi.items[0].item_code = "ITEM-A"
		allocs, errors = match_pi_to_remaining(buyer, pi)
		self.assertEqual(errors, [])
		self.assertEqual(len(allocs), 1)
		self.assertEqual(allocs[0]["buyer_row"].name, "BI-1")

	def test_validate_allocation_rejects_two_links_for_one_pi_item(self):
		from copy import deepcopy

		from erpnext_moldova_efactura.utils.pi_alloc import validate_allocation_qtys

		buyer, _ = self._pair()
		buyer.items[0].purchase_invoice = "PI-1"
		buyer.items[0].pi_detail = "PII-1"
		second = deepcopy(buyer.items[0])
		second.idx = 2
		second.name = "BI-2"
		second.pi_detail = "PII-1"
		buyer.items.append(second)
		with self.assertRaises(frappe.ValidationError):
			validate_allocation_qtys(buyer)

	def test_remaining_match_rate_mismatch(self):
		from erpnext_moldova_efactura.utils.pi_alloc import match_pi_to_remaining

		buyer, pi = self._pair()
		buyer.items[0].name = "BI-1"
		buyer.items[0].item_code = "ITEM-A"
		pi.items[0].name = "PII-1"
		pi.items[0].item_code = "ITEM-A"
		pi.items[0].rate = 40
		pi.items[0].amount = 80
		_, errors = match_pi_to_remaining(buyer, pi)
		self.assertTrue(errors)
		self.assertTrue(any("rate" in e.lower() for e in errors))

	def test_rate_matches_implied_from_line_amount(self):
		from erpnext_moldova_efactura.utils.pi_match import rate_matches

		buyer, pi = self._pair()
		buyer.items[0].qty = 186.763
		buyer.items[0].ef_qty = 186.763
		buyer.items[0].rate = 26.08
		buyer.items[0].net_amount = 4870.23
		pi.items[0].qty = 186.763
		pi.items[0].rate = 4870.23 / 186.763
		pi.items[0].amount = 4870.23
		self.assertTrue(rate_matches(buyer.items[0], pi.items[0], 2))

	def test_grand_total_one_bani_tolerance(self):
		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		buyer, pi = self._pair(grand_total=118.01)
		errors, _ = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertFalse(any("Grand Total" in e for e in errors))

	def test_grand_total_rounding_gap_still_fails(self):
		from erpnext_moldova_efactura.utils.pi_match import collect_totals_and_line_errors

		buyer, pi = self._pair(grand_total=118.55)
		errors, _ = collect_totals_and_line_errors(buyer, pi, mprec=2, qprec=3)
		self.assertTrue(any("Grand Total" in e for e in errors))
