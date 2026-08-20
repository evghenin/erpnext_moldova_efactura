import frappe


def execute():
	"""Copy eFactura Buyer.purchase_order into the purchase_orders child table."""
	if not frappe.db.exists("DocType", "eFactura Buyer"):
		return
	if not frappe.db.exists("DocType", "eFactura Buyer Purchase Order"):
		return
	if not frappe.db.has_column("eFactura Buyer", "purchase_order"):
		return

	rows = frappe.db.sql(
		"""
		SELECT name, purchase_order
		FROM `tabeFactura Buyer`
		WHERE IFNULL(purchase_order, '') != ''
		""",
		as_dict=True,
	)
	for row in rows:
		exists = frappe.db.exists(
			"eFactura Buyer Purchase Order",
			{"parent": row.name, "purchase_order": row.purchase_order},
		)
		if exists:
			continue
		frappe.get_doc(
			{
				"doctype": "eFactura Buyer Purchase Order",
				"parent": row.name,
				"parenttype": "eFactura Buyer",
				"parentfield": "purchase_orders",
				"purchase_order": row.purchase_order,
			}
		).insert(ignore_permissions=True)
