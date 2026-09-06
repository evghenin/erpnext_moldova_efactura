"""Move early PF header fields to the PEF-aligned names."""

import frappe

RENAMES = {
	"supplier": "supplier_party",
	"series": "f_series",
	"number": "f_number",
	"supplier_idno": "f_supplier_idno",
	"supplier_name": "f_supplier_name",
	"supplier_vat_id": "f_supplier_vat_id",
	"buyer_idno": "f_customer_idno",
	"buyer_vat_id": "f_customer_vat_id",
	"provider_reference": "f_provider_reference",
	"provider_account": "f_provider_account",
	"contract_reference": "f_contract_reference",
	"related_document_type": "f_related_document_type",
	"related_document_number": "f_related_document_number",
	"related_document_date": "f_related_document_date",
}


def execute():
	if not frappe.db.table_exists("Purchase Factura"):
		return
	assignments = []
	for old, new in RENAMES.items():
		if frappe.db.has_column("Purchase Factura", old) and frappe.db.has_column("Purchase Factura", new):
			assignments.append(f"`{new}`=coalesce(nullif(`{new}`, ''), `{old}`)")
	if assignments:
		frappe.db.sql(f"update `tabPurchase Factura` set {', '.join(assignments)}")
	frappe.db.sql("""update `tabPurchase Factura`
		set supplier_party_type='Supplier' where ifnull(supplier_party_type, '')=''""")
