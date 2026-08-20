import frappe


def execute():
	"""One picker label for eFactura Status: form field is efactura_status, status is hidden."""
	doctype = "Purchase eFactura"
	if not frappe.db.exists("DocType", doctype):
		return

	meta_changed = False
	for fieldname, values in (
		(
			"status",
			{"hidden": 1, "in_list_view": 0, "in_standard_filter": 0, "label": "Status"},
		),
		(
			"efactura_status",
			{"hidden": 0, "in_list_view": 1, "in_standard_filter": 1, "label": "eFactura Status"},
		),
	):
		name = frappe.db.get_value("DocField", {"parent": doctype, "fieldname": fieldname}, "name")
		if not name:
			continue
		frappe.db.set_value("DocField", name, values, update_modified=False)
		meta_changed = True

	for ps in (
		"Purchase eFactura-status-in_list_view",
		"Purchase eFactura-efactura_status-hidden",
	):
		if frappe.db.exists("Property Setter", ps):
			frappe.delete_doc("Property Setter", ps, force=1, ignore_permissions=True)
			meta_changed = True

	if meta_changed:
		frappe.clear_cache(doctype=doctype)
