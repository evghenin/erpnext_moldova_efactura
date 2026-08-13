import frappe

from erpnext_moldova_efactura.utils.uom_map import apply_qty_defaults, resolve_uom


def execute():
	"""Split legacy ef_uom Data into supplier_uom + ef_uom + stock/qty fields."""
	if not frappe.db.table_exists("eFactura Buyer Item"):
		return

	raw_col = "supplier_uom" if frappe.db.has_column("eFactura Buyer Item", "supplier_uom") else "efactura_uom"
	if not frappe.db.has_column("eFactura Buyer Item", raw_col):
		return

	has_stock_qty = frappe.db.has_column("eFactura Buyer Item", "stock_qty")
	has_stock_uom = frappe.db.has_column("eFactura Buyer Item", "stock_uom")

	rows = frappe.db.sql(
		f"""
		SELECT name, parent, ef_uom, `{raw_col}` as supplier_uom, ef_qty, qty, item_code, uom
		FROM `tabeFactura Buyer Item`
		""",
		as_dict=True,
	)
	for row in rows:
		updates = {}
		raw = (row.supplier_uom or "").strip()
		legacy = (row.ef_uom or "").strip()

		if not raw and legacy:
			raw = legacy
			updates[raw_col] = raw

		matched = resolve_uom(raw) if raw else None
		if matched:
			updates["ef_uom"] = matched
		elif legacy and not frappe.db.exists("UOM", legacy):
			updates["ef_uom"] = None

		if not row.ef_qty and row.qty:
			updates["ef_qty"] = row.qty

		if updates:
			frappe.db.set_value("eFactura Buyer Item", row.name, updates, update_modified=False)

	# Re-apply qty defaults for draft parents (fills stock_uom/stock_qty/qty)
	if not (has_stock_qty or has_stock_uom):
		return

	drafts = frappe.get_all("eFactura Buyer", filters={"docstatus": 0}, pluck="name")
	for name in drafts:
		doc = frappe.get_doc("eFactura Buyer", name)
		changed = False
		for item in doc.items:
			before = (item.ef_uom, item.uom, item.qty, getattr(item, "stock_qty", None), getattr(item, "stock_uom", None))
			apply_qty_defaults(item, force=False)
			after = (item.ef_uom, item.uom, item.qty, getattr(item, "stock_qty", None), getattr(item, "stock_uom", None))
			if before != after:
				changed = True
		if changed:
			doc.flags.allow_sfs_item_refresh = True
			doc.save(ignore_permissions=True)
