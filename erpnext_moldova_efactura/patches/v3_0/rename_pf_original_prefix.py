"""Copy the intermediate PF ef_* schema into f_* without changing original values."""

import frappe


def execute():
	parent = "Purchase Factura"
	child = "Purchase Factura Item"
	if not frappe.db.table_exists(parent):
		return
	if frappe.db.has_column(parent, "ef_currency"):
		names = frappe.db.sql(
			"""select name from `tabPurchase Factura`
			where ifnull(f_currency, '')='' and ifnull(ef_currency, '')!=''""",
			pluck=True,
		)
		for doctype, key in ((child, "parent"), (parent, "name")):
			assignments = []
			for field in frappe.get_meta(doctype).fields:
				if field.fieldname.startswith("f_"):
					old = "ef_" + field.fieldname[2:]
					if frappe.db.has_column(doctype, old):
						assignments.append(f"`{field.fieldname}`=`{old}`")
			if names and assignments:
				frappe.db.sql(
					f"update `tab{doctype}` set {', '.join(assignments)} where `{key}` in %(names)s",
					{"names": names},
				)
	frappe.db.sql("""update `tabPurchase Factura Item` i
		join `tabPurchase Factura` p on p.name=i.parent
		set i.purchase_invoice=p.purchase_invoice""")
