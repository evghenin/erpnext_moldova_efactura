import frappe

from erpnext_moldova_efactura.utils.buyer_status import PI_LINKED_SUFFIX


def execute():
	"""Drop legacy ' · Linked to PI' from status; shortcut filters by item link."""
	if frappe.db.exists("DocType", "Purchase eFactura"):
		fields = ["status"]
		if frappe.db.has_column("Purchase eFactura", "efactura_status"):
			fields.append("efactura_status")
		for field in fields:
			frappe.db.sql(
				f"""
				UPDATE `tabPurchase eFactura`
				SET `{field}` = REPLACE(`{field}`, %s, '')
				WHERE `{field}` LIKE %s
				""",
				(PI_LINKED_SUFFIX, f"%{PI_LINKED_SUFFIX}%"),
			)
			frappe.db.sql(
				f"""
				UPDATE `tabPurchase eFactura`
				SET `{field}` = 'Signed by Buyer'
				WHERE `{field}` = 'Linked to PI'
				"""
			)

	if frappe.db.exists("Workspace", "eFactura Purchase"):
		frappe.db.sql(
			"""
			UPDATE `tabWorkspace Shortcut`
			SET stats_filter = %s
			WHERE parent = 'eFactura Purchase' AND label = 'Linked to PI'
			""",
			('[["Purchase eFactura Item", "purchase_invoice", "is", "set"]]',),
		)
		frappe.db.sql(
			"""
			UPDATE `tabWorkspace Shortcut`
			SET stats_filter = %s
			WHERE parent = 'eFactura Purchase' AND label = 'Need Purchase Invoice'
			""",
			(
				'[["Purchase eFactura", "status", "in", ["Accepted", "Signed by Buyer"]], ["Purchase eFactura Item", "purchase_invoice", "is", "not set"]]',
			),
		)

	if frappe.db.exists("Number Card", "Purchase eFactura Need PI"):
		frappe.db.set_value(
			"Number Card",
			"Purchase eFactura Need PI",
			"filters_json",
			'[["Purchase eFactura", "status", "in", ["Accepted", "Signed by Buyer"]], ["Purchase eFactura Item", "purchase_invoice", "is", "not set"]]',
			update_modified=False,
		)
