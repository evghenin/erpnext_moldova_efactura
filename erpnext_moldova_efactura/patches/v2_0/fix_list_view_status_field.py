import json

import frappe


def execute():
	"""Frappe list indicator column is status_field, not status."""
	for name in ("Sales eFactura", "Purchase eFactura"):
		if not frappe.db.exists("List View Settings", name):
			continue
		raw = frappe.db.get_value("List View Settings", name, "fields")
		if not raw:
			continue
		fields = json.loads(raw)
		changed = False
		for row in fields:
			if row.get("fieldname") == "status":
				row["fieldname"] = "status_field"
				changed = True
		if changed:
			frappe.db.set_value(
				"List View Settings",
				name,
				"fields",
				json.dumps(fields),
				update_modified=False,
			)
