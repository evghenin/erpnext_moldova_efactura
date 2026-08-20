import frappe

from erpnext_moldova_efactura.utils.taxpayer_type import CODE_TO_LABEL

FIELDS = {
	"Purchase eFactura": (
		"ef_supplier_taxpayer_type",
		"ef_customer_taxpayer_type",
	),
	"Sales eFactura": (
		"ef_supplier_taxpayer_type",
		"ef_customer_taxpayer_type",
		"ef_transporter_taxpayer_type",
	),
}


def execute():
	"""Rewrite SFS TaxpayerType codes 1/2/3 to Select labels."""
	for doctype, fields in FIELDS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for field in fields:
			if not frappe.db.has_column(doctype, field):
				continue
			for code, label in CODE_TO_LABEL.items():
				frappe.db.sql(
					f"""
					UPDATE `tab{doctype}`
					SET `{field}` = %s
					WHERE `{field}` = %s
					""",
					(label, code),
				)
