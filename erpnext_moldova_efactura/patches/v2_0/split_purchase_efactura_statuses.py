import json

import frappe


def execute():
	"""Document indicator is Draft/Submitted/Cancelled; SFS status is efactura_status."""
	if frappe.db.has_column("Purchase eFactura", "efactura_status"):
		frappe.db.sql(
			"""
			UPDATE `tabPurchase eFactura`
			SET efactura_status = status
			WHERE ifnull(efactura_status, '') = '' AND ifnull(status, '') != ''
			"""
		)

	name = "Purchase eFactura"
	if not frappe.db.exists("List View Settings", name):
		return
	raw = frappe.db.get_value("List View Settings", name, "fields")
	if not raw:
		return
	fields = json.loads(raw)
	if any(row.get("fieldname") == "efactura_status" for row in fields):
		return
	insert_at = 2
	for i, row in enumerate(fields):
		if row.get("fieldname") == "status_field":
			insert_at = i + 1
			break
	fields.insert(
		insert_at,
		{"fieldname": "efactura_status", "label": "eFactura Status"},
	)
	frappe.db.set_value(
		"List View Settings",
		name,
		{
			"fields": json.dumps(fields),
			"total_fields": str(len(fields)),
		},
		update_modified=False,
	)
	frappe.clear_cache(doctype=name)
